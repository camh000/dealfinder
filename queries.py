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
        'guide_order': 'cs.AvgPrice DESC',
    },
    'cpu': {
        'table': 'CPU', 'alias': 'c',
        'group_cols': [('Model', False)],
        'not_null': ['Model'],
        'deal_select': ['c.Model', 'c.Brand', 'c.Socket', 'c.Cores'],
        'guide_select': ['rs.Model'],
        'guide_order': 'cs.AvgPrice DESC',
    },
    'hdd': {
        'table': 'HDD', 'alias': 'h',
        'group_cols': [('CapacityGB', False), ('Interface', True)],
        'not_null': ['CapacityGB'],
        'deal_select': ['h.Brand', 'h.CapacityGB', 'h.Interface', 'h.FormFactor', 'h.RPM'],
        'guide_select': ['rs.CapacityGB', 'rs.Interface'],
        'guide_order': 'rs.CapacityGB DESC, cs.AvgPrice DESC',
    },
    'ram': {
        'table': 'RAM', 'alias': 'r',
        'group_cols': [('Type', False), ('CapacityGB', False)],
        'not_null': ['Type', 'CapacityGB'],
        'deal_select': ['r.Brand', 'r.CapacityGB', 'r.Type', 'r.Speed'],
        'guide_select': ['rs.Type', 'rs.CapacityGB'],
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


def _stats_ctes(cfg, min_sold: int) -> str:
    """RawStats (mean/stdev per group) + ModelStats (sigma-trimmed stats)."""
    a = cfg['alias']
    group = ', '.join(f"{a}.{col}" for col, _ in cfg['group_cols'])
    group_bare = ', '.join(col for col, _ in cfg['group_cols'])
    not_null = ' AND '.join(f"{a}.{col} IS NOT NULL" for col in cfg['not_null'])
    return f"""
WITH RawStats AS (
    SELECT {group},
           AVG({EFF})    AS RawAvg,
           STDDEV({EFF}) AS StdDev
    FROM Scraper.{cfg['table']} {a}
    JOIN Scraper.EBAY e ON e.ID = {a}.ID
    WHERE e.SoldDate IS NOT NULL AND e.Price IS NOT NULL AND {not_null}
    GROUP BY {group}
    HAVING COUNT(*) >= {min_sold}
),
ModelStats AS (
    SELECT {group},
           ROUND(AVG({EFF}), 2) AS AvgPrice,
           ROUND(MIN({EFF}), 2) AS MinMarketPrice,
           ROUND(MAX({EFF}), 2) AS MaxMarketPrice
    FROM   Scraper.{cfg['table']} {a}
    JOIN   Scraper.EBAY e ON e.ID = {a}.ID
    JOIN   RawStats rs ON {_join_cond(cfg, 'rs', a)}
    WHERE  e.SoldDate IS NOT NULL AND e.Price IS NOT NULL AND {not_null}
      AND  {EFF} BETWEEN rs.RawAvg - 2 * rs.StdDev
                     AND rs.RawAvg + 2 * rs.StdDev
    GROUP  BY {group}
)""", group_bare


def build_deals_query(product_type: str, window_hours: int = 2, min_discount: float = 20) -> str:
    cfg = CATEGORIES[product_type]
    a = cfg['alias']
    interval = f"INTERVAL {_clamp_window(window_hours)} HOUR"
    threshold = _clamp_threshold(min_discount)
    ctes, _ = _stats_ctes(cfg, min_sold=5)
    extra = ',\n    '.join(cfg['deal_select'])
    return f"""{ctes}
SELECT
    e.ID,
    {extra},
    ROUND({EFF}, 2)                              AS CurrentPrice,
    ms.AvgPrice                                  AS AvgMarketPrice,
    ms.MinMarketPrice,
    ms.MaxMarketPrice,
    ROUND(ms.AvgPrice - {EFF}, 2)                AS PotentialGain,
    ROUND((1 - {EFF} / ms.AvgPrice) * 100, 1)    AS DiscountPct,
    e.Bids,
    e.EndTime,
    e.URL
FROM Scraper.EBAY e
JOIN Scraper.{cfg['table']} {a} ON {a}.ID = e.ID
JOIN ModelStats ms ON {_join_cond(cfg, 'ms', a)}
WHERE
    e.SoldDate IS NULL
    AND {EFF} < ms.AvgPrice * {threshold}
    AND e.EndTime > NOW()
    AND e.EndTime < NOW() + {interval}
ORDER BY PotentialGain DESC;
"""


def build_count_query(product_type: str, window_hours: int = 2, min_discount: float = 20) -> str:
    cfg = CATEGORIES[product_type]
    a = cfg['alias']
    interval = f"INTERVAL {_clamp_window(window_hours)} HOUR"
    threshold = _clamp_threshold(min_discount)
    group = ', '.join(f"{a}.{col}" for col, _ in cfg['group_cols'])
    not_null = ' AND '.join(f"{a}.{col} IS NOT NULL" for col in cfg['not_null'])
    return f"""
WITH ModelStats AS (
    SELECT {group}, AVG({EFF}) AS AvgPrice
    FROM Scraper.{cfg['table']} {a} JOIN Scraper.EBAY e ON e.ID = {a}.ID
    WHERE e.SoldDate IS NOT NULL AND e.Price IS NOT NULL AND {not_null}
    GROUP BY {group} HAVING COUNT(*) >= 5
)
SELECT COUNT(*) AS cnt
FROM Scraper.EBAY e
JOIN Scraper.{cfg['table']} {a} ON {a}.ID = e.ID
JOIN ModelStats ms ON {_join_cond(cfg, 'ms', a)}
WHERE e.SoldDate IS NULL AND {EFF} < ms.AvgPrice * {threshold}
  AND e.EndTime > NOW() AND e.EndTime < NOW() + {interval};
"""


def build_price_guide_query(product_type: str) -> str:
    cfg = CATEGORIES[product_type]
    a = cfg['alias']
    group = ', '.join(f"{a}.{col}" for col, _ in cfg['group_cols'])
    not_null = ' AND '.join(f"{a}.{col} IS NOT NULL" for col in cfg['not_null'])
    guide_cols = ',\n       '.join(cfg['guide_select'])
    return f"""
WITH RawStats AS (
    SELECT {group},
           AVG({EFF})    AS RawAvg,
           STDDEV({EFF}) AS StdDev,
           COUNT(*)      AS SoldCount
    FROM   Scraper.{cfg['table']} {a}
    JOIN   Scraper.EBAY e ON e.ID = {a}.ID
    WHERE  e.SoldDate IS NOT NULL AND e.Price IS NOT NULL AND {not_null}
    GROUP  BY {group}
    HAVING COUNT(*) >= 3
),
CleanStats AS (
    SELECT {group},
           ROUND(AVG({EFF}), 2) AS AvgPrice,
           ROUND(MIN({EFF}), 2) AS MinPrice,
           ROUND(MAX({EFF}), 2) AS MaxPrice
    FROM   Scraper.{cfg['table']} {a}
    JOIN   Scraper.EBAY e ON e.ID = {a}.ID
    JOIN   RawStats rs ON {_join_cond(cfg, 'rs', a)}
    WHERE  e.SoldDate IS NOT NULL AND e.Price IS NOT NULL AND {not_null}
      AND  {EFF} BETWEEN rs.RawAvg - 2 * rs.StdDev
                     AND rs.RawAvg + 2 * rs.StdDev
    GROUP  BY {group}
)
SELECT {guide_cols},
       cs.AvgPrice,
       cs.MinPrice,
       cs.MaxPrice,
       rs.SoldCount
FROM   RawStats rs
JOIN   CleanStats cs ON {_join_cond(cfg, 'cs', 'rs')}
ORDER  BY {cfg['guide_order']};
"""


def model_label_for_row(product_type: str, row: dict) -> str:
    """Human label stored in DealOutcomes.Model for a surfaced deal row."""
    if product_type == 'hdd':
        cap = row.get('CapacityGB')
        iface = row.get('Interface') or 'SATA'
        if cap and cap >= 1000:
            tb = cap / 1000
            return f"{int(tb)}TB {iface}" if cap % 1000 == 0 else f"{tb:.1f}TB {iface}"
        return f"{cap}GB {iface}" if cap else iface
    if product_type == 'ram':
        cap = row.get('CapacityGB')
        ram_type = row.get('Type') or 'RAM'
        return f"{cap}GB {ram_type}" if cap else ram_type
    return row.get('Model')
