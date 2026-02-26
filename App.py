from flask import Flask, jsonify, render_template, request
import mariadb
import os
from dotenv import load_dotenv

load_dotenv("credentials.env")

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
    e.EndTime,
    e.URL
FROM Scraper.EBAY e
JOIN Scraper.GPU g ON g.ID = e.ID
JOIN ModelStats ms ON ms.Model = g.Model
WHERE
    e.SoldDate IS NULL
    AND (e.Price / 100) < ms.AvgPrice * 0.8
    AND e.EndTime < NOW() + INTERVAL 2 HOUR
ORDER BY PotentialGain DESC;
"""

CPU_DEALS_QUERY = """
WITH ModelStats AS (
    SELECT
        c.Model,
        c.Socket,
        AVG(e.Price / 100) AS AvgPrice
    FROM Scraper.CPU c
    JOIN Scraper.EBAY e ON e.ID = c.ID
    WHERE
        e.SoldDate IS NOT NULL
        AND c.Model IS NOT NULL
    GROUP BY c.Model, c.Socket
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
    e.EndTime,
    e.URL
FROM Scraper.EBAY e
JOIN Scraper.CPU c ON c.ID = e.ID
JOIN ModelStats ms ON ms.Model = c.Model AND (ms.Socket = c.Socket OR (ms.Socket IS NULL AND c.Socket IS NULL))
WHERE
    e.SoldDate IS NULL
    AND (e.Price / 100) < ms.AvgPrice * 0.8
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
    e.EndTime,
    e.URL
FROM Scraper.EBAY e
JOIN Scraper.HDD h ON h.ID = e.ID
JOIN ModelStats ms ON ms.CapacityGB = h.CapacityGB AND ms.Interface = h.Interface
WHERE
    e.SoldDate IS NULL
    AND (e.Price / 100) < ms.AvgPrice * 0.8
    AND e.EndTime < NOW() + INTERVAL 2 HOUR
ORDER BY PotentialGain DESC;
"""

DEALS_QUERIES = {
    'gpu': GPU_DEALS_QUERY,
    'cpu': CPU_DEALS_QUERY,
    'hdd': HDD_DEALS_QUERY,
}

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

        for row in rows:
            if row.get("EndTime"):
                row["EndTime"] = row["EndTime"].strftime("%H:%M:%S")

        return jsonify({"status": "ok", "deals": rows})
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)