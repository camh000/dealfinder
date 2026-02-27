from flask import Flask, jsonify, render_template, request
import mariadb
import os
import logging
from dotenv import load_dotenv

load_dotenv("credentials.env")

log = logging.getLogger(__name__)

app = Flask(__name__)

def get_connection():
    return mariadb.connect(
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", 3305)),
        database=os.environ["DB_NAME"]
    )

GPU_DEALS_QUERY = """
WITH ModelStats AS (
    SELECT
        g.Model,
        AVG(e.Price / 100) AS AvgPrice
    FROM Scraper.GPU g
    JOIN Scraper.EBAY e ON e.ID = g.ID
    WHERE
        e.SoldDate IS NOT NULL
        AND g.Model IS NOT NULL
    GROUP BY g.Model
    HAVING COUNT(*) >= 5
)
SELECT
    e.ID,
    g.Model,
    g.Brand,
    g.VRAM,
    ROUND(e.Price / 100, 2) AS CurrentPrice,
    ROUND(ms.AvgPrice, 2) AS AvgMarketPrice,
    ROUND(ms.AvgPrice - (e.Price / 100), 2) AS PotentialGain,
    ROUND((1 - (e.Price / 100) / ms.AvgPrice) * 100, 1) AS DiscountPct,
    e.Bids,
    e.EndTime,
    e.URL
FROM Scraper.EBAY e
JOIN Scraper.GPU g ON g.ID = e.ID
JOIN ModelStats ms ON ms.Model = g.Model
WHERE
    e.SoldDate IS NULL
    AND (e.Price / 100) < ms.AvgPrice * 0.8
    AND e.EndTime > NOW()
    AND e.EndTime < NOW() + INTERVAL 2 HOUR
ORDER BY PotentialGain DESC;
"""

CPU_DEALS_QUERY = """
WITH ModelStats AS (
    SELECT
        c.Model,
        AVG(e.Price / 100) AS AvgPrice
    FROM Scraper.CPU c
    JOIN Scraper.EBAY e ON e.ID = c.ID
    WHERE
        e.SoldDate IS NOT NULL
        AND c.Model IS NOT NULL
    GROUP BY c.Model
    HAVING COUNT(*) >= 5
)
SELECT
    e.ID,
    c.Model,
    c.Brand,
    c.Socket,
    c.Cores,
    ROUND(e.Price / 100, 2) AS CurrentPrice,
    ROUND(ms.AvgPrice, 2) AS AvgMarketPrice,
    ROUND(ms.AvgPrice - (e.Price / 100), 2) AS PotentialGain,
    ROUND((1 - (e.Price / 100) / ms.AvgPrice) * 100, 1) AS DiscountPct,
    e.Bids,
    e.EndTime,
    e.URL
FROM Scraper.EBAY e
JOIN Scraper.CPU c ON c.ID = e.ID
JOIN ModelStats ms ON ms.Model = c.Model
WHERE
    e.SoldDate IS NULL
    AND (e.Price / 100) < ms.AvgPrice * 0.8
    AND e.EndTime > NOW()
    AND e.EndTime < NOW() + INTERVAL 2 HOUR
ORDER BY PotentialGain DESC;
"""

HDD_DEALS_QUERY = """
WITH ModelStats AS (
    SELECT
        h.CapacityGB,
        h.Interface,
        AVG(e.Price / 100) AS AvgPrice
    FROM Scraper.HDD h
    JOIN Scraper.EBAY e ON e.ID = h.ID
    WHERE
        e.SoldDate IS NOT NULL
        AND h.CapacityGB IS NOT NULL
    GROUP BY h.CapacityGB, h.Interface
    HAVING COUNT(*) >= 5
)
SELECT
    e.ID,
    h.Brand,
    h.CapacityGB,
    h.Interface,
    h.FormFactor,
    h.RPM,
    ROUND(e.Price / 100, 2) AS CurrentPrice,
    ROUND(ms.AvgPrice, 2) AS AvgMarketPrice,
    ROUND(ms.AvgPrice - (e.Price / 100), 2) AS PotentialGain,
    ROUND((1 - (e.Price / 100) / ms.AvgPrice) * 100, 1) AS DiscountPct,
    e.Bids,
    e.EndTime,
    e.URL
FROM Scraper.EBAY e
JOIN Scraper.HDD h ON h.ID = e.ID
JOIN ModelStats ms ON ms.CapacityGB = h.CapacityGB AND ms.Interface = h.Interface
WHERE
    e.SoldDate IS NULL
    AND (e.Price / 100) < ms.AvgPrice * 0.8
    AND e.EndTime > NOW()
    AND e.EndTime < NOW() + INTERVAL 2 HOUR
ORDER BY PotentialGain DESC;
"""

DEALS_QUERIES = {
    'gpu': GPU_DEALS_QUERY,
    'cpu': CPU_DEALS_QUERY,
    'hdd': HDD_DEALS_QUERY,
}

GPU_COUNT_QUERY = """
WITH ModelStats AS (
    SELECT g.Model, AVG(e.Price / 100) AS AvgPrice
    FROM Scraper.GPU g JOIN Scraper.EBAY e ON e.ID = g.ID
    WHERE e.SoldDate IS NOT NULL AND g.Model IS NOT NULL
    GROUP BY g.Model HAVING COUNT(*) >= 5
)
SELECT COUNT(*) AS cnt
FROM Scraper.EBAY e
JOIN Scraper.GPU g ON g.ID = e.ID
JOIN ModelStats ms ON ms.Model = g.Model
WHERE e.SoldDate IS NULL AND (e.Price / 100) < ms.AvgPrice * 0.8
  AND e.EndTime > NOW() AND e.EndTime < NOW() + INTERVAL 2 HOUR;
"""

CPU_COUNT_QUERY = """
WITH ModelStats AS (
    SELECT c.Model, AVG(e.Price / 100) AS AvgPrice
    FROM Scraper.CPU c JOIN Scraper.EBAY e ON e.ID = c.ID
    WHERE e.SoldDate IS NOT NULL AND c.Model IS NOT NULL
    GROUP BY c.Model HAVING COUNT(*) >= 5
)
SELECT COUNT(*) AS cnt
FROM Scraper.EBAY e
JOIN Scraper.CPU c ON c.ID = e.ID
JOIN ModelStats ms ON ms.Model = c.Model
WHERE e.SoldDate IS NULL AND (e.Price / 100) < ms.AvgPrice * 0.8
  AND e.EndTime > NOW() AND e.EndTime < NOW() + INTERVAL 2 HOUR;
"""

HDD_COUNT_QUERY = """
WITH ModelStats AS (
    SELECT h.CapacityGB, h.Interface, AVG(e.Price / 100) AS AvgPrice
    FROM Scraper.HDD h JOIN Scraper.EBAY e ON e.ID = h.ID
    WHERE e.SoldDate IS NOT NULL AND h.CapacityGB IS NOT NULL
    GROUP BY h.CapacityGB, h.Interface HAVING COUNT(*) >= 5
)
SELECT COUNT(*) AS cnt
FROM Scraper.EBAY e
JOIN Scraper.HDD h ON h.ID = e.ID
JOIN ModelStats ms ON ms.CapacityGB = h.CapacityGB AND ms.Interface = h.Interface
WHERE e.SoldDate IS NULL AND (e.Price / 100) < ms.AvgPrice * 0.8
  AND e.EndTime > NOW() AND e.EndTime < NOW() + INTERVAL 2 HOUR;
"""

PRICE_GUIDE_GPU_QUERY = """
SELECT g.Model, g.Brand, g.VRAM,
       ROUND(AVG(e.Price / 100), 2) AS AvgPrice,
       COUNT(*) AS SoldCount
FROM   Scraper.GPU g
JOIN   Scraper.EBAY e ON e.ID = g.ID
WHERE  e.SoldDate IS NOT NULL
  AND  g.Model IS NOT NULL
  AND  e.Price IS NOT NULL
GROUP  BY g.Model, g.Brand, g.VRAM
HAVING COUNT(*) >= 3
ORDER  BY AvgPrice DESC;
"""

PRICE_GUIDE_CPU_QUERY = """
SELECT c.Model, c.Brand, c.Socket, c.Cores,
       ROUND(AVG(e.Price / 100), 2) AS AvgPrice,
       COUNT(*) AS SoldCount
FROM   Scraper.CPU c
JOIN   Scraper.EBAY e ON e.ID = c.ID
WHERE  e.SoldDate IS NOT NULL
  AND  c.Model IS NOT NULL
  AND  e.Price IS NOT NULL
GROUP  BY c.Model, c.Brand, c.Socket, c.Cores
HAVING COUNT(*) >= 3
ORDER  BY AvgPrice DESC;
"""

PRICE_GUIDE_HDD_QUERY = """
SELECT h.CapacityGB, h.Interface, h.FormFactor, h.Brand,
       ROUND(AVG(e.Price / 100), 2) AS AvgPrice,
       COUNT(*) AS SoldCount
FROM   Scraper.HDD h
JOIN   Scraper.EBAY e ON e.ID = h.ID
WHERE  e.SoldDate IS NOT NULL
  AND  h.CapacityGB IS NOT NULL
  AND  e.Price IS NOT NULL
GROUP  BY h.CapacityGB, h.Interface, h.FormFactor, h.Brand
HAVING COUNT(*) >= 3
ORDER  BY h.CapacityGB DESC, AvgPrice DESC;
"""

COUNT_QUERIES = {
    'gpu': GPU_COUNT_QUERY,
    'cpu': CPU_COUNT_QUERY,
    'hdd': HDD_COUNT_QUERY,
}

OUTCOMES_RESOLVED_QUERY = """
SELECT
    d.EbayID,
    d.Category,
    d.Model,
    ROUND(d.SurfacedPrice / 100, 2)  AS SurfacedPrice,
    ROUND(d.AvgMarketPrice / 100, 2) AS AvgMarketPrice,
    d.DiscountPct                    AS SurfacedDiscountPct,
    d.BidCount                       AS BidCountAtSurfacing,
    d.EndTime,
    d.SurfacedAt,
    ROUND(e.Price / 100, 2)          AS FinalPrice,
    e.SoldDate,
    ROUND((1 - (e.Price / 100) / (d.AvgMarketPrice / 100)) * 100, 1) AS ActualDiscountPct,
    d.EndedUnsold,
    e.URL
FROM Scraper.DealOutcomes d
JOIN Scraper.EBAY e ON e.ID = d.EbayID
WHERE e.SoldDate IS NOT NULL
ORDER BY d.SurfacedAt DESC
LIMIT 200;
"""

OUTCOMES_PENDING_QUERY = """
SELECT
    d.EbayID,
    d.Category,
    d.Model,
    ROUND(d.SurfacedPrice / 100, 2)  AS SurfacedPrice,
    ROUND(d.AvgMarketPrice / 100, 2) AS AvgMarketPrice,
    d.DiscountPct                    AS SurfacedDiscountPct,
    d.EndTime,
    d.SurfacedAt,
    ROUND(e.Price / 100, 2)          AS CurrentPrice,
    e.Bids                           AS CurrentBids,
    d.GaveUp,
    e.URL
FROM Scraper.DealOutcomes d
JOIN Scraper.EBAY e ON e.ID = d.EbayID
WHERE e.SoldDate IS NULL
ORDER BY d.EndTime ASC;
"""


def ensure_outcomes_table():
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS Scraper.DealOutcomes (
                EbayID         BIGINT       PRIMARY KEY,
                Category       VARCHAR(10)  NOT NULL,
                Model          VARCHAR(150),
                SurfacedPrice  INT          NOT NULL,
                AvgMarketPrice INT          NOT NULL,
                DiscountPct    FLOAT        NOT NULL,
                BidCount       INT          NOT NULL DEFAULT 0,
                EndTime        DATETIME     NOT NULL,
                SurfacedAt     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                GaveUp         TINYINT(1)   NOT NULL DEFAULT 0,
                EndedUnsold    TINYINT(1)   NOT NULL DEFAULT 0
            )
        """)
        conn.commit()
        # Auto-migrate existing installations that predate optional columns.
        for col_sql in [
            "ALTER TABLE Scraper.DealOutcomes ADD COLUMN GaveUp TINYINT(1) NOT NULL DEFAULT 0",
            "ALTER TABLE Scraper.DealOutcomes ADD COLUMN EndedUnsold TINYINT(1) NOT NULL DEFAULT 0",
        ]:
            try:
                cur.execute(col_sql)
                conn.commit()
                col_name = col_sql.split("ADD COLUMN ")[1].split()[0]
                log.info("DealOutcomes: added %s column", col_name)
            except Exception:
                pass  # column already exists (MySQL error 1060) — safe to ignore
        log.info("DealOutcomes table ready")
    except Exception as e:
        log.error("Could not create DealOutcomes table: %s", e)
    finally:
        if conn:
            conn.close()


ensure_outcomes_table()


@app.route("/")
def index():
    return render_template("Index.html")


@app.route("/api/deals")
def deals():
    product_type = request.args.get('type', 'gpu').lower()
    if product_type not in DEALS_QUERIES:
        return jsonify({"status": "error", "message": f"Unknown type '{product_type}'. Use gpu, cpu, or hdd."}), 400
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute(DEALS_QUERIES[product_type])
        rows = cur.fetchall()

        # Record newly surfaced deals (INSERT IGNORE = only capture first sighting)
        if rows:
            try:
                for row in rows:
                    if product_type == 'hdd':
                        cap = row.get('CapacityGB')
                        iface = row.get('Interface') or 'SATA'
                        if cap and cap >= 1000:
                            model_label = f"{cap // 1000}TB {iface}"
                        elif cap:
                            model_label = f"{cap}GB {iface}"
                        else:
                            model_label = iface
                    else:
                        model_label = row.get('Model')
                    cur.execute("""
                        INSERT IGNORE INTO Scraper.DealOutcomes
                            (EbayID, Category, Model, SurfacedPrice, AvgMarketPrice, DiscountPct, BidCount, EndTime)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        row['ID'],
                        product_type.upper(),
                        model_label,
                        int(round(row['CurrentPrice'] * 100)),
                        int(round(row['AvgMarketPrice'] * 100)),
                        float(row['DiscountPct']),
                        int(row.get('Bids') or 0),
                        row['EndTime'],
                    ))
                conn.commit()
            except Exception as e:
                log.warning("Could not record surfaced deals: %s", e)

        for row in rows:
            if row.get("EndTime"):
                row["EndTime"] = row["EndTime"].isoformat()

        return jsonify({"status": "ok", "deals": rows})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/deal-counts")
def deal_counts():
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        counts = {}
        for key, query in COUNT_QUERIES.items():
            cur.execute(query)
            counts[key] = cur.fetchone()['cnt']
        return jsonify({"status": "ok", "counts": counts})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/stats")
def stats():
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)

        cur.execute("SELECT COUNT(*) AS total FROM Scraper.EBAY WHERE SoldDate IS NULL")
        active = cur.fetchone()["total"]

        cur.execute("SELECT COUNT(*) AS total FROM Scraper.EBAY WHERE SoldDate IS NOT NULL")
        sold = cur.fetchone()["total"]

        cur.execute("SELECT MAX(SoldDate) AS last_update FROM Scraper.EBAY")
        last = cur.fetchone()["last_update"]

        return jsonify({
            "active_listings": active,
            "sold_listings": sold,
            "last_updated": str(last) if last else "Never"
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/outcomes")
def outcomes():
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)

        cur.execute(OUTCOMES_RESOLVED_QUERY)
        resolved = cur.fetchall()

        cur.execute(OUTCOMES_PENDING_QUERY)
        pending = cur.fetchall()

        for row in resolved:
            for col in ('EndTime', 'SoldDate', 'SurfacedAt'):
                if row.get(col):
                    row[col] = row[col].isoformat()

        for row in pending:
            for col in ('EndTime', 'SurfacedAt'):
                if row.get(col):
                    row[col] = row[col].isoformat()

        beat_market = sum(1 for r in resolved if r['FinalPrice'] is not None
                          and r['FinalPrice'] < r['AvgMarketPrice'])
        total_resolved = len(resolved)
        win_rate = round(beat_market / total_resolved * 100, 1) if total_resolved > 0 else 0

        return jsonify({
            "status": "ok",
            "summary": {
                "total_resolved": total_resolved,
                "beat_market": beat_market,
                "win_rate": win_rate,
                "total_pending": len(pending),
            },
            "resolved": resolved,
            "pending": pending,
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/price-guide")
def price_guide():
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        result = {}
        for cat, query in [('gpu', PRICE_GUIDE_GPU_QUERY),
                            ('cpu', PRICE_GUIDE_CPU_QUERY),
                            ('hdd', PRICE_GUIDE_HDD_QUERY)]:
            cur.execute(query)
            result[cat] = cur.fetchall()
        return jsonify({"status": "ok", "components": result})
    except Exception as e:
        log.error("price_guide error: %s", e)
        return jsonify({"status": "error", "message": "internal error"}), 500
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
