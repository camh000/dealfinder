from flask import Flask, jsonify, render_template, request, make_response
from flask.json.provider import DefaultJSONProvider
from datetime import timezone
from decimal import Decimal
import hashlib
import mariadb
import os
import logging
from dotenv import load_dotenv

import queries

load_dotenv("credentials.env")

log = logging.getLogger(__name__)


class _JSONProvider(DefaultJSONProvider):
    """Serialise DB Decimals as JSON numbers (mariadb >=1.1.14 returns
    decimal.Decimal for ROUND()/AVG(), which would otherwise stringify)."""
    @staticmethod
    def default(o):
        if isinstance(o, Decimal):
            return float(o)
        return DefaultJSONProvider.default(o)


app = Flask(__name__)
app.json = _JSONProvider(app)


def _iso_utc(dt):
    """Serialise a naive DB datetime as a UTC ISO-8601 string.

    DB timestamps are stored as naive datetimes that already represent UTC
    (see parse_ebay_endtime's offset application). Tagging them as UTC at
    the API boundary makes the contract explicit for frontend consumers.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()

def get_connection():
    return mariadb.connect(
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", 3305)),
        database=os.environ["DB_NAME"]
    )


# Deal / count / price-guide SQL lives in queries.py — one source of truth
# shared with the scheduler's server-side surfacing (EbayScraper.SurfaceDeals).
def get_deals_query(product_type: str, window_hours: int = 2, min_discount: float = 20) -> str:
    return queries.build_deals_query(product_type, window_hours, min_discount)


def get_count_query(product_type: str, window_hours: int = 2, min_discount: float = 20) -> str:
    return queries.build_count_query(product_type, window_hours, min_discount)


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
    ROUND(COALESCE(d.FinalPrice, e.Price) / 100, 2)          AS FinalPrice,
    e.SoldDate,
    ROUND((1 - COALESCE(d.FinalPrice, e.Price) / d.AvgMarketPrice) * 100, 1) AS ActualDiscountPct,
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


# MariaDB errno 1060 (ER_DUP_FIELDNAME) means the column already exists and
# is expected on every run after the first. Any other error is real.
DUP_COLUMN_ERRNO = 1060


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
            "ALTER TABLE Scraper.DealOutcomes ADD COLUMN FinalPrice INT NULL",
        ]:
            col_name = col_sql.split("ADD COLUMN ")[1].split()[0]
            try:
                cur.execute(col_sql)
                conn.commit()
                log.info("DealOutcomes: added %s column", col_name)
            except mariadb.Error as e:
                if getattr(e, "errno", None) != DUP_COLUMN_ERRNO:
                    log.error(
                        "DealOutcomes: unexpected error adding %s (errno=%s): %s",
                        col_name, getattr(e, "errno", None), e,
                    )
        log.info("DealOutcomes table ready")
    except Exception as e:
        log.error("Could not create DealOutcomes table: %s", e)
    finally:
        if conn:
            conn.close()


ensure_outcomes_table()


def ensure_shipping_column():
    """EBAY.Shipping (pence) — postage folded into effective pricing."""
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        try:
            cur.execute("ALTER TABLE Scraper.EBAY ADD COLUMN Shipping INT NULL")
            conn.commit()
            log.info("EBAY: added Shipping column")
        except mariadb.Error as e:
            if getattr(e, "errno", None) != DUP_COLUMN_ERRNO:
                log.error("EBAY: unexpected error adding Shipping column: %s", e)
    except Exception as e:
        log.error("Could not ensure Shipping column: %s", e)
    finally:
        if conn:
            conn.close()


ensure_shipping_column()


def ensure_scrape_meta():
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS Scraper.ScrapeMeta (
                id           TINYINT  NOT NULL DEFAULT 1 PRIMARY KEY,
                LastScrapeAt DATETIME NULL
            )
        """)
        conn.commit()
    except Exception:
        pass
    finally:
        if conn:
            conn.close()


ensure_scrape_meta()


def ensure_ram_table():
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS Scraper.RAM (
                ID         BIGINT      NOT NULL PRIMARY KEY,
                Brand      VARCHAR(50),
                CapacityGB INT,
                Type       VARCHAR(10),
                Speed      INT,
                FOREIGN KEY (ID) REFERENCES Scraper.EBAY(ID)
            )
        """)
        conn.commit()
    except Exception:
        pass
    finally:
        if conn:
            conn.close()


ensure_ram_table()


def _compute_sw_version() -> str:
    """Produce a short version tag used for the SW cache name.

    Priority: APP_VERSION env var (set explicitly at deploy) >
    short hash of sw.js + Index.html mtimes (cache busts when either changes) >
    literal 'dev' if the files are unreadable.
    """
    explicit = os.environ.get('APP_VERSION')
    if explicit:
        return explicit[:12]
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        mtimes = (
            os.path.getmtime(os.path.join(here, 'static', 'sw.js')),
            os.path.getmtime(os.path.join(here, 'templates', 'Index.html')),
        )
        return hashlib.sha256(str(mtimes).encode()).hexdigest()[:8]
    except OSError:
        return 'dev'


_SW_VERSION = _compute_sw_version()
log.info("Service worker cache version: pcd-%s", _SW_VERSION)


@app.route('/sw.js')
def service_worker():
    # Rewrite the hardcoded cache name with a per-deploy version so that
    # clients invalidate stale assets on upgrade without needing a manual
    # version bump in static/sw.js. Also tell browsers not to cache sw.js
    # itself — SW specs recommend max-age=0 for the worker script.
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(here, 'static', 'sw.js'), 'r', encoding='utf-8') as f:
            body = f.read()
    except OSError as e:
        log.error("sw.js unreadable: %s", e)
        return jsonify({"status": "error", "message": "not found"}), 404
    body = body.replace("'pcd-v1'", f"'pcd-{_SW_VERSION}'", 1)
    resp = make_response(body)
    resp.headers['Content-Type'] = 'application/javascript'
    resp.headers['Service-Worker-Allowed'] = '/'
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp


@app.route("/")
def index():
    return render_template("Index.html")


@app.route("/api/deals")
def deals():
    product_type = request.args.get('type', 'gpu').lower()
    if product_type not in ('gpu', 'cpu', 'hdd', 'ram'):
        return jsonify({"status": "error", "message": f"Unknown type '{product_type}'. Use gpu, cpu, hdd, or ram."}), 400

    # Parse window parameter (default 2 hours, max 24)
    try:
        window_hours = int(request.args.get('window', 2))
        window_hours = max(1, min(window_hours, 24))
    except (ValueError, TypeError):
        window_hours = 2

    # Parse min_discount parameter (default 20%, min 0%)
    try:
        min_discount = float(request.args.get('min_discount', 20))
        min_discount = max(0, min_discount)
    except (ValueError, TypeError):
        min_discount = 20

    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute(get_deals_query(product_type, window_hours, min_discount))
        rows = cur.fetchall()

        # NOTE: deal surfacing (DealOutcomes first-sighting capture) moved to
        # the scheduler (EbayScraper.SurfaceDeals) — this endpoint is now
        # read-only, so page loads no longer have DB write side effects and
        # deals are tracked even when nobody has the dashboard open.

        for row in rows:
            if row.get("EndTime"):
                row["EndTime"] = _iso_utc(row["EndTime"])

        return jsonify({"status": "ok", "deals": rows})
    except Exception as e:
        log.error("deals error: %s", e)
        return jsonify({"status": "error", "message": "internal error"}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/deal-counts")
def deal_counts():
    # Parse window parameter (default 2 hours, max 24)
    try:
        window_hours = int(request.args.get('window', 2))
        window_hours = max(1, min(window_hours, 24))
    except (ValueError, TypeError):
        window_hours = 2

    # Parse min_discount parameter (default 20%, min 0%)
    try:
        min_discount = float(request.args.get('min_discount', 20))
        min_discount = max(0, min_discount)
    except (ValueError, TypeError):
        min_discount = 20

    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        counts = {}
        for key in ('gpu', 'cpu', 'hdd', 'ram'):
            cur.execute(get_count_query(key, window_hours, min_discount))
            counts[key] = cur.fetchone()['cnt']
        return jsonify({"status": "ok", "counts": counts})
    except Exception as e:
        log.error("deal_counts error: %s", e)
        return jsonify({"status": "error", "message": "internal error"}), 500
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

        cur.execute("""
            SELECT LastScrapeAt FROM Scraper.ScrapeMeta WHERE id = 1
        """)
        row = cur.fetchone()
        last_scrape = row["LastScrapeAt"] if row else None

        return jsonify({
            "active_listings": active,
            "sold_listings": sold,
            "last_scrape_at": _iso_utc(last_scrape),
        })
    except Exception as e:
        log.error("stats error: %s", e)
        return jsonify({"status": "error", "message": "internal error"}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/outcomes")
def outcomes():
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)

        # Lazy-backfill: lock in FinalPrice for rows resolved before the column existed.
        # Once written, d.FinalPrice is immutable — future re-listings of the sold item
        # won't change e.Price in the DB but this guards against it anyway.
        cur.execute("""
            UPDATE Scraper.DealOutcomes d
            JOIN Scraper.EBAY e ON e.ID = d.EbayID
            SET d.FinalPrice = e.Price
            WHERE e.SoldDate IS NOT NULL AND d.FinalPrice IS NULL AND d.EndedUnsold = 0
        """)
        if cur.rowcount > 0:
            conn.commit()

        cur.execute(OUTCOMES_RESOLVED_QUERY)
        resolved = cur.fetchall()

        cur.execute(OUTCOMES_PENDING_QUERY)
        pending = cur.fetchall()

        for row in resolved:
            for col in ('EndTime', 'SoldDate', 'SurfacedAt'):
                if row.get(col):
                    row[col] = _iso_utc(row[col])

        for row in pending:
            for col in ('EndTime', 'SurfacedAt'):
                if row.get(col):
                    row[col] = _iso_utc(row[col])

        # Win-rate math excludes ended-unsold auctions: they have no final
        # price, the table hides them, and nobody could have "beaten" them —
        # counting them silently diluted the headline stat.
        priced = [r for r in resolved
                  if not r['EndedUnsold'] and r['FinalPrice'] is not None]
        beat_market = sum(1 for r in priced if r['FinalPrice'] < r['AvgMarketPrice'])
        total_resolved = len(priced)
        ended_unsold = sum(1 for r in resolved if r['EndedUnsold'])
        win_rate = round(beat_market / total_resolved * 100, 1) if total_resolved > 0 else 0

        return jsonify({
            "status": "ok",
            "summary": {
                "total_resolved": total_resolved,
                "beat_market": beat_market,
                "win_rate": win_rate,
                "total_pending": len(pending),
                "ended_unsold": ended_unsold,
            },
            "resolved": resolved,
            "pending": pending,
        })
    except Exception as e:
        log.error("outcomes error: %s", e)
        return jsonify({"status": "error", "message": "internal error"}), 500
    finally:
        if conn:
            conn.close()


VALID_CATEGORIES = ('GPU', 'CPU', 'HDD', 'RAM')


def ensure_notify_recipients_table():
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS Scraper.NotifyRecipients (
                ID            INT AUTO_INCREMENT PRIMARY KEY,
                Name          VARCHAR(50)  NOT NULL,
                HaUrl         VARCHAR(200) NOT NULL,
                HaToken       VARCHAR(300) NOT NULL,
                NotifyService VARCHAR(100) NOT NULL,
                Categories    VARCHAR(50)  NOT NULL DEFAULT 'GPU,CPU,HDD,RAM',
                Enabled       TINYINT(1)   NOT NULL DEFAULT 1
            )
        """)
        conn.commit()
    except Exception as e:
        log.error("Could not create NotifyRecipients table: %s", e)
    finally:
        if conn:
            conn.close()


ensure_notify_recipients_table()


@app.route("/api/notify-settings", methods=["GET"])
def notify_settings_list():
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT ID, Name, HaUrl, NotifyService, Categories, Enabled,
                   (HaToken IS NOT NULL AND HaToken != '') AS TokenSet
            FROM Scraper.NotifyRecipients ORDER BY ID
        """)
        rows = cur.fetchall()
        for r in rows:
            r['Categories'] = [c for c in (r['Categories'] or '').split(',') if c]
            r['Enabled'] = bool(r['Enabled'])
            r['TokenSet'] = bool(r['TokenSet'])
        return jsonify({"status": "ok", "recipients": rows})
    except Exception as e:
        log.error("notify_settings_list error: %s", e)
        return jsonify({"status": "error", "message": "internal error"}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/notify-settings", methods=["POST"])
def notify_settings_save():
    """Create or update a recipient. Blank token on update keeps the stored one."""
    body = request.get_json(silent=True) or {}
    name = (body.get('name') or '').strip()[:50]
    ha_url = (body.get('ha_url') or '').strip().rstrip('/')[:200]
    service = (body.get('notify_service') or '').strip()[:100]
    token = (body.get('ha_token') or '').strip()[:300]
    enabled = 1 if body.get('enabled', True) else 0
    cats = [c.upper() for c in (body.get('categories') or []) if c.upper() in VALID_CATEGORIES]
    rid = body.get('id')

    if not (name and ha_url and service):
        return jsonify({"status": "error", "message": "name, ha_url and notify_service are required"}), 400
    if not ha_url.startswith(('http://', 'https://')):
        return jsonify({"status": "error", "message": "ha_url must start with http:// or https://"}), 400
    if not cats:
        return jsonify({"status": "error", "message": "select at least one category"}), 400

    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        if rid:
            if not token:
                # SECURITY: without this, anyone who can reach the API could
                # repoint an existing recipient's URL at their own server and
                # the scheduler would POST the STORED bearer token to it.
                # A destination change therefore requires re-entering the token.
                cur.execute("SELECT HaUrl FROM Scraper.NotifyRecipients WHERE ID=%s", (int(rid),))
                row = cur.fetchone()
                if row is None:
                    return jsonify({"status": "error", "message": "recipient not found"}), 404
                if row[0].rstrip('/') != ha_url:
                    return jsonify({"status": "error",
                                    "message": "HA URL changed — re-enter the token to confirm"}), 400
            if token:
                cur.execute("""
                    UPDATE Scraper.NotifyRecipients
                    SET Name=%s, HaUrl=%s, HaToken=%s, NotifyService=%s, Categories=%s, Enabled=%s
                    WHERE ID=%s
                """, (name, ha_url, token, service, ','.join(cats), enabled, int(rid)))
            else:
                cur.execute("""
                    UPDATE Scraper.NotifyRecipients
                    SET Name=%s, HaUrl=%s, NotifyService=%s, Categories=%s, Enabled=%s
                    WHERE ID=%s
                """, (name, ha_url, service, ','.join(cats), enabled, int(rid)))
        else:
            if not token:
                return jsonify({"status": "error", "message": "ha_token is required for a new recipient"}), 400
            cur.execute("""
                INSERT INTO Scraper.NotifyRecipients (Name, HaUrl, HaToken, NotifyService, Categories, Enabled)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (name, ha_url, token, service, ','.join(cats), enabled))
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        log.error("notify_settings_save error: %s", e)
        return jsonify({"status": "error", "message": "internal error"}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/notify-settings/<int:rid>", methods=["DELETE"])
def notify_settings_delete(rid):
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM Scraper.NotifyRecipients WHERE ID=%s", (rid,))
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        log.error("notify_settings_delete error: %s", e)
        return jsonify({"status": "error", "message": "internal error"}), 500
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
        for cat in ('gpu', 'cpu', 'hdd', 'ram'):
            cur.execute(queries.build_price_guide_query(cat))
            result[cat] = cur.fetchall()
        return jsonify({"status": "ok", "components": result})
    except Exception as e:
        log.error("price_guide error: %s", e)
        return jsonify({"status": "error", "message": "internal error"}), 500
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    # Dev-only entry point. Production runs under gunicorn (see Dockerfile.web),
    # which binds its own socket and ignores this block. Default to loopback so
    # a stray `python App.py` doesn't expose the dev server on all interfaces.
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_PORT", "5000"))
    app.run(host=host, port=port, debug=False)
