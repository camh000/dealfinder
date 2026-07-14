"""Shared SQL builders for deal detection, counts and the price guide.

Single source of truth for the scoring model, used by both the Flask API
(App.py) and the scheduler-side deal surfacing (EbayScraper.SurfaceDeals).

Pricing basis: EFFECTIVE price = item price + shipping (both stored in pence).
Shipping applies to both the sold-market statistics and the live listing
price, so discounts are postage-inclusive and apples-to-apples.
"""
import math
import os
import re


# ── CPU socket derivation ───────────────────────────────────────────────────
# A CPU's socket is a function of its family + generation, so a compact rules
# table fills it for the ~43% of listings whose title never states it — no
# per-SKU master table needed. Genuinely ambiguous cases (Intel HEDT X-series
# that overlaps mainstream generations, mobile BGA parts) return None rather
# than a confidently-wrong socket. Models arrive already normalised
# ("i5-6600K", "Ryzen 5 5600X", "Xeon E5-2680 V4").

_INTEL_CORE_SOCKET = {          # mainstream desktop Core, by generation
    2: 'LGA1155', 3: 'LGA1155', 4: 'LGA1150', 5: 'LGA1150',
    6: 'LGA1151', 7: 'LGA1151', 8: 'LGA1151', 9: 'LGA1151',
    10: 'LGA1200', 11: 'LGA1200', 12: 'LGA1700', 13: 'LGA1700', 14: 'LGA1700',
}
_INTEL_HEDT_SOCKET = {          # X-series / -E enthusiast platforms, by generation
    3: 'LGA2011', 4: 'LGA2011', 5: 'LGA2011-3', 6: 'LGA2011-3',
    7: 'LGA2066', 9: 'LGA2066', 10: 'LGA2066',
}
# HEDT SKUs ending in K are indistinguishable from mainstream by generation.
_INTEL_HEDT_SKUS = {'3820', '3930K', '3960X', '4820K', '4930K', '4960X',
                    '5820K', '5930K', '5960X', '6800K', '6850K', '6900K', '6950X'}
_XEON_E3_SOCKET = {1: 'LGA1155', 2: 'LGA1155', 3: 'LGA1150', 4: 'LGA1150',
                   5: 'LGA1151', 6: 'LGA1151'}


def socket_for(model) -> str | None:
    """Derive a CPU socket from its normalised model string, or None if the
    family/generation doesn't pin one unambiguously."""
    if not model:
        return None
    u = str(model).strip().upper()

    # ── AMD Ryzen — "RYZEN 5 5600X", "RYZEN 3 3400G", "RYZEN 9 7940HS" ──
    m = re.match(r'RYZEN\s+\d\s+(\d)\d{2,3}([A-Z0-9]*)$', u)
    if m:
        gen, suf = int(m.group(1)), m.group(2)
        if suf.endswith(('HS', 'HX', 'H', 'U')):     # mobile BGA — no socket
            return None
        if gen in (1, 2, 3, 4, 5):
            return 'AM4'
        if gen in (7, 8, 9):
            return 'AM5'
        return None
    if 'THREADRIPPER' in u:
        return None                                   # TR4 / sTRX4 / sWRX8 vary

    # ── Intel Core — "I5-6600K", "I7-10700K", "I9-13900KF" ──
    m = re.match(r'I([3579])-(\d{3,5})([A-Z]*)$', u)
    if m:
        tier, num, suf = int(m.group(1)), m.group(2), m.group(3)
        if (num + suf) in _INTEL_HEDT_SKUS or (tier in (7, 9) and suf.startswith('X')):
            gen = int(num) // 1000 if len(num) >= 4 else int(num[0])
            return _INTEL_HEDT_SOCKET.get(gen)
        if len(num) == 3:                              # 1st-gen (Nehalem/Westmere)
            return 'LGA1366' if num[0] == '9' else 'LGA1156'
        return _INTEL_CORE_SOCKET.get(int(num) // 1000)

    # ── Intel Xeon ──
    return _xeon_socket(u)


def _xeon_socket(u) -> str | None:
    m = re.match(r'XEON\s+(E\d)-\d{4}(?:\s+V(\d))?', u)          # E3/E5/E7 (+Vn)
    if m:
        fam, v = m.group(1), int(m.group(2) or 1)
        if fam == 'E3':
            return _XEON_E3_SOCKET.get(v)
        if fam == 'E5':
            return 'LGA2011' if v <= 2 else 'LGA2011-3'
        return 'LGA2011'                                          # E7 (coarse)
    m = re.match(r'XEON\s+(?:SILVER|GOLD|PLATINUM|BRONZE)\s+(\d{4})', u)  # Scalable
    if m:
        g = int(m.group(1)[1])                                   # 2nd digit = gen
        if g <= 2:
            return 'LGA3647'
        if g == 3:
            return 'LGA4189'
        return 'LGA4677'                                          # Sapphire/Emerald
    m = re.match(r'XEON\s+W-(\d)\d{3}', u)                        # W-1250 / W-2145 / W-3175
    if m:
        return {'1': 'LGA1151', '2': 'LGA2066', '3': 'LGA3647'}.get(m.group(1))
    if re.match(r'XEON\s+E-2\d{3}', u):                           # E-2224G (Coffee)
        return 'LGA1151'
    m = re.match(r'XEON\s+[WEXL](\d)\d{3}', u)                    # legacy X5670 / W3690
    if m:
        return 'LGA1366' if m.group(1) in ('3', '5') else None
    return None


# ── Motherboard chipset → socket ────────────────────────────────────────────
# A board's socket is fixed by its chipset. This small, stable table (~60
# chipsets) is the mobo equivalent of the CPU family rules — it pins the socket
# and doubles as the chipset vocabulary the parser recognises. Chipset codes
# don't collide across vendors (AMD A/B/X + specific numbers vs Intel
# H/B/Z/Q/P/X + different numbers), so one flat map is unambiguous.
_CHIPSET_SOCKET = {
    # AMD AM4
    'A320': 'AM4', 'B350': 'AM4', 'X370': 'AM4', 'B450': 'AM4', 'X470': 'AM4',
    'A520': 'AM4', 'B550': 'AM4', 'X570': 'AM4',
    # AMD AM5
    'A620': 'AM5', 'B650': 'AM5', 'B650E': 'AM5', 'X670': 'AM5', 'X670E': 'AM5',
    'B840': 'AM5', 'B850': 'AM5', 'X870': 'AM5', 'X870E': 'AM5',
    # AMD Threadripper
    'X399': 'TR4', 'TRX40': 'sTRX4', 'WRX80': 'sWRX8',
    # Intel LGA1155 (Sandy/Ivy Bridge)
    'H61': 'LGA1155', 'B75': 'LGA1155', 'Q75': 'LGA1155', 'H77': 'LGA1155',
    'Z75': 'LGA1155', 'Z77': 'LGA1155', 'P67': 'LGA1155', 'H67': 'LGA1155',
    'Z68': 'LGA1155', 'Q77': 'LGA1155',
    # Intel LGA1150 (Haswell / Broadwell)
    'H81': 'LGA1150', 'B85': 'LGA1150', 'Q87': 'LGA1150', 'H87': 'LGA1150',
    'Z87': 'LGA1150', 'H97': 'LGA1150', 'Z97': 'LGA1150',
    # Intel LGA1151 (Skylake → Coffee Lake, 100/200/300 series)
    'H110': 'LGA1151', 'B150': 'LGA1151', 'Q150': 'LGA1151', 'H170': 'LGA1151',
    'Z170': 'LGA1151', 'B250': 'LGA1151', 'H270': 'LGA1151', 'Z270': 'LGA1151',
    'H310': 'LGA1151', 'B360': 'LGA1151', 'B365': 'LGA1151', 'H370': 'LGA1151',
    'Q370': 'LGA1151', 'Z370': 'LGA1151', 'Z390': 'LGA1151',
    # Intel LGA1200 (Comet / Rocket Lake, 400/500 series)
    'H410': 'LGA1200', 'B460': 'LGA1200', 'H470': 'LGA1200', 'Z490': 'LGA1200',
    'Q470': 'LGA1200', 'H510': 'LGA1200', 'B560': 'LGA1200', 'H570': 'LGA1200',
    'Z590': 'LGA1200',
    # Intel LGA1700 (Alder / Raptor Lake, 600/700 series)
    'H610': 'LGA1700', 'B660': 'LGA1700', 'H670': 'LGA1700', 'Z690': 'LGA1700',
    'B760': 'LGA1700', 'H770': 'LGA1700', 'Z790': 'LGA1700',
    # Intel HEDT
    'X79': 'LGA2011', 'X99': 'LGA2011-3', 'X299': 'LGA2066',
}
# Chipset codes longest-first so "X670E"/"B650E" win over "X670"/"B650".
CHIPSETS = sorted(_CHIPSET_SOCKET, key=len, reverse=True)


def chipset_socket(chipset) -> str | None:
    """Socket for a motherboard chipset code, or None if unknown."""
    return _CHIPSET_SOCKET.get(str(chipset or '').upper())

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
        'has_bundle': True,   # CPU+mobo bundles live here too — kept out of stats
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
    'ssd': {
        'table': 'SSD', 'alias': 's',
        # DriveType splits portable/external USB SSDs from internal drives —
        # different markets, same reasoning as HDD. Gen deliberately NOT a
        # group dimension: most titles omit it and grouping on it would
        # fragment forever; it's display-only.
        'group_cols': [('CapacityGB', False), ('Interface', True), ('DriveType', True)],
        'not_null': ['CapacityGB'],
        'deal_select': ['s.Brand', 's.CapacityGB', 's.Interface', 's.FormFactor', 's.DriveType', 's.Gen'],
        'guide_select': ['rs.CapacityGB', 'rs.Interface', 'rs.DriveType'],
        'guide_order': 'rs.CapacityGB DESC, ms.AvgPrice DESC',
    },
    'mobo': {
        'table': 'MOBO', 'alias': 'mb',
        # Grouped by chipset + form factor: an ITX/mATX board of the same
        # chipset commands a real premium over full ATX, so they price apart
        # (null-safe FormFactor so a rare unstated one still forms a group).
        'group_cols': [('Chipset', False), ('FormFactor', True)],
        'not_null': ['Chipset'],
        'deal_select': ['mb.Brand', 'mb.Chipset', 'mb.Socket', 'mb.FormFactor'],
        'guide_select': ['rs.Chipset', 'rs.FormFactor'],
        'guide_order': 'rs.Chipset, ms.AvgPrice DESC',
        'has_bundle': True,   # a bundle is a MOBO row too — kept out of stats
    },
    'ram': {
        'table': 'RAM', 'alias': 'r',
        # FormFactor splits DIMM (desktop) vs SODIMM (laptop); KitConfig splits
        # stick composition — at the same total capacity, 2x8 sold ~31% above
        # 1x16 and ~56% above 4x4 (120d data). NULL KitConfig (unstated titles)
        # forms its own blended group via the null-safe join.
        'group_cols': [('Type', False), ('CapacityGB', False), ('FormFactor', True), ('KitConfig', True)],
        'not_null': ['Type', 'CapacityGB'],
        'deal_select': ['r.Brand', 'r.CapacityGB', 'r.Type', 'r.Speed', 'r.FormFactor', 'r.KitConfig'],
        'guide_select': ['rs.Type', 'rs.CapacityGB', 'rs.FormFactor', 'rs.KitConfig'],
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


def _bundle_excl(cfg, alias: str | None = None) -> str:
    """SQL fragment excluding CPU+mobo bundles. Their price covers two
    components, so they'd poison a single-item median and score as a fake deal —
    kept out of the stats and the scored feed, surfaced separately instead."""
    return f" AND {alias or cfg['alias']}.IsBundle = 0" if cfg.get('has_bundle') else ""


# eBay sold prices are heavily right-skewed (bundles, mislabelled multi-item
# lots): on real data the GPU mean sat ~75% above the median. Market price is
# therefore the MEDIAN of sold effective prices — a single absurd sale cannot
# move it. Display min/max come from a sanity band around the median so the
# UI range isn't stretched by outliers either.
BAND_LO, BAND_HI = 0.4, 2.5

# Market stats only trust recent sales. Component prices drift — GPUs
# especially — so a median blending year-old sales with last week's
# misprices today's market in both directions. Trade-off: a shorter window
# is more current but drops thin models below the sold-count floor sooner.
MARKET_STATS_DAYS = int(os.environ.get('MARKET_STATS_DAYS', '120'))


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
      AND e.SoldDate > NOW() - INTERVAL {MARKET_STATS_DAYS} DAY
      AND COALESCE(e.Quantity, 1) = 1{_bundle_excl(cfg)}
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
    ROUND(e.Price / 100, 2)                      AS ItemPrice,
    ROUND(COALESCE(e.Shipping, 0) / 100, 2)      AS Shipping,
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
    dout.SurfacedAt,
    e.EndTime,
    e.URL
FROM Scraper.EBAY e
JOIN Scraper.{cfg['table']} {a} ON {a}.ID = e.ID
JOIN ModelStats ms ON {_join_cond(cfg, 'ms', a)}
LEFT JOIN Scraper.DealOutcomes dout ON dout.EbayID = e.ID
WHERE
    e.SoldDate IS NULL
    AND COALESCE(e.ListingType, 'auction') = 'auction'
    AND {EFF_UNIT} < ms.AvgPrice * {threshold}
    AND {FEEDBACK_OK}
    AND {FRESH_OK}
    AND COALESCE(e.ReserveNotMet, 0) = 0
    AND e.EndTime > NOW()
    AND e.EndTime < NOW() + {interval}{_bundle_excl(cfg)}
ORDER BY DealScore DESC;
"""


def build_bin_deals_query(product_type: str, min_discount: float = 25,
                          added_within_hours: int | None = None) -> str:
    """Buy-It-Now bargains: live fixed-price listings priced under the sold
    median RIGHT NOW. No bidding dynamics — the listed price IS the final
    price — so there's no prediction gate, no bid damping and no urgency
    weighting; the discount is real the moment it's seen. Sorted by discount.
    The default threshold is stricter than the auction feed's 20%: a BIN find
    pings a phone immediately, so it has to be worth interrupting someone.

    added_within_hours (optional) restricts to listings FIRST SEEN in the last
    N hours — the browsable feed's time window (FirstSeenAt is set once on
    insert, so re-seeing a listing every sweep doesn't reset it). NULL = no
    limit beyond the freshness gate."""
    cfg = CATEGORIES[product_type]
    a = cfg['alias']
    threshold = _clamp_threshold(min_discount)
    ctes = _stats_ctes(cfg, min_sold=5)
    extra = ',\n    '.join(cfg['deal_select'])
    added_clause = ''
    if added_within_hours is not None:
        h = max(1, min(int(added_within_hours), 720))
        added_clause = f"\n    AND e.FirstSeenAt > NOW() - INTERVAL {h} HOUR"
    return f"""{ctes}
SELECT
    e.ID,
    {extra},
    ROUND({EFF}, 2)                              AS CurrentPrice,
    ROUND(e.Price / 100, 2)                      AS ItemPrice,
    ROUND(COALESCE(e.Shipping, 0) / 100, 2)      AS Shipping,
    {QTY}                                        AS Quantity,
    ROUND({EFF_UNIT}, 2)                         AS PerUnitPrice,
    ms.AvgPrice                                  AS AvgMarketPrice,
    ms.MinMarketPrice,
    ms.MaxMarketPrice,
    ROUND(ms.AvgPrice * {QTY} - {EFF}, 2)        AS PotentialGain,
    ROUND((1 - {EFF_UNIT} / ms.AvgPrice) * 100, 1) AS DiscountPct,
    e.SellerFeedbackPct,
    e.SellerFeedbackCount,
    e.FirstSeenAt,
    e.URL
FROM Scraper.EBAY e
JOIN Scraper.{cfg['table']} {a} ON {a}.ID = e.ID
JOIN ModelStats ms ON {_join_cond(cfg, 'ms', a)}
WHERE
    e.SoldDate IS NULL
    AND e.ListingType = 'bin'
    AND COALESCE(e.Bids, 0) = 0
    AND COALESCE(e.ReserveNotMet, 0) = 0
    AND {EFF_UNIT} < ms.AvgPrice * {threshold}
    AND {FEEDBACK_OK}
    AND {FRESH_OK}{added_clause}{_bundle_excl(cfg)}
ORDER BY DiscountPct DESC;
"""


def build_bundle_deals_query(min_discount: float = 20, min_sold: int = 3) -> str:
    """Live CPU+motherboard bundle deals, SCORED against the sum of the parts:
    a bundle's market value = the bare CPU model's median + the bare
    chipset+form-factor board's median. Both component groups must have
    enough recent sales (min_sold) or the bundle can't be valued and is
    skipped. Bundles are excluded from those component medians (IsBundle=0
    filters below), so they can't inflate the very prices they're valued
    against. Returns one row per bundle listing with BOTH the CPU and the mobo
    attributes, so it renders (and filters) under either category. Not tracked
    as an outcome — bundles never touch the win-rate stats."""
    threshold = _clamp_threshold(min_discount)
    return f"""
WITH CpuMed AS (
    SELECT c.Model,
           MEDIAN({EFF}) OVER (PARTITION BY c.Model) AS Med,
           COUNT(*)      OVER (PARTITION BY c.Model) AS N
    FROM Scraper.CPU c JOIN Scraper.EBAY e ON e.ID = c.ID
    WHERE e.SoldDate IS NOT NULL AND e.Price IS NOT NULL AND c.Model IS NOT NULL
      AND c.IsBundle = 0 AND e.SoldDate > NOW() - INTERVAL {MARKET_STATS_DAYS} DAY
      AND COALESCE(e.Quantity, 1) = 1
),
CpuStats AS (SELECT DISTINCT Model, Med FROM CpuMed WHERE N >= {min_sold}),
MoboMed AS (
    SELECT mb.Chipset, mb.FormFactor,
           MEDIAN({EFF}) OVER (PARTITION BY mb.Chipset, mb.FormFactor) AS Med,
           COUNT(*)      OVER (PARTITION BY mb.Chipset, mb.FormFactor) AS N
    FROM Scraper.MOBO mb JOIN Scraper.EBAY e ON e.ID = mb.ID
    WHERE e.SoldDate IS NOT NULL AND e.Price IS NOT NULL AND mb.Chipset IS NOT NULL
      AND mb.IsBundle = 0 AND e.SoldDate > NOW() - INTERVAL {MARKET_STATS_DAYS} DAY
      AND COALESCE(e.Quantity, 1) = 1
),
MoboStats AS (SELECT DISTINCT Chipset, FormFactor, Med FROM MoboMed WHERE N >= {min_sold})
SELECT
    e.ID,
    c.Model, c.Socket, mb.Chipset, mb.Socket AS MoboSocket, mb.FormFactor,
    ROUND({EFF}, 2)                         AS CurrentPrice,
    ROUND(e.Price / 100, 2)                 AS ItemPrice,
    ROUND(COALESCE(e.Shipping, 0) / 100, 2) AS Shipping,
    1                                       AS Quantity,
    ROUND({EFF}, 2)                         AS PerUnitPrice,
    ROUND(cs.Med + ms.Med, 2)               AS AvgMarketPrice,
    ROUND((cs.Med + ms.Med) - {EFF}, 2)     AS PotentialGain,
    ROUND((1 - {EFF} / (cs.Med + ms.Med)) * 100, 1) AS DiscountPct,
    ROUND(((1 - {EFF} / (cs.Med + ms.Med)) * 100)
        / GREATEST(TIMESTAMPDIFF(MINUTE, NOW(), e.EndTime) / 60.0, 0.25)
        / (1 + COALESCE(e.Bids, 0)), 2)     AS DealScore,
    e.Bids,
    COALESCE(e.ListingType, 'auction')      AS ListingType,
    e.SellerFeedbackPct,
    e.SellerFeedbackCount,
    e.EndTime,
    e.URL
FROM Scraper.EBAY e
JOIN Scraper.CPU  c  ON c.ID  = e.ID AND c.IsBundle  = 1
JOIN Scraper.MOBO mb ON mb.ID = e.ID AND mb.IsBundle = 1
JOIN CpuStats  cs ON cs.Model = c.Model
JOIN MoboStats ms ON ms.Chipset = mb.Chipset AND ms.FormFactor <=> mb.FormFactor
WHERE e.SoldDate IS NULL
  AND {EFF} < (cs.Med + ms.Med) * {threshold}
  AND {FEEDBACK_OK}
  AND {FRESH_OK}
  AND COALESCE(e.ReserveNotMet, 0) = 0
ORDER BY DiscountPct DESC;
"""


def group_median_query(product_type: str, params: dict) -> tuple[str, list]:
    """(sql, binds) for one market group's current sold median.

    Same basis as the market stats (recency window, single units only) but
    scoped to a single group — feeds median_below price alerts, where the
    question is "what is THIS group's median right now", not the full guide.
    Returns one row (MedPrice, N) or none when the group has no recent sales.
    """
    cfg = CATEGORIES[product_type]
    a = cfg['alias']
    cond, values = model_where(product_type, params)
    sql = f"""
SELECT DISTINCT MEDIAN({EFF}) OVER () AS MedPrice, COUNT(*) OVER () AS N
FROM Scraper.{cfg['table']} {a}
JOIN Scraper.EBAY e ON e.ID = {a}.ID
WHERE e.SoldDate IS NOT NULL AND e.Price IS NOT NULL
  AND e.SoldDate > NOW() - INTERVAL {MARKET_STATS_DAYS} DAY
  AND COALESCE(e.Quantity, 1) = 1
  AND {cond}
"""
    return sql, values


# listing_below alerts only consider an auction's price MEANINGFUL in its
# final stretch: a 99p-start auction with days left is always "below target"
# and would ping on every fresh listing while telling you nothing. Two hours
# matches the window the snipe premiums are trained on, so the predicted
# final used to confirm the hit is calibrated.
ALERT_AUCTION_WINDOW_HOURS = int(os.environ.get('ALERT_AUCTION_WINDOW_HOURS', '2'))


def group_live_below_query(product_type: str, params: dict, max_price: float) -> tuple[str, list]:
    """(sql, binds) for listings GENUINELY available under a price cap.

    Feeds listing_below price alerts: a fresh live listing whose
    delivery-inclusive PER-UNIT price is below the user's target AND whose
    price means something — Buy-It-Now at any time, or an auction inside its
    final ALERT_AUCTION_WINDOW_HOURS (the caller then confirms the PREDICTED
    final also clears the target). Same trust gates as the deal feed
    (freshness, seller feedback, reserve) so an alert never fires on a
    phantom or scam listing. Cheapest first, capped — the alert names the
    best hit, it doesn't enumerate the market.
    """
    cfg = CATEGORIES[product_type]
    a = cfg['alias']
    cond, values = model_where(product_type, params)
    sql = f"""
SELECT e.ID, e.Title, ROUND({EFF_UNIT}, 2) AS PerUnitPrice, {QTY} AS Quantity,
       ROUND({EFF}, 2) AS CurrentPrice, e.Bids, e.EndTime, e.URL,
       COALESCE(e.ListingType, 'auction') AS ListingType
FROM Scraper.{cfg['table']} {a}
JOIN Scraper.EBAY e ON e.ID = {a}.ID
WHERE e.SoldDate IS NULL
  AND {FRESH_OK}
  AND {FEEDBACK_OK}
  AND COALESCE(e.ReserveNotMet, 0) = 0
  AND (COALESCE(e.ListingType, 'auction') = 'bin'
       OR (e.EndTime > NOW()
           AND e.EndTime < NOW() + INTERVAL {ALERT_AUCTION_WINDOW_HOURS} HOUR))
  AND {cond}
  AND {EFF_UNIT} < %s
ORDER BY PerUnitPrice ASC
LIMIT 3
"""
    return sql, values + [max_price]


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
  AND e.SoldDate IS NULL AND COALESCE(e.ListingType, 'auction') = 'auction'
  AND {EFF_UNIT} < rs.MedPrice * {threshold}
  AND {FEEDBACK_OK}
  AND {FRESH_OK}
  AND COALESCE(e.ReserveNotMet, 0) = 0
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


def time_bucket(hours) -> str:
    """Bucket time-to-end for premium stats. An auction keeps accruing bids
    until it closes, so how much time is left when we see it drives how far it
    rises: a 4-bid item with 5 min left is nearly settled; the same with 2h to
    go keeps climbing. Buckets match the ≤2h surfacing window the ratios are
    trained on. None (unknown) → 'any' (time-agnostic)."""
    if hours is None:
        return 'any'
    if hours < 0.25:
        return '<15m'
    if hours < 1:
        return '15-60m'
    return '60m+'


# Categories whose auctions close near their spotted price — the snipe premium
# (which, via the category fallback, inherits contested GPU/HDD dynamics) only
# adds error there. SSD measured worse than the no-model baseline (19.6% vs
# 14.5%) with an 8% over-prediction bias, so it gets no premium.
_NO_PREMIUM_CATEGORIES = {'SSD'}


def median_ratios(rows, min_samples: int = 5) -> dict:
    """rows: (category, bid_count, hours_to_end, surfaced_price, final_price).

    Returns {key: (median_final_over_surfaced, sample_count)} at three
    specificities, so a lookup can degrade gracefully:
      (cat, bid_bucket, time_bucket) — most specific
      (cat, bid_bucket, 'any')       — bid bucket, any time-to-end
      (cat, 'all')                   — category catch-all
    Groups below min_samples are dropped — too little history to trust.
    """
    import statistics
    groups = {}
    for cat, bids, hours, surfaced, final in rows:
        if not surfaced or final is None:
            continue
        ratio = float(final) / float(surfaced)
        bb, tb = bid_bucket(bids), time_bucket(hours)
        groups.setdefault((cat, bb, 'any'), []).append(ratio)
        if tb != 'any':      # avoid double-counting when time-to-end is unknown
            groups.setdefault((cat, bb, tb), []).append(ratio)
        groups.setdefault((cat, 'all'), []).append(ratio)
    return {
        key: (round(statistics.median(vals), 3), len(vals))
        for key, vals in groups.items()
        if len(vals) >= min_samples
    }


def premium_for(premiums, category, bids, hours_to_end=None):
    """(ratio, samples) for a live listing — the single lookup every prediction
    surface funnels through. Precedence: exact (cat, bid, time) →
    (cat, bid, any) → category 'all'. Two guards:
      • 0-bid listings NEVER inherit the contested 'all' premium (it wildly
        over-predicts a calm listing that isn't being sniped);
      • no-premium categories (SSD) always return 1.0.
    A missing history gives (1.0, 0): the prediction equals the current price."""
    cat = (category or '').upper()
    if cat in _NO_PREMIUM_CATEGORIES:
        return (1.0, 0)
    bb = bid_bucket(bids)
    for key in ((cat, bb, time_bucket(hours_to_end)), (cat, bb, 'any')):
        if key in premiums:
            return premiums[key]
    if bb == '0':
        return (1.0, 0)
    return premiums.get((cat, 'all'), (1.0, 0))


# ── probabilistic surfacing ─────────────────────────────────────────────────
# premium_for gives a POINT prediction (current × median ratio). That ignores
# how noisy a cohort is: a deal predicted to land exactly at the margin is
# flagged whether the cohort's outcomes are tight or scattered. The functions
# below keep the full realized ratio distribution per cohort so a surfacing
# rule can ask "what's the probability this closes at/under the target?" — which
# folds in both the model's bias (these are real outcomes) and its spread.

def ratio_distributions(rows, min_samples: int = 5) -> dict:
    """Like median_ratios, but returns the full sorted final/surfaced ratio
    list per cohort (same three specificities and min_samples guard), so a
    caller can compute empirical P(final <= threshold)."""
    groups = {}
    for cat, bids, hours, surfaced, final in rows:
        if not surfaced or final is None:
            continue
        ratio = float(final) / float(surfaced)
        bb, tb = bid_bucket(bids), time_bucket(hours)
        groups.setdefault((cat, bb, 'any'), []).append(ratio)
        if tb != 'any':
            groups.setdefault((cat, bb, tb), []).append(ratio)
        groups.setdefault((cat, 'all'), []).append(ratio)
    return {k: sorted(v) for k, v in groups.items() if len(v) >= min_samples}


def _distribution_for(dists, category, bids, hours_to_end):
    """Best-matching ratio list for a live listing, using the exact precedence
    (and 0-bid / no-premium guards) as premium_for. None when unusable."""
    cat = (category or '').upper()
    if cat in _NO_PREMIUM_CATEGORIES:
        return None
    bb = bid_bucket(bids)
    for key in ((cat, bb, time_bucket(hours_to_end)), (cat, bb, 'any')):
        if key in dists:
            return dists[key]
    if bb == '0':
        return None
    return dists.get((cat, 'all'))


def wilson_lower_bound(k: int, n: int, z: float = 1.0) -> float:
    """Lower bound of a binomial proportion (Wilson score interval). With few
    samples this pulls the naive k/n estimate down, so a thin or noisy cohort
    must show a stronger empirical signal to clear a confidence bar. z=1.0 is a
    ~84% one-sided bound; larger z = more conservative."""
    if n <= 0:
        return 0.0
    phat = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = phat + z2 / (2 * n)
    half = z * math.sqrt((phat * (1.0 - phat) + z2 / (4 * n)) / n)
    return max(0.0, (centre - half) / denom)


def prob_below(dists, category, bids, hours_to_end, threshold_ratio, z: float = 1.0):
    """Conservative empirical probability that final/surfaced <= threshold_ratio
    for this listing's cohort — i.e. that it closes at/under the target price.

    Uses the realized ratio distribution (embodying the model's bias AND its
    spread), then a Wilson lower bound so thin cohorts stay honest. Returns
    (prob_lower_bound, samples); (None, 0) when there is no usable cohort."""
    dist = _distribution_for(dists, category, bids, hours_to_end)
    if not dist:
        return (None, 0)
    n = len(dist)
    k = sum(1 for r in dist if r <= threshold_ratio)
    return (wilson_lower_bound(k, n, z), n)


# Resolved outcomes that feed the premium model (sold, not ended-unsold).
# The near-miss control cohort is excluded: premiums must stay trained on
# the same population they predict for, or the experiment contaminates it.
# HoursToEnd = time-to-close when the deal was first surfaced (drives how far
# it rises before the hammer).
SNIPE_PREMIUM_QUERY = """
SELECT d.Category, d.BidCount,
       TIMESTAMPDIFF(SECOND, d.SurfacedAt, COALESCE(e.EndTime, d.EndTime)) / 3600.0
           AS HoursToEnd,
       d.SurfacedPrice,
       COALESCE(d.FinalPrice, e.Price)
FROM Scraper.DealOutcomes d
JOIN Scraper.EBAY e ON e.ID = d.EbayID
WHERE e.SoldDate IS NOT NULL
  AND d.EndedUnsold = 0
  AND d.NearMiss = 0
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
        end = row.get('EndTime')
        hours_left = None
        if end is not None and hasattr(end, 'timestamp'):
            hours_left = max((end - now).total_seconds() / 3600.0, 0.0)
        ratio, samples = premium_for(premiums, cat, bids, hours_left)
        price = float(row.get('CurrentPrice') or 0)
        qty = int(row.get('Quantity') or 1)
        market_lot = float(row.get('AvgMarketPrice') or 0) * qty
        predicted = round(price * ratio, 2)
        row['PredictedFinalPrice'] = predicted
        row['PremiumSamples'] = samples
        row['PredictedDiscountPct'] = (
            round((1 - predicted / market_lot) * 100, 1) if market_lot > 0 else None)
        hours = max(hours_left, 0.25) if hours_left is not None else 0.25
        row['DealScore'] = round(
            max(row['PredictedDiscountPct'] or 0, 0) / hours / (1 + bids), 2)
    rows.sort(key=lambda r: r.get('DealScore') or 0, reverse=True)
    return rows


def model_where(product_type: str, params: dict) -> tuple[str, list]:
    """WHERE fragment + bind values selecting one market group.

    `params` carries the group-column values as strings (from a query
    string); None/absent/'' matches NULL via the null-safe operator.
    Returns (sql_condition_on_alias, values) — alias is cfg['alias'].
    """
    cfg = CATEGORIES[product_type]
    a = cfg['alias']
    conds, values = [], []
    for col, _null_safe in cfg['group_cols']:
        raw = params.get(col)
        if raw in (None, ''):
            conds.append(f"{a}.{col} IS NULL")
        else:
            conds.append(f"{a}.{col} <=> %s")
            values.append(raw)
    return ' AND '.join(conds), values


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


# ── /bin context filters (shared by the browse page + BIN-watch alerts) ──
# The canonical Python mirror of CTX_FILTERS in common.js. A "BIN watch" alert
# stores one of these filter sets; the scraper matches new BIN finds against it
# to decide who to notify. Keep the two in sync when adding a filter.

def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_CTX_FILTER_MATCHERS = {
    'gpu': {
        'series': lambda r, v: str(r.get('Model') or '').upper().startswith(str(v).upper()),
    },
    'cpu': {
        'family': lambda r, v: str(v).lower() in str(r.get('Model') or '').lower(),
        'socket': lambda r, v: (r.get('Socket') or '') == v,
    },
    'mobo': {
        'socket': lambda r, v: (r.get('Socket') or '') == v,
        'chipset': lambda r, v: (r.get('Chipset') or '') == v,
        'ff': lambda r, v: (r.get('FormFactor') or 'ATX') == v,
    },
    'hdd': {
        'iface': lambda r, v: (r.get('Interface') or 'SATA') == v,
        'type': lambda r, v: (r.get('DriveType') or 'Internal') == v,
        'mincap': lambda r, v: (_num(r.get('CapacityGB')) or 0) >= (_num(v) or 0),
    },
    'ssd': {
        'iface': lambda r, v: (r.get('Interface') or '') == v,
        'mincap': lambda r, v: (_num(r.get('CapacityGB')) or 0) >= (_num(v) or 0),
    },
    'ram': {
        'type': lambda r, v: (r.get('Type') or '') == v,
        'ff': lambda r, v: (r.get('FormFactor') or 'DIMM') == v,
        'kit': lambda r, v: (not r.get('KitConfig')) if v == '?'
        else str(r.get('KitConfig') or '').lower().startswith(str(v).lower()),
        'mincap': lambda r, v: (_num(r.get('CapacityGB')) or 0) >= (_num(v) or 0),
    },
}


def ctx_filter_match(product_type: str, filters: dict, row: dict) -> bool:
    """Does a BIN find (row with the category's attribute columns) satisfy a
    saved /bin filter set? Empty/absent filter values are 'any'."""
    matchers = _CTX_FILTER_MATCHERS.get((product_type or '').lower(), {})
    for key, val in (filters or {}).items():
        if val in (None, ''):
            continue
        fn = matchers.get(key)
        if fn and not fn(row, val):
            return False
    return True


def _norm_group_val(v):
    """Normalise a group-column value for equality: numbers compare numerically,
    everything else case-insensitively as text. None/'' collapse to ''."""
    if v in (None, ''):
        return ''
    n = _num(v)
    if n is not None:
        return n
    return str(v).strip().lower()


def group_match(product_type: str, params: dict, row: dict) -> bool:
    """Does a surfaced row belong to the EXACT market group a subscription names?
    Compares the category's group columns; an unspecified column is a wildcard,
    so a partial group (e.g. just Interface) matches every capacity within it."""
    cfg = CATEGORIES.get((product_type or '').lower())
    if not cfg:
        return False
    for col, _ in cfg['group_cols']:
        want = (params or {}).get(col)
        if want in (None, ''):
            continue
        if _norm_group_val(row.get(col)) != _norm_group_val(want):
            return False
    return True


def subscription_scope_match(scope_kind: str, product_type: str,
                             params: dict, row: dict) -> bool:
    """Unified scope test for a subscription against a live/surfaced row (the
    category is assumed already matched by the caller):
      all    — the whole category (params ignored)
      filter — the shared /bin context filters (ctx_filter_match)
      group  — one exact market group from a model page (group_match)"""
    if scope_kind == 'all':
        return True
    if scope_kind == 'group':
        return group_match(product_type, params, row)
    return ctx_filter_match(product_type, params, row)   # 'filter' (default)


def subscription_label(product_type: str, scope_kind: str, params: dict) -> str:
    """Human label for a subscription's scope, for the Settings list."""
    if scope_kind == 'all':
        return f"{product_type.upper()} (all)"
    if scope_kind == 'group':
        p = dict(params or {})
        if 'CapacityGB' in p:      # arrives as a string from a URL query
            p['CapacityGB'] = _num(p['CapacityGB'])
        return _base_label(product_type, p) or product_type.upper()
    return bin_watch_label(product_type, params)   # 'filter'


def bin_watch_label(product_type: str, filters: dict) -> str:
    """Short human summary of a filter-scope subscription for the Settings list."""
    parts = [product_type.upper()]
    labels = {
        'series': lambda v: v, 'family': lambda v: v,
        'iface': lambda v: v, 'type': lambda v: v,
        'ff': lambda v: v, 'type_ram': lambda v: v,
        'kit': lambda v: 'single' if v == '1x' else 'unstated' if v == '?' else f'{v} kit',
        'mincap': lambda v: (f"{int(_num(v) / 1000)}TB+" if (_num(v) or 0) >= 1000
                             else f"{int(_num(v) or 0)}GB+"),
    }
    for k, v in (filters or {}).items():
        if v in (None, ''):
            continue
        fn = labels.get(k)
        parts.append(fn(v) if fn else str(v))
    return ' · '.join(parts) if len(parts) > 1 else f"{product_type.upper()} (all)"


def model_label_for_row(product_type: str, row: dict) -> str:
    """Human label stored in DealOutcomes.Model for a surfaced deal row.
    Multi-unit lots get a ×N suffix (e.g. '4TB SAS ×5')."""
    label = _base_label(product_type, row)
    qty = int(row.get('Quantity') or 1)
    return f"{label} ×{qty}" if qty > 1 and label else label


def _base_label(product_type: str, row: dict) -> str:
    if product_type in ('hdd', 'ssd'):
        cap = row.get('CapacityGB')
        iface = row.get('Interface') or 'SATA'
        # 'SSD' suffix distinguishes "1TB SATA SSD" from the HDD "1TB SATA".
        kind = ' SSD' if product_type == 'ssd' else ''
        # Only annotate the non-default (External) — internal is the norm.
        ext = ' External' if row.get('DriveType') == 'External' else ''
        if cap and cap >= 1000:
            tb = cap / 1000
            size = f"{int(tb)}TB" if cap % 1000 == 0 else f"{tb:.1f}TB"
            return f"{size} {iface}{kind}{ext}"
        return f"{cap}GB {iface}{kind}{ext}" if cap else f"{iface}{kind}{ext}"
    if product_type == 'ram':
        cap = row.get('CapacityGB')
        ram_type = row.get('Type') or 'RAM'
        # Only annotate the non-default (SODIMM) — DIMM is the norm.
        so = ' SODIMM' if row.get('FormFactor') == 'SODIMM' else ''
        kit = f" ({row['KitConfig']})" if row.get('KitConfig') else ''
        return f"{cap}GB {ram_type}{so}{kit}" if cap else f"{ram_type}{so}{kit}"
    if product_type == 'mobo':
        chip = row.get('Chipset') or 'Motherboard'
        # Only annotate the non-default (full ATX) form factor.
        ff = row.get('FormFactor')
        return f"{chip} {ff}" if ff and ff != 'ATX' else chip
    return row.get('Model')
