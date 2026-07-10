"""Shared SQL builders for deal detection, counts and the price guide.

Single source of truth for the scoring model, used by both the Flask API
(App.py) and the scheduler-side deal surfacing (EbayScraper.SurfaceDeals).

Pricing basis: EFFECTIVE price = item price + shipping (both stored in pence).
Shipping applies to both the sold-market statistics and the live listing
price, so discounts are postage-inclusive and apples-to-apples.
"""
import os

# Effective listing price in pounds. Older rows scraped before the Shipping
# column existed have NULL shipping and are treated as free-postage.
EFF = "((e.Price + COALESCE(e.Shipping, 0)) / 100)"

# Units in the listing (job lots — HDD for now). NULL (pre-migration rows and
# non-lot categories) means 1; GREATEST guards a bad 0 from ever dividing.
QTY = "GREATEST(COALESCE(e.Quantity, 1), 1)"

# Per-unit effective price — the deal-detection basis. A lot is a deal when
# its price PER UNIT beats the single-item market median: parting out resells
# per unit, so that's the number the discount is really on.
EFF_UNIT = f"({EFF} / {QTY})"

# Freshness gate: only surface deals the scraper has actually SEEN recently.
# Seller-cancelled listings vanish from eBay search but keep a future EndTime
# in the DB — without this they'd show as phantom deals (dead link, "listing
# was ended by the seller") until the original end time passed. Default 90 min
# = one full-scrape cycle plus margin; targeted scrapes re-stamp tracked deals
# far more often than that in their final hour.
STALE_DEAL_MINUTES = int(os.environ.get('STALE_DEAL_MINUTES', '90'))
FRESH_OK = f"e.LastSeenAt > NOW() - INTERVAL {STALE_DEAL_MINUTES} MINUTE"

# Seller-quality gate for the deal feed (stats are unaffected — a sold price
# is market data regardless of who sold it). The percentage is only trusted
# once the seller has real history: "0% positive (0)" is a brand-new account,
# not a scammer, and NULL is a row not re-scraped since the column landed.
MIN_SELLER_FEEDBACK_PCT = float(os.environ.get('MIN_SELLER_FEEDBACK_PCT', '90'))
FEEDBACK_OK = ("(e.SellerFeedbackCount IS NULL OR e.SellerFeedbackCount < 3 "
               f"OR e.SellerFeedbackPct >= {MIN_SELLER_FEEDBACK_PCT})")

# Per-category config.
#   table         satellite table name
#   alias         SQL alias for the satellite table
#   group_cols    (column, null_safe_join) tuples the market stats group by
#   not_null      columns that must be non-NULL for a row to enter the stats
#   deal_select   extra satellite columns surfaced on deal rows (UI contract)
#   guide_order   ORDER BY clause for the price-guide query
CATEGORIES = {
    'gpu': {
        'table': 'GPU', 'alias': 'g',
        'group_cols': [('Model', False)],
        'not_null': ['Model'],
        'deal_select': ['g.Model', 'g.Brand', 'g.VRAM'],
        'guide_select': ['rs.Model'],
        'guide_order': 'ms.AvgPrice DESC',
    },
    'cpu': {
        'table': 'CPU', 'alias': 'c',
        'group_cols': [('Model', False)],
        'not_null': ['Model'],
        'deal_select': ['c.Model', 'c.Brand', 'c.Socket', 'c.Cores'],
        'guide_select': ['rs.Model'],
        'guide_order': 'ms.AvgPrice DESC',
    },
    'hdd': {
        'table': 'HDD', 'alias': 'h',
        # DriveType splits Internal vs External so a portable USB drive is never
        # priced against a bare internal of the same capacity (null_safe: legacy
        # rows may still be NULL between migration and backfill).
        'group_cols': [('CapacityGB', False), ('Interface', True), ('DriveType', True)],
        'not_null': ['CapacityGB'],
        'deal_select': ['h.Brand', 'h.CapacityGB', 'h.Interface', 'h.FormFactor', 'h.RPM', 'h.DriveType'],
        'guide_select': ['rs.CapacityGB', 'rs.Interface', 'rs.DriveType'],
        'guide_order': 'rs.CapacityGB DESC, ms.AvgPrice DESC',
    },
    'ram': {
        'table': 'RAM', 'alias': 'r',
        # FormFactor splits DIMM (desktop) vs SODIMM (laptop) — different markets.
        'group_cols': [('Type', False), ('CapacityGB', False), ('FormFactor', True)],
        'not_null': ['Type', 'CapacityGB'],
        'deal_select': ['r.Brand', 'r.CapacityGB', 'r.Type', 'r.Speed', 'r.FormFactor'],
        'guide_select': ['rs.Type', 'rs.CapacityGB', 'rs.FormFactor'],
        'guide_order': 'rs.Type, rs.CapacityGB',
    },
}


def _clamp_window(window_hours) -> int:
    return max(1, min(int(window_hours), 24))


def _clamp_threshold(min_discount) -> float:
    return (100 - max(0.0, float(min_discount))) / 100.0


def _join_cond(cfg, left: str, right: str) -> str:
    parts = []
    for col, null_safe in cfg['group_cols']:
        op = '<=>' if null_safe else '='
        parts.append(f"{left}.{col} {op} {right}.{col}")
    return ' AND '.join(parts)


# eBay sold prices are heavily right-skewed (bundles, mislabelled multi-item
# lots): on real data the GPU mean sat ~75% above the median. Market price is
# therefore the MEDIAN of sold effective prices — a single absurd sale cannot
# move it. Display min/max come from a sanity band around the median so the
# UI range isn't stretched by outliers either.
BAND_LO, BAND_HI = 0.4, 2.5


def _median_ctes(cfg) -> str:
    """SoldRows (per-sale effective prices) + RawStats (median + count per group)."""
    a = cfg['alias']
    group = ', '.join(f"{a}.{col}" for col, _ in cfg['group_cols'])
    cols = ', '.join(col for col, _ in cfg['group_cols'])
    not_null = ' AND '.join(f"{a}.{col} IS NOT NULL" for col in cfg['not_null'])
    return f"""SoldRows AS (
    SELECT {group}, {EFF} AS Eff
    FROM Scraper.{cfg['table']} {a}
    JOIN Scraper.EBAY e ON e.ID = {a}.ID
    WHERE e.SoldDate IS NOT NULL AND e.Price IS NOT NULL AND {not_null}
      AND COALESCE(e.Quantity, 1) = 1
),
RawStats AS (
    SELECT DISTINCT {cols},
           MEDIAN(Eff) OVER (PARTITION BY {cols}) AS MedPrice,
           COUNT(*)    OVER (PARTITION BY {cols}) AS SoldCount
    FROM SoldRows
)"""


def _stats_ctes(cfg, min_sold: int) -> str:
    """Median CTEs + ModelStats (median market price, banded min/max)."""
    sr_cols = ', '.join(f"sr.{col}" for col, _ in cfg['group_cols'])
    join_sr_rs = _join_cond(cfg, 'sr', 'rs')
    return f"""
WITH {_median_ctes(cfg)},
ModelStats AS (
    SELECT {sr_cols},
           ROUND(rs.MedPrice, 2)  AS AvgPrice,
           ROUND(MIN(sr.Eff), 2)  AS MinMarketPrice,
           ROUND(MAX(sr.Eff), 2)  AS MaxMarketPrice,
           rs.SoldCount           AS SoldCount
    FROM SoldRows sr
    JOIN RawStats rs ON {join_sr_rs}
    WHERE rs.SoldCount >= {min_sold}
      AND sr.Eff BETWEEN rs.MedPrice * {BAND_LO} AND rs.MedPrice * {BAND_HI}
    GROUP BY {sr_cols}, rs.MedPrice, rs.SoldCount
)"""


def build_deals_query(product_type: str, window_hours: int = 2, min_discount: float = 20) -> str:
    cfg = CATEGORIES[product_type]
    a = cfg['alias']
    interval = f"INTERVAL {_clamp_window(window_hours)} HOUR"
    threshold = _clamp_threshold(min_discount)
    ctes = _stats_ctes(cfg, min_sold=5)
    extra = ',\n    '.join(cfg['deal_select'])
    # DealScore: discount% weighted by urgency (1/hours-left, floored at 15
    # min so the divisor can't explode) and damped by competition (1/(1+bids))
    # — a 25%-off item ending in 20 min with no bids outranks a 40%-off item
    # ending in 6 h with 9 bidders that will be bid up anyway.
    # CurrentPrice is the whole listing (what you'd bid); discount and score
    # are per-unit; PotentialGain is whole-lot (median × qty − price).
    return f"""{ctes}
SELECT
    e.ID,
    {extra},
    ROUND({EFF}, 2)                              AS CurrentPrice,
    {QTY}                                        AS Quantity,
    ROUND({EFF_UNIT}, 2)                         AS PerUnitPrice,
    ms.AvgPrice                                  AS AvgMarketPrice,
    ms.MinMarketPrice,
    ms.MaxMarketPrice,
    ROUND(ms.AvgPrice * {QTY} - {EFF}, 2)        AS PotentialGain,
    ROUND((1 - {EFF_UNIT} / ms.AvgPrice) * 100, 1) AS DiscountPct,
    ROUND(((1 - {EFF_UNIT} / ms.AvgPrice) * 100)
        / GREATEST(TIMESTAMPDIFF(MINUTE, NOW(), e.EndTime) / 60.0, 0.25)
        / (1 + COALESCE(e.Bids, 0)), 2)          AS DealScore,
    e.Bids,
    e.SellerFeedbackPct,
    e.SellerFeedbackCount,
    e.EndTime,
    e.URL
FROM Scraper.EBAY e
JOIN Scraper.{cfg['table']} {a} ON {a}.ID = e.ID
JOIN ModelStats ms ON {_join_cond(cfg, 'ms', a)}
WHERE
    e.SoldDate IS NULL
    AND {EFF_UNIT} < ms.AvgPrice * {threshold}
    AND {FEEDBACK_OK}
    AND {FRESH_OK}
    AND e.EndTime > NOW()
    AND e.EndTime < NOW() + {interval}
ORDER BY DealScore DESC;
"""


def build_count_query(product_type: str, window_hours: int = 2, min_discount: float = 20) -> str:
    cfg = CATEGORIES[product_type]
    a = cfg['alias']
    interval = f"INTERVAL {_clamp_window(window_hours)} HOUR"
    threshold = _clamp_threshold(min_discount)
    # Same median basis as the deals query — the tab badges previously used an
    # untrimmed mean and could disagree with the list they were counting.
    return f"""
WITH {_median_ctes(cfg)}
SELECT COUNT(*) AS cnt
FROM Scraper.EBAY e
JOIN Scraper.{cfg['table']} {a} ON {a}.ID = e.ID
JOIN RawStats rs ON {_join_cond(cfg, 'rs', a)}
WHERE rs.SoldCount >= 5
  AND e.SoldDate IS NULL AND {EFF_UNIT} < rs.MedPrice * {threshold}
  AND {FEEDBACK_OK}
  AND {FRESH_OK}
  AND e.EndTime > NOW() AND e.EndTime < NOW() + {interval};
"""


def build_price_guide_query(product_type: str) -> str:
    cfg = CATEGORIES[product_type]
    ctes = _stats_ctes(cfg, min_sold=3)
    guide_cols = ',\n       '.join(cfg['guide_select'])
    return f"""{ctes}
SELECT {guide_cols},
       ms.AvgPrice,
       ms.MinMarketPrice AS MinPrice,
       ms.MaxMarketPrice AS MaxPrice,
       ms.SoldCount
FROM   ModelStats ms
JOIN   RawStats rs ON {_join_cond(cfg, 'rs', 'ms')}
ORDER  BY {cfg['guide_order']};
"""


# ── snipe-premium predictions ──────────────────────────────────────────────────
# Lives here (not EbayScraper) because the web image ships only App.py +
# queries.py, and both containers need to annotate deal rows identically.

def bid_bucket(bids) -> str:
    """Bucket a bid count for premium stats: '0', '1-3' or '4+'."""
    if not bids:
        return '0'
    return '1-3' if bids <= 3 else '4+'


def median_ratios(rows, min_samples: int = 5) -> dict:
    """rows: (category, bid_count, surfaced_price, final_price) tuples.

    Returns {(category, bucket): (median_final_over_surfaced, sample_count)}
    where bucket is a bid_bucket value plus an 'all' category-level fallback.
    Groups below min_samples are dropped — too little history to trust.
    """
    import statistics
    groups = {}
    for cat, bids, surfaced, final in rows:
        if not surfaced or final is None:
            continue
        ratio = float(final) / float(surfaced)
        groups.setdefault((cat, bid_bucket(bids)), []).append(ratio)
        groups.setdefault((cat, 'all'), []).append(ratio)
    return {
        key: (round(statistics.median(vals), 3), len(vals))
        for key, vals in groups.items()
        if len(vals) >= min_samples
    }


# Resolved outcomes that feed the premium model (sold, not ended-unsold).
SNIPE_PREMIUM_QUERY = """
SELECT d.Category, d.BidCount, d.SurfacedPrice,
       COALESCE(d.FinalPrice, e.Price)
FROM Scraper.DealOutcomes d
JOIN Scraper.EBAY e ON e.ID = d.EbayID
WHERE e.SoldDate IS NOT NULL
  AND d.EndedUnsold = 0
  AND d.SurfacedPrice > 0
  AND COALESCE(d.FinalPrice, e.Price) IS NOT NULL;
"""


def annotate_predictions(rows: list, product_type: str, premiums: dict, now=None) -> list:
    """Add outcome-calibrated final-price predictions to deal rows, in place.

    History says contested auctions close above their spotted price (e.g. HDD
    4+ bids ~1.56×), so a raw current-price discount overstates many deals.
    Each row gains PredictedFinalPrice / PredictedDiscountPct / PremiumSamples,
    and DealScore is recomputed on the PREDICTED discount, then rows re-sorted.
    Ratio fallback: (cat, bucket) → (cat, 'all') → 1.0, so with no history the
    prediction equals the current price and nothing changes.
    """
    from datetime import datetime, timezone
    cat = product_type.upper()
    if now is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    for row in rows:
        bids = int(row.get('Bids') or 0)
        entry = premiums.get((cat, bid_bucket(bids))) or premiums.get((cat, 'all'))
        ratio, samples = entry if entry else (1.0, 0)
        price = float(row.get('CurrentPrice') or 0)
        qty = int(row.get('Quantity') or 1)
        market_lot = float(row.get('AvgMarketPrice') or 0) * qty
        predicted = round(price * ratio, 2)
        row['PredictedFinalPrice'] = predicted
        row['PremiumSamples'] = samples
        row['PredictedDiscountPct'] = (
            round((1 - predicted / market_lot) * 100, 1) if market_lot > 0 else None)
        end = row.get('EndTime')
        hours = 0.25
        if end is not None and hasattr(end, 'timestamp'):
            hours = max((end - now).total_seconds() / 3600.0, 0.25)
        row['DealScore'] = round(
            max(row['PredictedDiscountPct'] or 0, 0) / hours / (1 + bids), 2)
    rows.sort(key=lambda r: r.get('DealScore') or 0, reverse=True)
    return rows


def filter_predicted_deals(rows: list) -> list:
    """Keep only deals predicted to close BELOW their market value.

    A row with premium history whose predicted final lands at/above the
    (lot-scaled) median is a deal in name only — history says bidding will
    erase the discount. Rows without premium history pass through unchanged:
    their prediction equals the current price, so PredictedDiscountPct is the
    current discount, which already met the feed threshold.

    Display/notification gate only — SurfaceDeals records every first
    sighting regardless, so resolved outcomes can prove (or disprove) that
    the rows this hides really would have been bid past their value.
    """
    return [r for r in rows
            if r.get('PredictedDiscountPct') is None or r['PredictedDiscountPct'] > 0]


def model_label_for_row(product_type: str, row: dict) -> str:
    """Human label stored in DealOutcomes.Model for a surfaced deal row.
    Multi-unit lots get a ×N suffix (e.g. '4TB SAS ×5')."""
    label = _base_label(product_type, row)
    qty = int(row.get('Quantity') or 1)
    return f"{label} ×{qty}" if qty > 1 and label else label


def _base_label(product_type: str, row: dict) -> str:
    if product_type == 'hdd':
        cap = row.get('CapacityGB')
        iface = row.get('Interface') or 'SATA'
        # Only annotate the non-default (External) — internal is the norm.
        ext = ' External' if row.get('DriveType') == 'External' else ''
        if cap and cap >= 1000:
            tb = cap / 1000
            size = f"{int(tb)}TB" if cap % 1000 == 0 else f"{tb:.1f}TB"
            return f"{size} {iface}{ext}"
        return f"{cap}GB {iface}{ext}" if cap else f"{iface}{ext}"
    if product_type == 'ram':
        cap = row.get('CapacityGB')
        ram_type = row.get('Type') or 'RAM'
        # Only annotate the non-default (SODIMM) — DIMM is the norm.
        so = ' SODIMM' if row.get('FormFactor') == 'SODIMM' else ''
        return f"{cap}GB {ram_type}{so}" if cap else f"{ram_type}{so}"
    return row.get('Model')
