"""Shared SQL builders for deal detection, counts and the price guide.

Single source of truth for the scoring model, used by both the Flask API
(App.py) and the scheduler-side deal surfacing (EbayScraper.SurfaceDeals).

Pricing basis: EFFECTIVE price = item price + shipping (both stored in pence).
Shipping applies to both the sold-market statistics and the live listing
price, so discounts are postage-inclusive and apples-to-apples.
"""

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
    e.EndTime,
    e.URL
FROM Scraper.EBAY e
JOIN Scraper.{cfg['table']} {a} ON {a}.ID = e.ID
JOIN ModelStats ms ON {_join_cond(cfg, 'ms', a)}
WHERE
    e.SoldDate IS NULL
    AND {EFF_UNIT} < ms.AvgPrice * {threshold}
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
