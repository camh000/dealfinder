from flask import Flask, abort, jsonify, redirect, render_template, request, make_response
from flask.json.provider import DefaultJSONProvider
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
import json
import mariadb
import os
import logging
import statistics
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


# Optional HTTP Basic Auth — enabled when both env vars are set. Guards every
# route (the notify-settings API manages HA tokens, so nothing is left open).
# Browsers cache the credentials, so the PWA/service worker keeps working.
HTTP_USER = os.environ.get('HTTP_USER', '')
HTTP_PASS = os.environ.get('HTTP_PASS', '')
if HTTP_USER and HTTP_PASS:
    log.info("HTTP Basic Auth enabled for all routes")


@app.before_request
def _basic_auth_gate():
    if not (HTTP_USER and HTTP_PASS):
        return None
    auth = request.authorization
    if (auth is not None and auth.type == 'basic'
            and hmac.compare_digest(auth.username or '', HTTP_USER)
            and hmac.compare_digest(auth.password or '', HTTP_PASS)):
        return None
    return ('Authentication required', 401,
            {'WWW-Authenticate': 'Basic realm="dealfinder"'})


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
    conn = mariadb.connect(
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", 3305)),
        database=os.environ["DB_NAME"]
    )
    # Pin the session to UTC — NOW() in the deal-window SQL must match the
    # UTC-naive EndTimes we store, and _iso_utc()'s UTC tag must be true.
    cur = conn.cursor()
    cur.execute("SET time_zone = '+00:00'")
    cur.close()
    return conn


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
    ROUND(d.PredictedFinal / 100, 2)                         AS PredictedFinal,
    e.SoldDate,
    ROUND((1 - COALESCE(d.FinalPrice, e.Price) / d.AvgMarketPrice) * 100, 1) AS ActualDiscountPct,
    d.EndedUnsold,
    e.URL
FROM Scraper.DealOutcomes d
JOIN Scraper.EBAY e ON e.ID = d.EbayID
WHERE e.SoldDate IS NOT NULL AND d.NearMiss = 0
ORDER BY d.SurfacedAt DESC
LIMIT 200;
"""

# Pending = genuinely still in flight. GaveUp rows are excluded: the verifier
# permanently stopped chasing them (eBay purges completed listings from search
# after ~90 days, so they can never resolve), and listing them as "unresolved"
# just buried the one or two live deals under a pile of dead months-old records.
# They're still counted separately in the summary so the history isn't hidden.
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
WHERE e.SoldDate IS NULL AND d.GaveUp = 0 AND d.NearMiss = 0
ORDER BY d.EndTime ASC;
"""

GAVE_UP_COUNT_QUERY = """
SELECT COUNT(*) AS n
FROM Scraper.DealOutcomes d
JOIN Scraper.EBAY e ON e.ID = d.EbayID
WHERE e.SoldDate IS NULL AND d.GaveUp = 1 AND d.NearMiss = 0;
"""

# Near-miss control cohort (12–20% band, recorded but never surfaced):
# resolved win rate, kept out of the headline scoreboard. If this rivals
# the main win rate, the surfacing threshold is leaving money on the table.
NEAR_MISS_SUMMARY_QUERY = """
SELECT COUNT(*) AS n,
       COALESCE(SUM(COALESCE(d.FinalPrice, e.Price) < d.AvgMarketPrice), 0) AS beat
FROM Scraper.DealOutcomes d
JOIN Scraper.EBAY e ON e.ID = d.EbayID
WHERE d.NearMiss = 1
  AND e.SoldDate IS NOT NULL
  AND d.EndedUnsold = 0
  AND COALESCE(d.FinalPrice, e.Price) IS NOT NULL;
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
            "ALTER TABLE Scraper.DealOutcomes ADD COLUMN PredictedFinal INT NULL",
            "ALTER TABLE Scraper.DealOutcomes ADD COLUMN VerifyMisses INT NOT NULL DEFAULT 0",
            "ALTER TABLE Scraper.DealOutcomes ADD COLUMN NearMiss TINYINT(1) NOT NULL DEFAULT 0",
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


def ensure_quantity_column():
    """EBAY.Quantity (units per listing) — job lots priced per unit. The
    scraper's EnsureQuantityColumn also backfills HDD titles; this one just
    guarantees the column exists before the deal queries reference it."""
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        try:
            cur.execute("ALTER TABLE Scraper.EBAY ADD COLUMN Quantity INT NULL")
            conn.commit()
            log.info("EBAY: added Quantity column")
        except mariadb.Error as e:
            if getattr(e, "errno", None) != DUP_COLUMN_ERRNO:
                log.error("EBAY: unexpected error adding Quantity column: %s", e)
    except Exception as e:
        log.error("Could not ensure Quantity column: %s", e)
    finally:
        if conn:
            conn.close()


ensure_quantity_column()


def ensure_seller_feedback_columns():
    """EBAY.SellerFeedbackPct/-Count — the deal queries reference them, so the
    web container must guarantee they exist even if it starts first."""
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        for col_sql in ("SellerFeedbackPct FLOAT NULL", "SellerFeedbackCount INT NULL"):
            try:
                cur.execute(f"ALTER TABLE Scraper.EBAY ADD COLUMN {col_sql}")
                conn.commit()
                log.info("EBAY: added %s column", col_sql.split()[0])
            except mariadb.Error as e:
                if getattr(e, "errno", None) != DUP_COLUMN_ERRNO:
                    log.error("EBAY: unexpected error adding %s: %s", col_sql.split()[0], e)
    except Exception as e:
        log.error("Could not ensure seller feedback columns: %s", e)
    finally:
        if conn:
            conn.close()


ensure_seller_feedback_columns()


def ensure_last_seen_column():
    """EBAY.LastSeenAt — deal queries filter on it; see EnsureLastSeenColumn
    in EbayScraper for the stamped backfill (this just guarantees existence)."""
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        try:
            cur.execute("ALTER TABLE Scraper.EBAY ADD COLUMN LastSeenAt DATETIME NULL")
            conn.commit()
            cur.execute("UPDATE Scraper.EBAY SET LastSeenAt = NOW() WHERE LastSeenAt IS NULL")
            conn.commit()
            log.info("EBAY: added LastSeenAt column")
        except mariadb.Error as e:
            if getattr(e, "errno", None) != DUP_COLUMN_ERRNO:
                log.error("EBAY: unexpected error adding LastSeenAt column: %s", e)
    except Exception as e:
        log.error("Could not ensure LastSeenAt column: %s", e)
    finally:
        if conn:
            conn.close()


ensure_last_seen_column()


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
        # /api/health reads LastRunStats — must exist even before the
        # scraper's first run of the new code writes it.
        try:
            cur.execute("ALTER TABLE Scraper.ScrapeMeta ADD COLUMN LastRunStats TEXT NULL")
            conn.commit()
        except mariadb.Error as e:
            if getattr(e, "errno", None) != DUP_COLUMN_ERRNO:
                log.error("ScrapeMeta: unexpected error adding LastRunStats: %s", e)
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


def ensure_ssd_table():
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS Scraper.SSD (
                ID         BIGINT      NOT NULL PRIMARY KEY,
                Brand      VARCHAR(50),
                CapacityGB INT,
                Interface  VARCHAR(10),
                FormFactor VARCHAR(10),
                DriveType  VARCHAR(16),
                Gen        TINYINT     NULL,
                FOREIGN KEY (ID) REFERENCES Scraper.EBAY(ID)
            )
        """)
        conn.commit()
    except Exception:
        pass
    finally:
        if conn:
            conn.close()


ensure_ssd_table()


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
        mtimes = []
        for sub in ('templates', 'static', os.path.join('static', 'css'), os.path.join('static', 'js')):
            d = os.path.join(here, sub)
            if os.path.isdir(d):
                mtimes += [os.path.getmtime(os.path.join(d, f))
                           for f in sorted(os.listdir(d))
                           if os.path.isfile(os.path.join(d, f))]
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


PAGE_CATEGORIES = ('gpu', 'cpu', 'hdd', 'ssd', 'ram')


@app.route("/")
def index():
    return redirect("/deals/gpu")


@app.route("/deals/<cat>")
def deals_page(cat):
    if cat not in PAGE_CATEGORIES:
        abort(404)
    return render_template("deals.html", category=cat)


@app.route("/outcomes")
def outcomes_page():
    return render_template("outcomes.html")


@app.route("/prices")
def prices_page():
    return render_template("prices.html")


@app.route("/model/<cat>")
def model_page(cat):
    if cat not in PAGE_CATEGORIES:
        abort(404)
    return render_template("model.html", category=cat)


@app.route("/settings")
def settings_page():
    return render_template("settings.html")


@app.route("/health")
def health_page():
    return render_template("health.html")


@app.route("/api/deals")
def deals():
    product_type = request.args.get('type', 'gpu').lower()
    if product_type not in ('gpu', 'cpu', 'hdd', 'ssd', 'ram'):
        return jsonify({"status": "error", "message": f"Unknown type '{product_type}'. Use gpu, cpu, hdd, ssd, or ram."}), 400

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

        # Outcome-calibrated predictions: re-rank on the PREDICTED discount
        # (contested auctions get bid past their current price) and drop
        # rows history says will close at/above market — a deal in name
        # only. Must run before the ISO conversion — the annotator needs
        # EndTime as a datetime.
        pcur = conn.cursor()
        pcur.execute(queries.SNIPE_PREMIUM_QUERY)
        premiums = queries.median_ratios(pcur.fetchall())
        queries.annotate_predictions(rows, product_type, premiums)
        rows = queries.filter_predicted_deals(rows)

        for row in rows:
            for col in ("EndTime", "SurfacedAt"):
                if row.get(col):
                    row[col] = _iso_utc(row[col])

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
        # Badges count the same rows the list shows: full deals query,
        # prediction-annotated, predicted-over-market rows dropped. The
        # plain count query can't apply the prediction gate (it lives in
        # Python), and a badge that disagrees with its list reads as a bug.
        pcur = conn.cursor()
        pcur.execute(queries.SNIPE_PREMIUM_QUERY)
        premiums = queries.median_ratios(pcur.fetchall())
        counts = {}
        for key in ('gpu', 'cpu', 'hdd', 'ssd', 'ram'):
            cur.execute(get_deals_query(key, window_hours, min_discount))
            rows = cur.fetchall()
            queries.annotate_predictions(rows, key, premiums)
            counts[key] = len(queries.filter_predicted_deals(rows))
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

        cur.execute(GAVE_UP_COUNT_QUERY)
        gave_up = cur.fetchone()['n']

        cur.execute(NEAR_MISS_SUMMARY_QUERY)
        nm = cur.fetchone()
        nm_resolved = int(nm['n'] or 0)
        nm_beat = int(nm['beat'] or 0)
        near_miss = {
            "resolved": nm_resolved,
            "beat_market": nm_beat,
            "win_rate": round(nm_beat / nm_resolved * 100, 1) if nm_resolved else 0,
        }

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

        # How good is the snipe-premium model? Median |error| of the final
        # price it predicted at surfacing vs what the auction actually did.
        errs = [abs(float(r['FinalPrice']) - float(r['PredictedFinal']))
                / float(r['PredictedFinal']) * 100
                for r in priced if r.get('PredictedFinal')]
        prediction = {"median_abs_err_pct": round(statistics.median(errs), 1) if errs else None,
                      "n": len(errs)}

        # Lifetime report card (excludes the near-miss control cohort).
        cur.execute("""
            SELECT ROUND(SUM(AvgMarketPrice) / 100)
            FROM Scraper.DealOutcomes WHERE NearMiss = 0
        """)
        (market_value_tracked,) = cur.fetchone().values()
        discounts = [float(r['ActualDiscountPct']) for r in priced
                     if r.get('ActualDiscountPct') is not None]
        lifetime = {
            "market_value_tracked": float(market_value_tracked or 0),
            "median_actual_discount": round(statistics.median(discounts), 1) if discounts else None,
        }

        return jsonify({
            "status": "ok",
            "summary": {
                "total_resolved": total_resolved,
                "beat_market": beat_market,
                "win_rate": win_rate,
                "total_pending": len(pending),
                "ended_unsold": ended_unsold,
                "gave_up": gave_up,
                "near_miss": near_miss,
                "prediction": prediction,
                "lifetime": lifetime,
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


VALID_CATEGORIES = ('GPU', 'CPU', 'HDD', 'SSD', 'RAM')


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


def _guide_extras(cur, cat):
    """Per-group live-listing counts and 30-day trend for one category.

    Trend = median of sold effective prices in the last 30 days vs the prior
    31–90 days (singles only), reported when both windows have >=3 samples.
    Returns ({group_tuple: live_count}, {group_tuple: (trend_pct, n_recent)}).
    """
    cfg = queries.CATEGORIES[cat]
    a = cfg['alias']
    group_cols = [c for c, _ in cfg['group_cols']]
    cols = ', '.join(f"{a}.{c}" for c in group_cols)

    cur.execute(f"""
        SELECT {cols}, COUNT(*)
        FROM Scraper.{cfg['table']} {a}
        JOIN Scraper.EBAY e ON e.ID = {a}.ID
        WHERE e.SoldDate IS NULL AND e.EndTime > NOW()
          AND e.LastSeenAt > NOW() - INTERVAL {queries.STALE_DEAL_MINUTES} MINUTE
        GROUP BY {cols}
    """)
    live = {tuple(r[:-1]): int(r[-1]) for r in cur.fetchall()}

    cur.execute(f"""
        SELECT {cols}, (e.Price + COALESCE(e.Shipping, 0)) / 100, e.SoldDate
        FROM Scraper.{cfg['table']} {a}
        JOIN Scraper.EBAY e ON e.ID = {a}.ID
        WHERE e.SoldDate > NOW() - INTERVAL 90 DAY
          AND e.Price IS NOT NULL AND COALESCE(e.Quantity, 1) = 1
    """)
    cutoff = datetime.utcnow() - timedelta(days=30)
    recent, prior = {}, {}
    for row in cur.fetchall():
        key, price, sold = tuple(row[:-2]), float(row[-2]), row[-1]
        (recent if sold >= cutoff else prior).setdefault(key, []).append(price)
    trends = {}
    for key in recent:
        if len(recent[key]) >= 3 and len(prior.get(key, [])) >= 3:
            med_r, med_p = statistics.median(recent[key]), statistics.median(prior[key])
            if med_p > 0:
                trends[key] = (round((med_r / med_p - 1) * 100, 1), len(recent[key]))
    return live, trends


@app.route("/api/price-guide")
def price_guide():
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        pcur = conn.cursor()
        result = {}
        for cat in ('gpu', 'cpu', 'hdd', 'ssd', 'ram'):
            cur.execute(queries.build_price_guide_query(cat))
            rows = cur.fetchall()
            live, trends = _guide_extras(pcur, cat)
            group_cols = [c for c, _ in queries.CATEGORIES[cat]['group_cols']]
            for r in rows:
                key = tuple(r.get(c) for c in group_cols)
                r['LiveCount'] = live.get(key, 0)
                t = trends.get(key)
                r['Trend30dPct'] = t[0] if t else None
                r['TrendSamples'] = t[1] if t else None
            result[cat] = rows
        return jsonify({"status": "ok", "components": result})
    except Exception as e:
        log.error("price_guide error: %s", e)
        return jsonify({"status": "error", "message": "internal error"}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/snapshots")
def snapshots():
    """Price/bid trajectories for live tracked deals — feeds row sparklines."""
    ids = [int(x) for x in request.args.get('ids', '').split(',') if x.strip().isdigit()][:100]
    if not ids:
        return jsonify({"status": "ok", "series": {}})
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        placeholders = ', '.join(['%s'] * len(ids))
        cur.execute(f"""
            SELECT EbayID, MinutesLeft, ROUND(EffPrice / 100, 2), Bids
            FROM Scraper.DealSnapshots
            WHERE EbayID IN ({placeholders})
            ORDER BY EbayID, SnapAt
        """, tuple(ids))
        series = {}
        for ebay_id, mins, price, bids in cur.fetchall():
            series.setdefault(str(ebay_id), []).append(
                [mins, float(price), int(bids or 0)])
        return jsonify({"status": "ok", "series": series})
    except Exception as e:
        log.error("snapshots error: %s", e)
        return jsonify({"status": "error", "message": "internal error"}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/model-detail")
def model_detail():
    """Everything we know about one market group: recent individual sales,
    live listings, headline stats and a monthly trend series."""
    cat = request.args.get('type', '').lower()
    if cat not in queries.CATEGORIES:
        return jsonify({"status": "error", "message": "unknown type"}), 400
    cfg = queries.CATEGORIES[cat]
    a, table = cfg['alias'], cfg['table']
    params = {col: request.args.get(col) for col, _ in cfg['group_cols']}
    cond, values = queries.model_where(cat, params)
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)

        cur.execute(f"""
            SELECT e.Title, ROUND((e.Price + COALESCE(e.Shipping, 0)) / 100, 2) AS Price,
                   e.SoldDate, e.URL, COALESCE(e.Quantity, 1) AS Quantity
            FROM Scraper.{table} {a}
            JOIN Scraper.EBAY e ON e.ID = {a}.ID
            WHERE e.SoldDate IS NOT NULL AND e.Price IS NOT NULL
              AND e.SoldDate > NOW() - INTERVAL 180 DAY AND {cond}
            ORDER BY e.SoldDate DESC
            LIMIT 200
        """, tuple(values))
        sold = cur.fetchall()

        cur.execute(f"""
            SELECT e.ID, e.Title, ROUND(e.Price / 100, 2) AS ItemPrice,
                   ROUND(COALESCE(e.Shipping, 0) / 100, 2) AS Shipping,
                   e.Bids, e.EndTime, e.URL, COALESCE(e.Quantity, 1) AS Quantity
            FROM Scraper.{table} {a}
            JOIN Scraper.EBAY e ON e.ID = {a}.ID
            WHERE e.SoldDate IS NULL AND e.EndTime > NOW()
              AND e.LastSeenAt > NOW() - INTERVAL {queries.STALE_DEAL_MINUTES} MINUTE
              AND {cond}
            ORDER BY e.EndTime
            LIMIT 50
        """, tuple(values))
        live = cur.fetchall()

        # Stats + monthly trend from single-unit sales only (lot totals would
        # skew everything upward).
        singles = [r for r in sold if int(r['Quantity']) == 1]
        cutoff_120 = datetime.utcnow() - timedelta(days=queries.MARKET_STATS_DAYS)
        window = [float(r['Price']) for r in singles if r['SoldDate'] >= cutoff_120]
        stats = {
            "median": round(statistics.median(window), 2) if window else None,
            "min": min(window) if window else None,
            "max": max(window) if window else None,
            "n": len(window),
        }
        monthly = {}
        for r in singles:
            monthly.setdefault(r['SoldDate'].strftime('%Y-%m'), []).append(float(r['Price']))
        trend = [{"month": m, "median": round(statistics.median(v), 2), "n": len(v)}
                 for m, v in sorted(monthly.items())]

        for r in sold:
            if r.get('SoldDate'):
                r['SoldDate'] = _iso_utc(r['SoldDate'])
        for r in live:
            if r.get('EndTime'):
                r['EndTime'] = _iso_utc(r['EndTime'])

        return jsonify({"status": "ok", "group": params, "stats": stats,
                        "trend": trend, "sold": sold[:40], "live": live})
    except Exception as e:
        log.error("model_detail error: %s", e)
        return jsonify({"status": "error", "message": "internal error"}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/health")
def health_api():
    """Scrape + data observability without docker logs."""
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)

        cur.execute("SELECT LastScrapeAt, LastRunStats FROM Scraper.ScrapeMeta WHERE id = 1")
        meta = cur.fetchone() or {}
        run_stats = None
        if meta.get('LastRunStats'):
            try:
                run_stats = json.loads(meta['LastRunStats'])
            except ValueError:
                run_stats = None

        categories = {}
        pcur = conn.cursor()
        for cat in PAGE_CATEGORIES:
            table = queries.CATEGORIES[cat]['table']
            pcur.execute(f"""
                SELECT
                    SUM(e.SoldDate IS NULL AND e.EndTime > NOW()
                        AND e.LastSeenAt > NOW() - INTERVAL {queries.STALE_DEAL_MINUTES} MINUTE),
                    SUM(e.SoldDate IS NOT NULL
                        AND e.SoldDate > NOW() - INTERVAL {queries.MARKET_STATS_DAYS} DAY),
                    SUM(e.SoldDate IS NOT NULL)
                FROM Scraper.{table} s JOIN Scraper.EBAY e ON e.ID = s.ID
            """)
            fresh, sold_recent, sold_all = pcur.fetchone()
            categories[cat] = {"live": int(fresh or 0),
                               "sold_window": int(sold_recent or 0),
                               "sold_total": int(sold_all or 0)}

        pcur.execute("""
            SELECT SUM(e.SoldDate IS NULL AND d.GaveUp = 0 AND d.NearMiss = 0),
                   SUM(e.SoldDate IS NOT NULL AND d.NearMiss = 0),
                   SUM(d.GaveUp = 1), SUM(d.NearMiss = 1)
            FROM Scraper.DealOutcomes d JOIN Scraper.EBAY e ON e.ID = d.EbayID
        """)
        pending, resolved, gave_up, near_miss = pcur.fetchone()
        pcur.execute("SELECT COUNT(*), COUNT(DISTINCT EbayID) FROM Scraper.DealSnapshots")
        snaps, snap_items = pcur.fetchone()

        return jsonify({
            "status": "ok",
            "last_scrape_at": _iso_utc(meta.get('LastScrapeAt')),
            "last_run": run_stats,
            "categories": categories,
            "outcomes": {"pending": int(pending or 0), "resolved": int(resolved or 0),
                         "gave_up": int(gave_up or 0), "near_miss": int(near_miss or 0)},
            "snapshots": {"rows": int(snaps or 0), "deals": int(snap_items or 0)},
        })
    except Exception as e:
        log.error("health error: %s", e)
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
