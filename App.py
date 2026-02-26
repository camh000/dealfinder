from flask import Flask, jsonify, render_template
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

DEALS_QUERY = """
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

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/deals")
def deals():
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute(DEALS_QUERY)
        rows = cur.fetchall()
        conn.close()

        for row in rows:
            if row.get("EndTime"):
                row["EndTime"] = row["EndTime"].strftime("%H:%M:%S")

        return jsonify({"status": "ok", "deals": rows})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/stats")
def stats():
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)

        cur.execute("SELECT COUNT(*) AS total FROM Scraper.EBAY WHERE SoldDate IS NULL")
        active = cur.fetchone()["total"]

        cur.execute("SELECT COUNT(*) AS total FROM Scraper.EBAY WHERE SoldDate IS NOT NULL")
        sold = cur.fetchone()["total"]

        cur.execute("SELECT MAX(SoldDate) AS last_update FROM Scraper.EBAY")
        last = cur.fetchone()["last_update"]

        conn.close()
        return jsonify({
            "active_listings": active,
            "sold_listings": sold,
            "last_updated": str(last) if last else "Never"
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)