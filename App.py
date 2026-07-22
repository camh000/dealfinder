from flask import Flask, abort, jsonify, redirect, render_template, request, make_response, session
from flask.json.provider import DefaultJSONProvider
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
import json
import mariadb
import os
import logging
import secrets
import statistics
from werkzeug.security import check_password_hash, generate_password_hash
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


@app.after_request
def _noindex_header(resp):
    """Keep the whole app out of search indexes — public but unlisted. The
    header covers API/non-HTML responses too, and (unlike a robots.txt
    Disallow) still lets a crawler READ the noindex, which is what actually
    removes a URL from results. Belt-and-braces with the <meta robots> tag."""
    resp.headers['X-Robots-Tag'] = 'noindex, nofollow'
    # API payloads are live data (deal feeds, insight cohorts, stats). Without
    # this, a browser or Cloudflare can serve a stale response — e.g. an insight
    # page showing an old win rate that "never changes".
    if request.path.startswith('/api/'):
        resp.headers['Cache-Control'] = 'no-store'
    return resp


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


def _hours_left(endtime):
    """Hours from now until a naive-UTC EndTime (floored at 0); None if unknown.
    Feeds the time-aware snipe premium (queries.premium_for)."""
    if endtime is None or not hasattr(endtime, 'timestamp'):
        return None
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return max((endtime - now).total_seconds() / 3600.0, 0.0)


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


# Structured satellite attributes attached to each outcome row so the shared
# context filters (socket / capacity / chipset / …) work on this page too — the
# outcome itself only stores a Model label. One row lives in one satellite, so
# COALESCE across them picks the right value (GPU uses the Model label already).
_OUTCOME_ATTRS = """,
    COALESCE(c.Socket, mb.Socket)                       AS Socket,
    mb.Chipset                                          AS Chipset,
    COALESCE(h.CapacityGB, sd.CapacityGB, ra.CapacityGB) AS CapacityGB,
    COALESCE(h.Interface, sd.Interface)                 AS Interface,
    COALESCE(h.DriveType, sd.DriveType)                 AS DriveType,
    ra.Type                                             AS Type,
    COALESCE(mb.FormFactor, h.FormFactor, sd.FormFactor, ra.FormFactor) AS FormFactor,
    ra.KitConfig                                        AS KitConfig"""
_OUTCOME_JOINS = """
LEFT JOIN Scraper.CPU  c  ON c.ID  = d.EbayID
LEFT JOIN Scraper.MOBO mb ON mb.ID = d.EbayID
LEFT JOIN Scraper.HDD  h  ON h.ID  = d.EbayID
LEFT JOIN Scraper.SSD  sd ON sd.ID = d.EbayID
LEFT JOIN Scraper.RAM  ra ON ra.ID = d.EbayID"""

OUTCOMES_RESOLVED_QUERY = f"""
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
    e.URL{_OUTCOME_ATTRS}
FROM Scraper.DealOutcomes d
JOIN Scraper.EBAY e ON e.ID = d.EbayID{_OUTCOME_JOINS}
WHERE e.SoldDate IS NOT NULL AND d.NearMiss = 0
ORDER BY d.SurfacedAt DESC
LIMIT 200;
"""

# Pending = genuinely still in flight. GaveUp rows are excluded: the verifier
# permanently stopped chasing them (eBay purges completed listings from search
# after ~90 days, so they can never resolve), and listing them as "unresolved"
# just buried the one or two live deals under a pile of dead months-old records.
# They're still counted separately in the summary so the history isn't hidden.
OUTCOMES_PENDING_QUERY = f"""
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
    e.URL{_OUTCOME_ATTRS}
FROM Scraper.DealOutcomes d
JOIN Scraper.EBAY e ON e.ID = d.EbayID{_OUTCOME_JOINS}
WHERE e.SoldDate IS NULL AND d.GaveUp = 0 AND d.NearMiss = 0
ORDER BY d.EndTime ASC;
"""

GAVE_UP_COUNT_QUERY = """
SELECT COUNT(*) AS n
FROM Scraper.DealOutcomes d
JOIN Scraper.EBAY e ON e.ID = d.EbayID
WHERE e.SoldDate IS NULL AND d.GaveUp = 1 AND d.NearMiss = 0;
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
            "ALTER TABLE Scraper.DealOutcomes ADD COLUMN PredMargin TINYINT(1) NOT NULL DEFAULT 0",
            "ALTER TABLE Scraper.DealOutcomes ADD COLUMN ItemLocation VARCHAR(80) NULL",
            "ALTER TABLE Scraper.DealOutcomes ADD COLUMN Epid VARCHAR(20) NULL",
            "ALTER TABLE Scraper.DealOutcomes ADD COLUMN CategoryPath VARCHAR(200) NULL",
            "ALTER TABLE Scraper.DealOutcomes ADD COLUMN EnrichNote VARCHAR(60) NULL",
            "ALTER TABLE Scraper.DealOutcomes ADD COLUMN ItemCondition VARCHAR(40) NULL",
            "ALTER TABLE Scraper.DealOutcomes ADD COLUMN Watchers INT NULL",
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


def ensure_reserve_column():
    """EBAY.ReserveNotMet — the deal queries gate on it."""
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        try:
            cur.execute("ALTER TABLE Scraper.EBAY ADD COLUMN ReserveNotMet TINYINT(1) NOT NULL DEFAULT 0")
            conn.commit()
        except mariadb.Error as e:
            if getattr(e, "errno", None) != DUP_COLUMN_ERRNO:
                log.error("EBAY: unexpected error adding ReserveNotMet: %s", e)
    except Exception:
        pass
    finally:
        if conn:
            conn.close()


ensure_reserve_column()


def ensure_listing_type_column():
    """EBAY.ListingType — the deal queries filter auctions vs BIN on it.
    The scraper container owns the real migration; this guards deploy order."""
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        try:
            cur.execute("ALTER TABLE Scraper.EBAY ADD COLUMN ListingType VARCHAR(8) NOT NULL DEFAULT 'auction'")
            conn.commit()
        except mariadb.Error as e:
            if getattr(e, "errno", None) != DUP_COLUMN_ERRNO:
                log.error("EBAY: unexpected error adding ListingType: %s", e)
    except Exception:
        pass
    finally:
        if conn:
            conn.close()


ensure_listing_type_column()


def ensure_seller_feedback_columns():
    """EBAY.SellerFeedbackPct/-Count — the deal queries reference them, so the
    web container must guarantee they exist even if it starts first."""
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        for col_sql in ("SellerFeedbackPct FLOAT NULL", "SellerFeedbackCount INT NULL",
                        "Watchers INT NULL"):
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


def ensure_first_seen_column():
    """EBAY.FirstSeenAt — the BIN feed's 'added within' filter reads it; the
    scraper owns the real backfill, this just guards deploy order."""
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        try:
            cur.execute("ALTER TABLE Scraper.EBAY ADD COLUMN FirstSeenAt DATETIME NULL")
            conn.commit()
            cur.execute("UPDATE Scraper.EBAY SET FirstSeenAt = LastSeenAt WHERE FirstSeenAt IS NULL")
            conn.commit()
            log.info("EBAY: added FirstSeenAt column")
        except mariadb.Error as e:
            if getattr(e, "errno", None) != DUP_COLUMN_ERRNO:
                log.error("EBAY: unexpected error adding FirstSeenAt column: %s", e)
    except Exception as e:
        log.error("Could not ensure FirstSeenAt column: %s", e)
    finally:
        if conn:
            conn.close()


ensure_first_seen_column()


def ensure_offer_columns():
    """EBAY.HasBin / HasBestOffer — the deal-page advisor reads them; scraper
    owns the real migration, this guards deploy order."""
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        for col in ('HasBid', 'HasBin', 'HasBestOffer'):
            try:
                cur.execute(f"ALTER TABLE Scraper.EBAY ADD COLUMN {col} TINYINT(1) NULL")
                conn.commit()
            except mariadb.Error as e:
                if getattr(e, "errno", None) != DUP_COLUMN_ERRNO:
                    log.error("EBAY: unexpected error adding %s: %s", col, e)
    except Exception as e:
        log.error("Could not ensure offer columns: %s", e)
    finally:
        if conn:
            conn.close()


ensure_offer_columns()


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


def ensure_ram_kit_column():
    """RAM.KitConfig — deal/guide queries reference it; the scraper's
    EnsureRamKitConfig does the title backfill."""
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        try:
            cur.execute("ALTER TABLE Scraper.RAM ADD COLUMN KitConfig VARCHAR(10) NULL")
            conn.commit()
        except mariadb.Error as e:
            if getattr(e, "errno", None) != DUP_COLUMN_ERRNO:
                log.error("RAM: unexpected error adding KitConfig: %s", e)
    except Exception:
        pass
    finally:
        if conn:
            conn.close()


ensure_ram_kit_column()


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


def ensure_mobo_table():
    """MOBO satellite (new motherboard category). Created web-side too so the
    guide/deal queries don't depend on the scraper booting first."""
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS Scraper.MOBO (
                ID         BIGINT      NOT NULL PRIMARY KEY,
                Brand      VARCHAR(40),
                Chipset    VARCHAR(12),
                Socket     VARCHAR(12),
                FormFactor VARCHAR(8),
                IsBundle   TINYINT(1)  NOT NULL DEFAULT 0,
                FOREIGN KEY (ID) REFERENCES Scraper.EBAY(ID)
            )
        """)
        conn.commit()
        # IsBundle on both bundle-bearing tables (CPU+mobo bundles). Guarded so
        # the guide/deal queries can filter bundles out regardless of boot order.
        for tbl in ('CPU', 'MOBO'):
            try:
                cur.execute(f"ALTER TABLE Scraper.{tbl} ADD COLUMN IsBundle TINYINT(1) NOT NULL DEFAULT 0")
                conn.commit()
            except mariadb.Error as e:
                if getattr(e, "errno", None) != DUP_COLUMN_ERRNO:
                    log.error("%s IsBundle column: %s", tbl, e)
    except Exception:
        pass
    finally:
        if conn:
            conn.close()


ensure_mobo_table()


# ── user accounts ──────────────────────────────────────────────────────────────
# Session-cookie login. Bootstrap mode: while no users exist, everything is
# open and Settings offers "create the admin account"; the first user created
# becomes the admin. Passwords are werkzeug-hashed; the session secret is
# generated once and persisted in AppConfig so logins survive restarts.

def ensure_auth_tables():
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS Scraper.AppConfig (
                K VARCHAR(40) PRIMARY KEY,
                V TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS Scraper.Users (
                ID           INT AUTO_INCREMENT PRIMARY KEY,
                Username     VARCHAR(40) NOT NULL UNIQUE,
                PasswordHash VARCHAR(255) NOT NULL,
                IsAdmin      TINYINT(1) NOT NULL DEFAULT 0,
                CreatedAt    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS Scraper.PriceAlerts (
                ID          INT AUTO_INCREMENT PRIMARY KEY,
                UserID      INT NOT NULL,
                Category    VARCHAR(10) NOT NULL,
                GroupParams TEXT NOT NULL,
                Label       VARCHAR(150),
                Kind        VARCHAR(20) NOT NULL DEFAULT 'listing_below',
                TargetPrice INT NOT NULL,
                RecipientID INT NULL,
                Enabled     TINYINT(1) NOT NULL DEFAULT 1,
                CreatedAt   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                LastFiredAt DATETIME NULL
            )
        """)
        conn.commit()
        # BIN-watch alerts (kind='bin_new') carry a min-discount %, not a price,
        # and store their /bin filter set in GroupParams — so TargetPrice must
        # allow NULL and a MinDiscount column is added.
        try:
            cur.execute("ALTER TABLE Scraper.PriceAlerts ADD COLUMN MinDiscount FLOAT NULL")
            conn.commit()
        except mariadb.Error as e:
            if getattr(e, "errno", None) != DUP_COLUMN_ERRNO:
                log.error("PriceAlerts: MinDiscount column: %s", e)
        try:
            cur.execute("ALTER TABLE Scraper.PriceAlerts MODIFY TargetPrice INT NULL")
            conn.commit()
        except mariadb.Error as e:
            log.error("PriceAlerts: relax TargetPrice: %s", e)

        # ── Unified notifications (v1) ──────────────────────────────────────
        # Every notification is now a user-owned SUBSCRIPTION delivered to that
        # user's own HA endpoint. Users carry the endpoint; PriceAlerts carries
        # the scope (all/filter/group) and listing type (auction/bin/any). Kind
        # is repurposed as the TRIGGER: listing_price / median_price /
        # discount_pct (migration maps the old listing_below/median_below/
        # bin_new values).
        def _add_col(sql):
            try:
                cur.execute(sql)
                conn.commit()
            except mariadb.Error as e:
                if getattr(e, "errno", None) != DUP_COLUMN_ERRNO:
                    log.error("column add failed (%s): %s", sql, e)
        _add_col("ALTER TABLE Scraper.Users ADD COLUMN HaUrl VARCHAR(200) NULL")
        _add_col("ALTER TABLE Scraper.Users ADD COLUMN HaToken VARCHAR(300) NULL")
        _add_col("ALTER TABLE Scraper.Users ADD COLUMN NotifyService VARCHAR(100) NULL")
        _add_col("ALTER TABLE Scraper.Users ADD COLUMN NotifyEnabled TINYINT(1) NOT NULL DEFAULT 1")
        _add_col("ALTER TABLE Scraper.PriceAlerts ADD COLUMN ScopeKind VARCHAR(8) NOT NULL DEFAULT 'group'")
        _add_col("ALTER TABLE Scraper.PriceAlerts ADD COLUMN ListingType VARCHAR(8) NOT NULL DEFAULT 'any'")
        _migrate_notify_unified(cur, conn)
    except Exception as e:
        log.error("Could not ensure auth tables: %s", e)
    finally:
        if conn:
            conn.close()


# This deployment's recipient→user mapping for the one-time unification. The
# recipient names ('Cam'/'Dad') don't match the usernames ('cameron'/'spencer'),
# and Cam confirmed the pairing; a recipient already referenced by a user's
# alert (RecipientID) is mapped by that link first, this covers the rest.
_RECIPIENT_USER_ALIASES = {'cam': 'cameron', 'dad': 'spencer'}

# Old alert-kind → (new trigger Kind, ScopeKind, ListingType).
_KIND_UPGRADE = {
    'listing_below': ('listing_price', 'group', 'any'),
    'median_below':  ('median_price',  'group', 'any'),
    'bin_new':       ('discount_pct',  'filter', 'bin'),
}

# The old auction feed surfaced at this discount for everyone with the category
# ticked; the migrated default subscriptions reproduce that exactly.
_DEFAULT_FEED_DISCOUNT = float(os.environ.get('SURFACE_MIN_DISCOUNT', '20'))


def _migrate_notify_unified(cur, conn):
    """One-time: fold NotifyRecipients into per-user endpoints, turn each
    recipient's category ticks into default auction subscriptions, and upgrade
    existing alert kinds to the trigger/scope/listing-type model. Idempotent
    via an AppConfig marker."""
    cur.execute("SELECT V FROM Scraper.AppConfig WHERE K = 'notify_unified_v1'")
    if cur.fetchone():
        return
    try:
        # Upgrade existing alert rows first (kind → trigger + scope + type).
        for old, (kind, scope, ltype) in _KIND_UPGRADE.items():
            cur.execute("""UPDATE Scraper.PriceAlerts
                           SET Kind=%s, ScopeKind=%s, ListingType=%s WHERE Kind=%s""",
                        (kind, scope, ltype, old))

        # Copy recipient endpoints into their matching users + default subs.
        try:
            cur.execute("""SELECT ID, Name, HaUrl, HaToken, NotifyService, Categories, Enabled
                           FROM Scraper.NotifyRecipients""")
            recipients = cur.fetchall()
        except mariadb.Error:
            recipients = []          # no legacy recipients (fresh install)
        cur.execute("SELECT ID, Username FROM Scraper.Users")
        users = cur.fetchall()
        by_name = {u[1].lower(): u[0] for u in users}
        # recipient ID → user ID via an existing alert link (authoritative).
        cur.execute("""SELECT DISTINCT RecipientID, UserID FROM Scraper.PriceAlerts
                       WHERE RecipientID IS NOT NULL""")
        by_alert = {r: u for r, u in cur.fetchall()}

        for rid, name, url, token, service, cats, enabled in recipients:
            uid = (by_alert.get(rid)
                   or by_name.get((name or '').lower())
                   or by_name.get(_RECIPIENT_USER_ALIASES.get((name or '').lower(), '')))
            if not uid:
                log.warning("notify migration: recipient %r has no matching user — skipped", name)
                continue
            # Endpoint: don't clobber an endpoint a user already set.
            cur.execute("""UPDATE Scraper.Users
                           SET HaUrl=%s, HaToken=%s, NotifyService=%s, NotifyEnabled=%s
                           WHERE ID=%s AND (HaUrl IS NULL OR HaUrl='')""",
                        (url, token, service, 1 if enabled else 1, uid))
            # Ticked categories → default auction discount subscriptions.
            for c in [x.strip().lower() for x in (cats or '').split(',') if x.strip()]:
                if c not in queries.CATEGORIES:
                    continue
                cur.execute("""SELECT 1 FROM Scraper.PriceAlerts
                               WHERE UserID=%s AND Category=%s AND ScopeKind='all'
                                 AND ListingType='auction' AND Kind='discount_pct' LIMIT 1""",
                            (uid, c))
                if cur.fetchone():
                    continue
                cur.execute("""INSERT INTO Scraper.PriceAlerts
                        (UserID, Category, GroupParams, Label, Kind, ScopeKind,
                         ListingType, TargetPrice, MinDiscount, Enabled)
                        VALUES (%s,%s,'{}',%s,'discount_pct','all','auction',NULL,%s,1)""",
                            (uid, c, f"{c.upper()} auctions", _DEFAULT_FEED_DISCOUNT))

        cur.execute("INSERT INTO Scraper.AppConfig (K, V) VALUES ('notify_unified_v1','1')")
        conn.commit()
        log.info("notify migration: unified %d recipient(s) into user endpoints + default subs",
                 len(recipients))
    except Exception as e:
        conn.rollback()
        log.error("notify unification migration failed: %s", e)


ensure_auth_tables()


def _app_secret() -> str:
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT V FROM Scraper.AppConfig WHERE K = 'secret_key'")
        row = cur.fetchone()
        if row:
            return row[0]
        secret = secrets.token_hex(32)
        cur.execute("INSERT INTO Scraper.AppConfig (K, V) VALUES ('secret_key', %s)", (secret,))
        conn.commit()
        return secret
    except Exception:
        # No DB (tests/dev) — sessions just will not survive restarts.
        return secrets.token_hex(32)
    finally:
        if conn:
            conn.close()


app.secret_key = _app_secret()

_users_exist_cache = {"at": 0.0, "val": False}


def _users_exist() -> bool:
    import time as _time
    if _time.time() - _users_exist_cache["at"] < 30:
        return _users_exist_cache["val"]
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM Scraper.Users")
        val = cur.fetchone()[0] > 0
    except Exception:
        val = False
    finally:
        if conn:
            conn.close()
    _users_exist_cache.update(at=_time.time(), val=val)
    return val


_AUTH_EXEMPT = ('/login', '/sw.js', '/api/login')

# Guest mode: signed-out visitors can VIEW everything — every page and every
# market-data read — but cannot change anything. Mutating requests and the
# settings APIs (recipients expose HA URLs; users/alerts/passwords/bin config
# are private) require a session. This is the shape that makes exposing the
# app publicly tolerable: the anonymous surface is read-only market data.
_GUEST_BLOCKED_PREFIXES = ('/api/my-endpoint', '/api/bin-settings',
                           '/api/users', '/api/subscriptions', '/api/password')


@app.before_request
def _session_gate():
    if request.path.startswith('/static') or request.path in _AUTH_EXEMPT:
        return None
    if not _users_exist():          # bootstrap mode — open until an admin exists
        return None
    if session.get('uid'):
        return None
    if (request.method in ('GET', 'HEAD', 'OPTIONS')
            and not any(request.path.startswith(p) for p in _GUEST_BLOCKED_PREFIXES)):
        return None                 # guest: the read-only surface stays open
    if request.path.startswith('/api/'):
        return jsonify({"status": "error", "message": "login required"}), 401
    return redirect('/login?next=' + request.path)


def _current_user():
    return {"id": session.get('uid'), "name": session.get('uname'),
            "admin": bool(session.get('admin'))} if session.get('uid') else None


def _require_admin():
    if not _users_exist():
        return None                  # bootstrap: first user setup is open
    u = _current_user()
    if not u or not u["admin"]:
        return jsonify({"status": "error", "message": "admin only"}), 403
    return None


@app.route('/login', methods=['GET'])
def login_page():
    if not _users_exist() or session.get('uid'):
        return redirect('/')
    return render_template('login.html')


@app.route('/api/login', methods=['POST'])
def api_login():
    body = request.get_json(silent=True) or {}
    name = (body.get('username') or '').strip()[:40]
    pw = body.get('password') or ''
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT ID, Username, PasswordHash, IsAdmin FROM Scraper.Users WHERE Username = %s", (name,))
        row = cur.fetchone()
        if not row or not check_password_hash(row['PasswordHash'], pw):
            return jsonify({"status": "error", "message": "wrong username or password"}), 401
        session.permanent = True
        session['uid'] = row['ID']
        session['uname'] = row['Username']
        session['admin'] = bool(row['IsAdmin'])
        return jsonify({"status": "ok"})
    except Exception as e:
        log.error("login error: %s", e)
        return jsonify({"status": "error", "message": "internal error"}), 500
    finally:
        if conn:
            conn.close()


@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({"status": "ok"})


@app.route('/api/me')
def api_me():
    return jsonify({"status": "ok", "user": _current_user(),
                    "bootstrap": not _users_exist()})


@app.route('/api/users', methods=['GET'])
def users_list():
    err = _require_admin()
    if err:
        return err
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT ID, Username, IsAdmin, CreatedAt FROM Scraper.Users ORDER BY ID")
        rows = cur.fetchall()
        for r in rows:
            r['IsAdmin'] = bool(r['IsAdmin'])
            r['CreatedAt'] = _iso_utc(r['CreatedAt'])
        return jsonify({"status": "ok", "users": rows})
    except Exception as e:
        log.error("users_list error: %s", e)
        return jsonify({"status": "error", "message": "internal error"}), 500
    finally:
        if conn:
            conn.close()


@app.route('/api/users', methods=['POST'])
def users_create():
    err = _require_admin()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    name = (body.get('username') or '').strip()[:40]
    pw = body.get('password') or ''
    if not name or len(pw) < 8:
        return jsonify({"status": "error", "message": "username required; password min 8 chars"}), 400
    bootstrap = not _users_exist()
    is_admin = 1 if (bootstrap or body.get('is_admin')) else 0   # first user is always admin
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO Scraper.Users (Username, PasswordHash, IsAdmin) VALUES (%s, %s, %s)",
                    (name, generate_password_hash(pw), is_admin))
        conn.commit()
        _users_exist_cache["at"] = 0
        if bootstrap:               # log the founder straight in
            session.permanent = True
            session['uid'] = cur.lastrowid
            session['uname'] = name
            session['admin'] = True
        return jsonify({"status": "ok"})
    except mariadb.IntegrityError:
        return jsonify({"status": "error", "message": "username taken"}), 400
    except Exception as e:
        log.error("users_create error: %s", e)
        return jsonify({"status": "error", "message": "internal error"}), 500
    finally:
        if conn:
            conn.close()


@app.route('/api/users/<int:uid>', methods=['DELETE'])
def users_delete(uid):
    err = _require_admin()
    if err:
        return err
    if uid == session.get('uid'):
        return jsonify({"status": "error", "message": "you cannot delete yourself"}), 400
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM Scraper.PriceAlerts WHERE UserID = %s", (uid,))
        cur.execute("DELETE FROM Scraper.Users WHERE ID = %s", (uid,))
        conn.commit()
        _users_exist_cache["at"] = 0
        return jsonify({"status": "ok"})
    except Exception as e:
        log.error("users_delete error: %s", e)
        return jsonify({"status": "error", "message": "internal error"}), 500
    finally:
        if conn:
            conn.close()


@app.route('/api/password', methods=['POST'])
def password_change():
    u = _current_user()
    if not u:
        return jsonify({"status": "error", "message": "login required"}), 401
    body = request.get_json(silent=True) or {}
    pw = body.get('password') or ''
    if len(pw) < 8:
        return jsonify({"status": "error", "message": "password min 8 chars"}), 400
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("UPDATE Scraper.Users SET PasswordHash = %s WHERE ID = %s",
                    (generate_password_hash(pw), u["id"]))
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        log.error("password_change error: %s", e)
        return jsonify({"status": "error", "message": "internal error"}), 500
    finally:
        if conn:
            conn.close()


# A subscription's TRIGGER (repurposed Kind column). Price triggers carry a
# £ TargetPrice; the discount trigger carries a % MinDiscount.
_TRIGGER_KINDS = ('listing_price', 'median_price', 'discount_pct')
_SCOPE_KINDS = ('all', 'filter', 'group')
_LISTING_TYPES = ('auction', 'bin', 'any')


@app.route('/api/subscriptions', methods=['GET'])
def subscriptions_list():
    u = _current_user()
    if not u and _users_exist():
        return jsonify({"status": "error", "message": "login required"}), 401
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT ID, Category, GroupParams, Label, Kind, ScopeKind, ListingType,
                   ROUND(TargetPrice / 100, 2) AS TargetPrice, MinDiscount, Enabled,
                   LastFiredAt
            FROM Scraper.PriceAlerts
            WHERE UserID = %s ORDER BY ID DESC
        """, (u["id"] if u else 0,))
        rows = cur.fetchall()
        for r in rows:
            r['Enabled'] = bool(r['Enabled'])
            r['GroupParams'] = json.loads(r['GroupParams'] or '{}')
            r['LastFiredAt'] = _iso_utc(r['LastFiredAt'])
        return jsonify({"status": "ok", "subscriptions": rows})
    except Exception as e:
        log.error("subscriptions_list error: %s", e)
        return jsonify({"status": "error", "message": "internal error"}), 500
    finally:
        if conn:
            conn.close()


def _subscription_fields(body):
    """Validate a subscription create body → dict of columns or an
    (error, status) tuple. A subscription is (scope, listing_type, trigger):
      trigger discount_pct → a % MinDiscount + a scope (all/filter/group);
      trigger listing_price/median_price → a £ TargetPrice + a group scope."""
    kind = body.get('kind') if body.get('kind') in _TRIGGER_KINDS else 'listing_price'
    scope = body.get('scope_kind') if body.get('scope_kind') in _SCOPE_KINDS else None
    ltype = body.get('listing_type') if body.get('listing_type') in _LISTING_TYPES else None
    params = body.get('filters') if body.get('filters') is not None else body.get('group')
    params = params or {}
    if not isinstance(params, dict):
        return ("scope params must be an object", 400)
    if kind == 'discount_pct':
        scope = scope or ('all' if not params else 'filter')
        ltype = ltype or 'bin'
        try:
            md = float(body.get('min_discount'))
            if not 1 <= md <= 90:
                raise ValueError
        except (TypeError, ValueError):
            return ("min_discount must be 1–90", 400)
        return dict(kind=kind, scope=scope, ltype=ltype,
                    group=json.dumps(params), target=None, min_disc=md)
    # price trigger — always an exact market group, either listing type
    try:
        target = int(round(float(body.get('target_price')) * 100))
        if target <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return ("target_price must be a positive number", 400)
    return dict(kind=kind, scope='group', ltype=ltype or 'any',
                group=json.dumps(params), target=target, min_disc=None)


@app.route('/api/subscriptions', methods=['POST'])
def subscriptions_create():
    u = _current_user()
    if not u and _users_exist():
        return jsonify({"status": "error", "message": "login required"}), 401
    body = request.get_json(silent=True) or {}
    cat = (body.get('category') or '').lower()
    if cat not in queries.CATEGORIES:
        return jsonify({"status": "error", "message": "unknown category"}), 400
    f = _subscription_fields(body)
    if isinstance(f, tuple):
        return jsonify({"status": "error", "message": f[0]}), f[1]
    label = (body.get('label') or '')[:150]
    if not label:
        label = queries.subscription_label(cat, f['scope'], json.loads(f['group']))[:150]
    uid = u["id"] if u else 0
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        # Whole-category feeds are idempotent: re-"Watch these" retunes the
        # existing feed (and re-enables it) instead of stacking duplicates.
        updated = False
        if f['scope'] == 'all' and f['kind'] == 'discount_pct':
            cur.execute("""UPDATE Scraper.PriceAlerts
                           SET MinDiscount = %s, Enabled = 1, Label = %s
                           WHERE UserID = %s AND Category = %s AND ScopeKind = 'all'
                             AND Kind = 'discount_pct' AND ListingType = %s""",
                        (f['min_disc'], label, uid, cat, f['ltype']))
            updated = cur.rowcount > 0
        if not updated:
            cur.execute("""
                INSERT INTO Scraper.PriceAlerts
                    (UserID, Category, GroupParams, Label, Kind, ScopeKind, ListingType,
                     TargetPrice, MinDiscount)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (uid, cat, f['group'], label, f['kind'],
                  f['scope'], f['ltype'], f['target'], f['min_disc']))
        conn.commit()
        # Nudge if the sub can never fire because the owner has no endpoint.
        cur.execute("""SELECT 1 FROM Scraper.Users WHERE ID = %s
                       AND HaUrl IS NOT NULL AND HaUrl <> ''
                       AND HaToken IS NOT NULL AND HaToken <> ''
                       AND NotifyService IS NOT NULL AND NotifyService <> ''""",
                    (u["id"] if u else 0,))
        has_endpoint = cur.fetchone() is not None
        return jsonify({"status": "ok", "has_endpoint": has_endpoint})
    except Exception as e:
        log.error("subscriptions_create error: %s", e)
        return jsonify({"status": "error", "message": "internal error"}), 500
    finally:
        if conn:
            conn.close()


@app.route('/api/subscriptions/<int:aid>', methods=['PATCH'])
def subscriptions_edit(aid):
    """Edit a subscription: enabled + the threshold (target price or min
    discount, by trigger kind). Owner-scoped."""
    u = _current_user()
    if not u and _users_exist():
        return jsonify({"status": "error", "message": "login required"}), 401
    body = request.get_json(silent=True) or {}
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT Kind FROM Scraper.PriceAlerts WHERE ID = %s AND UserID = %s",
                    (aid, u["id"] if u else 0))
        row = cur.fetchone()
        if not row:
            return jsonify({"status": "error", "message": "not found"}), 404
        sets, vals = [], []
        if 'enabled' in body:
            sets.append("Enabled = %s"); vals.append(1 if body['enabled'] else 0)
        if row['Kind'] == 'discount_pct' and body.get('min_discount') is not None:
            try:
                md = float(body['min_discount'])
                if not 1 <= md <= 90:
                    raise ValueError
            except (TypeError, ValueError):
                return jsonify({"status": "error", "message": "min_discount must be 1–90"}), 400
            sets.append("MinDiscount = %s"); vals.append(md)
        if row['Kind'] != 'discount_pct' and body.get('target_price') is not None:
            try:
                tp = int(round(float(body['target_price']) * 100))
                if tp <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                return jsonify({"status": "error", "message": "target_price must be positive"}), 400
            sets.append("TargetPrice = %s"); vals.append(tp)
        if not sets:
            return jsonify({"status": "ok"})   # nothing to change
        wcur = conn.cursor()
        wcur.execute(f"UPDATE Scraper.PriceAlerts SET {', '.join(sets)} WHERE ID = %s AND UserID = %s",
                     (*vals, aid, u["id"] if u else 0))
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        log.error("subscriptions_edit error: %s", e)
        return jsonify({"status": "error", "message": "internal error"}), 500
    finally:
        if conn:
            conn.close()


@app.route('/api/subscriptions/<int:aid>', methods=['DELETE'])
def subscriptions_delete(aid):
    u = _current_user()
    if not u and _users_exist():
        return jsonify({"status": "error", "message": "login required"}), 401
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM Scraper.PriceAlerts WHERE ID = %s AND UserID = %s",
                    (aid, u["id"] if u else 0))
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        log.error("subscriptions_delete error: %s", e)
        return jsonify({"status": "error", "message": "internal error"}), 500
    finally:
        if conn:
            conn.close()


@app.route('/api/my-endpoint', methods=['GET'])
def my_endpoint_get():
    """The signed-in user's own Home Assistant notification endpoint. This is
    where ALL of their subscriptions deliver — there is no separate recipient."""
    u = _current_user()
    if not u and _users_exist():
        return jsonify({"status": "error", "message": "login required"}), 401
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute("""SELECT HaUrl, NotifyService, COALESCE(NotifyEnabled,1) AS NotifyEnabled,
                              (HaToken IS NOT NULL AND HaToken <> '') AS TokenSet
                       FROM Scraper.Users WHERE ID = %s""", (u["id"] if u else 0,))
        row = cur.fetchone() or {}
        return jsonify({"status": "ok", "endpoint": {
            "HaUrl": row.get('HaUrl') or '',
            "NotifyService": row.get('NotifyService') or '',
            "NotifyEnabled": bool(row.get('NotifyEnabled', 1)),
            "TokenSet": bool(row.get('TokenSet')),
        }})
    except Exception as e:
        log.error("my_endpoint_get error: %s", e)
        return jsonify({"status": "error", "message": "internal error"}), 500
    finally:
        if conn:
            conn.close()


@app.route('/api/my-endpoint', methods=['POST'])
def my_endpoint_save():
    """Save the signed-in user's endpoint. Blank token on update keeps the
    stored one — but changing the HA URL requires re-entering the token, so a
    hijacked session can't repoint the stored bearer token at another server."""
    u = _current_user()
    if not u and _users_exist():
        return jsonify({"status": "error", "message": "login required"}), 401
    body = request.get_json(silent=True) or {}
    ha_url = (body.get('ha_url') or '').strip().rstrip('/')[:200]
    service = (body.get('notify_service') or '').strip()[:100]
    token = (body.get('ha_token') or '').strip()[:300]
    enabled = 1 if body.get('enabled', True) else 0
    uid = u["id"] if u else 0
    if not (ha_url and service):
        return jsonify({"status": "error", "message": "ha_url and notify_service are required"}), 400
    if not ha_url.startswith(('http://', 'https://')):
        return jsonify({"status": "error", "message": "ha_url must start with http:// or https://"}), 400
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT HaUrl, HaToken FROM Scraper.Users WHERE ID = %s", (uid,))
        cur_row = cur.fetchone() or {}
        if not token:
            if not (cur_row.get('HaToken') or ''):
                return jsonify({"status": "error", "message": "ha_token is required"}), 400
            if (cur_row.get('HaUrl') or '').rstrip('/') != ha_url:
                return jsonify({"status": "error",
                                "message": "HA URL changed — re-enter the token to confirm"}), 400
        wcur = conn.cursor()
        if token:
            wcur.execute("""UPDATE Scraper.Users
                            SET HaUrl=%s, HaToken=%s, NotifyService=%s, NotifyEnabled=%s
                            WHERE ID=%s""", (ha_url, token, service, enabled, uid))
        else:
            wcur.execute("""UPDATE Scraper.Users
                            SET HaUrl=%s, NotifyService=%s, NotifyEnabled=%s
                            WHERE ID=%s""", (ha_url, service, enabled, uid))
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        log.error("my_endpoint_save error: %s", e)
        return jsonify({"status": "error", "message": "internal error"}), 500
    finally:
        if conn:
            conn.close()


# ── Buy-It-Now feed ─────────────────────────────────────────────────────────────
# The BIN watcher notifies at bin_min_discount (default 25%); this page is the
# browsable version at a friendlier threshold — every live fixed-price listing
# currently under its market median, no auction dynamics, first to buy wins.

@app.route('/bin')
def bin_page():
    return render_template('bin.html')


@app.route('/api/bin-deals')
def api_bin_deals():
    want = (request.args.get('type') or 'all').lower()
    if want != 'all' and want not in queries.CATEGORIES:
        return jsonify({"status": "error", "message": "unknown category"}), 400
    try:
        min_discount = max(5.0, min(float(request.args.get('min_discount', 10)), 90.0))
    except ValueError:
        min_discount = 10.0
    # added_within = hours since first seen; 0/absent = no window (all live BIN)
    added_within = None
    raw_hours = request.args.get('added_within')
    if raw_hours:
        try:
            h = int(raw_hours)
            if h > 0:
                added_within = max(1, min(h, 720))
        except ValueError:
            pass
    cats = list(queries.CATEGORIES) if want == 'all' else [want]
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        rows = []
        for cat in cats:
            cur.execute(queries.build_bin_deals_query(cat, min_discount, added_within))
            for r in cur.fetchall():
                r['_cat'] = cat
                r['_label'] = queries.model_label_for_row(cat, r)
                r['FirstSeenAt'] = _iso_utc(r.get('FirstSeenAt'))
                rows.append(r)
        rows.sort(key=lambda r: float(r.get('DiscountPct') or 0), reverse=True)
        return jsonify({"status": "ok", "deals": rows})
    except Exception as e:
        log.error("bin_deals error: %s", e)
        return jsonify({"status": "error", "message": "internal error"}), 500
    finally:
        if conn:
            conn.close()


# ── insight pages: prediction accuracy + prediction-surfacing experiment ───────
# Linked from the OUTCOMES stat cards. Read-only analytics over DealOutcomes;
# all math in Python so the SQL stays one plain SELECT each.

def _wilson_ci(k: int, n: int, z: float = 1.96) -> list:
    """95% Wilson score interval for a win rate, as [lo%, hi%]. Honest about
    small n — a 5-for-5 cohort shows ~[57, 100], not '100% proven'."""
    if not n:
        return [0.0, 0.0]
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return [round(max(0.0, centre - half) * 100, 1), round(min(1.0, centre + half) * 100, 1)]


def _err_stats(rows: list) -> dict:
    """Prediction-error aggregates for a list of (signed_err_pct, baseline_abs_pct)."""
    if not rows:
        return {"n": 0}
    errs = [e for e, _ in rows]
    abs_errs = sorted(abs(e) for e in errs)
    baselines = sorted(b for _, b in rows)
    n = len(errs)
    return {
        "n": n,
        "median_abs_err_pct": round(statistics.median(abs_errs), 1),
        "bias_pct": round(statistics.median(errs), 1),
        "baseline_median_abs_err_pct": round(statistics.median(baselines), 1),
        "within_10_pct": round(sum(1 for e in abs_errs if e <= 10) / n * 100, 1),
        "within_20_pct": round(sum(1 for e in abs_errs if e <= 20) / n * 100, 1),
    }


@app.route('/insights/predictions')
def insights_predictions_page():
    return render_template('predictions.html')


@app.route('/api/insights/predictions')
def api_insights_predictions():
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT d.EbayID, d.Category, d.Model, d.BidCount,
                   ROUND(d.SurfacedPrice / 100, 2)                 AS SurfacedPrice,
                   ROUND(d.PredictedFinal / 100, 2)                AS PredictedFinal,
                   ROUND(COALESCE(d.FinalPrice, e.Price) / 100, 2) AS FinalPrice,
                   ROUND(d.AvgMarketPrice / 100, 2)                AS AvgMarketPrice,
                   COALESCE(e.EndTime, d.EndTime)                  AS EndTime
            FROM Scraper.DealOutcomes d
            JOIN Scraper.EBAY e ON e.ID = d.EbayID
            WHERE e.SoldDate IS NOT NULL AND d.EndedUnsold = 0 AND d.NearMiss = 0
              AND d.PredictedFinal IS NOT NULL AND d.PredictedFinal > 0
              AND COALESCE(d.FinalPrice, e.Price) IS NOT NULL
              AND d.SurfacedPrice > 0
            ORDER BY COALESCE(e.EndTime, d.EndTime) DESC
        """)
        raw = cur.fetchall()

        by_cat, by_bucket, pairs, over_time = {}, {}, [], {}
        for r in raw:
            final, pred, surf = float(r['FinalPrice']), float(r['PredictedFinal']), float(r['SurfacedPrice'])
            # signed: positive = closed ABOVE the prediction (we under-called)
            r['ErrPct'] = round((final - pred) / pred * 100, 1)
            baseline = abs(final - surf) / surf * 100  # "no model": final = surfaced price
            pair = (r['ErrPct'], baseline)
            pairs.append(pair)
            by_cat.setdefault(r['Category'], []).append(pair)
            by_bucket.setdefault(queries.bid_bucket(r['BidCount']), []).append(pair)
            # Trend: bucket by the day the auction resolved (EndTime), so the
            # tuning changes show up as the newer days' error drops.
            et = r['EndTime']
            day = et.strftime('%Y-%m-%d') if hasattr(et, 'strftime') else str(et)[:10]
            over_time.setdefault(day, []).append((abs(r['ErrPct']), baseline))
            r['EndTime'] = _iso_utc(r['EndTime'])

        over_time_series = [
            {"date": d,
             "median_abs_err_pct": round(statistics.median([a for a, _ in v]), 1),
             "baseline_median_abs_err_pct": round(statistics.median([b for _, b in v]), 1),
             "n": len(v)}
            for d, v in sorted(over_time.items())
        ]

        histogram = []
        for lo in range(-50, 50, 10):
            n = sum(1 for e, _ in pairs
                    if (lo <= max(-50, min(49.999, e)) < lo + 10))
            histogram.append({"lo": lo, "hi": lo + 10, "n": n})

        # What the model currently believes: the live premium ratios. Keys are
        # (cat, bid, time) / (cat, bid, 'any') / (cat, 'all') — join the tail
        # into a readable bucket label ("4+/60m+", "1-3/any", "all").
        cur2 = conn.cursor()
        cur2.execute(queries.SNIPE_PREMIUM_QUERY)
        ratios = [{"category": key[0], "bucket": '/'.join(map(str, key[1:])),
                   "ratio": ratio, "n": n}
                  for key, (ratio, n)
                  in sorted(queries.median_ratios(cur2.fetchall()).items(),
                            key=lambda kv: tuple(map(str, kv[0])))]

        return jsonify({
            "status": "ok",
            "overall": _err_stats(pairs),
            "by_category": [{"category": c, **_err_stats(v)}
                            for c, v in sorted(by_cat.items())],
            "by_bucket": [{"bucket": b, **_err_stats(v)}
                          for b, v in sorted(by_bucket.items())],
            "histogram": histogram,
            "over_time": over_time_series,
            "ratios": ratios,
            "rows": raw[:150],
        })
    except Exception as e:
        log.error("insights_predictions error: %s", e)
        return jsonify({"status": "error", "message": "internal error"}), 500
    finally:
        if conn:
            conn.close()


# Resolved flagged deals before the prediction-surfacing verdict means much.
_PRED_TARGET_N = 50


@app.route('/insights/predsurface')
def insights_predsurface_page():
    return render_template('predsurface.html')


@app.route('/api/insights/predsurface')
def api_insights_predsurface():
    """Prediction-surfacing experiment: could we surface purely on PREDICTED
    margin? PredMargin rows are deals (any current discount) the model — with
    real premium history — predicts will close >= PRED_SURFACE_MARGIN under
    median: the set the rule "surface predicted >= X% under median" would
    surface. We compare their resolved win rate to the deals the model DID
    predict but at < that margin (the set the rule would skip) — if the flagged
    set wins clearly more, the prediction separates winners from losers well
    enough to drive surfacing. The live feed is shown for reference."""
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT d.NearMiss, d.PredMargin, d.PredictedFinal, d.EndedUnsold, d.GaveUp,
                   ROUND(COALESCE(d.FinalPrice, e.Price) / 100, 2) AS FinalPrice,
                   ROUND(d.AvgMarketPrice / 100, 2)                AS AvgMarketPrice,
                   d.Category, d.Model, d.DiscountPct,
                   ROUND(d.SurfacedPrice / 100, 2)                 AS SurfacedPrice,
                   (e.SoldDate IS NOT NULL)                        AS Sold,
                   COALESCE(e.EndTime, d.EndTime)                  AS EndTime,
                   d.SurfacedAt
            FROM Scraper.DealOutcomes d
            JOIN Scraper.EBAY e ON e.ID = d.EbayID
            ORDER BY COALESCE(e.EndTime, d.EndTime) DESC
        """)
        raw = cur.fetchall()

        def resolved_win(r):
            if not r['Sold'] or r['EndedUnsold'] or r['FinalPrice'] is None or not r['AvgMarketPrice']:
                return None
            return float(r['FinalPrice']) < float(r['AvgMarketPrice'])

        def cohort(rows):
            done = [(r, w) for r in rows if (w := resolved_win(r)) is not None]
            wins = sum(1 for _, w in done if w)
            discs = sorted((1 - float(r['FinalPrice']) / float(r['AvgMarketPrice'])) * 100
                           for r, _ in done)
            return {
                "tracked": len(rows), "resolved": len(done), "wins": wins,
                "win_rate": round(wins / len(done) * 100, 1) if done else None,
                "wr_ci": _wilson_ci(wins, len(done)),
                "pending": sum(1 for r in rows if not r['Sold'] and not r['EndedUnsold'] and not r['GaveUp']),
                "median_actual_discount": round(statistics.median(discs), 1) if discs else None,
            }

        pred_rows = [r for r in raw if r['PredMargin']]
        # Same-window comparison (the cohort only exists from when it started).
        start = min((r['SurfacedAt'] for r in pred_rows if r['SurfacedAt']), default=None)
        in_win = lambda r: start is None or (r['SurfacedAt'] and r['SurfacedAt'] >= start)
        # The rule would SKIP these: the model made a prediction (PredictedFinal
        # set) but below the margin. The direct control for the flagged set.
        skipped_rows = [r for r in raw if r['PredictedFinal'] is not None
                        and not r['PredMargin'] and in_win(r)]
        main_rows = [r for r in raw if not r['NearMiss'] and in_win(r)]

        recent = []
        for r in pred_rows[:100]:
            w = resolved_win(r)
            recent.append({
                "Category": r['Category'], "Model": r['Model'], "DiscountPct": r['DiscountPct'],
                "SurfacedPrice": r['SurfacedPrice'], "FinalPrice": r['FinalPrice'],
                "AvgMarketPrice": r['AvgMarketPrice'], "EndTime": _iso_utc(r['EndTime']),
                "result": ('win' if w else 'miss') if w is not None
                          else ('unsold' if r['EndedUnsold'] else 'gave up' if r['GaveUp'] else 'pending'),
            })

        return jsonify({
            "status": "ok",
            "pred": cohort(pred_rows),          # predicted >= margin under median (rule surfaces)
            "main": cohort(main_rows),          # the live feed, same window (reference)
            "skipped": cohort(skipped_rows),    # predicted below margin (rule skips)
            "margin": _PRED_SURFACE_MARGIN,
            "target_n": _PRED_TARGET_N,
            "window_start": _iso_utc(start),
            "rows": recent,
        })
    except Exception as e:
        log.error("insights_predsurface error: %s", e)
        return jsonify({"status": "error", "message": "internal error"}), 500
    finally:
        if conn:
            conn.close()


_PRED_SURFACE_MARGIN = float(os.environ.get('PRED_SURFACE_MARGIN', '0'))


# ── BIN watcher settings ────────────────────────────────────────────────────────
# Runtime-adjustable from Settings (admin). Stored in AppConfig; the scraper
# re-reads them every scan decision (EbayScraper.GetBinConfig, 60s cache), so
# changes apply within a minute — no container restart. Env vars are the
# defaults for installs that never touch the UI.

_BIN_LIMITS = {'scan_minutes': (5, 240), 'min_discount': (5, 90)}


def _bin_defaults() -> dict:
    return {
        'enabled': os.environ.get('BIN_ENABLED', '1').lower() not in ('0', 'false', ''),
        'scan_minutes': int(os.environ.get('BIN_SCAN_MINUTES', '30')),
        'min_discount': float(os.environ.get('BIN_MIN_DISCOUNT', '25')),
    }


@app.route('/api/bin-settings', methods=['GET'])
def bin_settings_get():
    cfg = _bin_defaults()
    cfg['filters'] = {}
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT K, V FROM Scraper.AppConfig WHERE K IN "
                    "('bin_enabled', 'bin_scan_minutes', 'bin_min_discount', 'bin_filters')")
        stored = dict(cur.fetchall())
        if 'bin_enabled' in stored:
            cfg['enabled'] = stored['bin_enabled'] == '1'
        if 'bin_scan_minutes' in stored:
            cfg['scan_minutes'] = int(stored['bin_scan_minutes'])
        if 'bin_min_discount' in stored:
            cfg['min_discount'] = float(stored['bin_min_discount'])
        if 'bin_filters' in stored:
            cfg['filters'] = json.loads(stored['bin_filters'] or '{}')
    except Exception as e:
        log.error("bin_settings_get error: %s", e)
    finally:
        if conn:
            conn.close()
    return jsonify({"status": "ok", **cfg})


@app.route('/api/bin-settings', methods=['POST'])
def bin_settings_post():
    err = _require_admin()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    try:
        scan = int(body.get('scan_minutes'))
        disc = float(body.get('min_discount'))
        enabled = bool(body.get('enabled'))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "scan_minutes and min_discount must be numbers"}), 400
    lo, hi = _BIN_LIMITS['scan_minutes']
    if not lo <= scan <= hi:
        return jsonify({"status": "error", "message": f"scan interval must be {lo}-{hi} minutes"}), 400
    lo, hi = _BIN_LIMITS['min_discount']
    if not lo <= disc <= hi:
        return jsonify({"status": "error", "message": f"min discount must be {lo}-{hi}%"}), 400
    # Per-category model filters: {"hdd": "6TB, 8TB, 10TB", ...}. A find only
    # notifies when its model label contains one of the comma-separated terms
    # (blank / absent category = everything).
    filters = body.get('filters') or {}
    if not isinstance(filters, dict):
        return jsonify({"status": "error", "message": "filters must be an object"}), 400
    clean_filters = {}
    for k, v in filters.items():
        if k not in queries.CATEGORIES:
            return jsonify({"status": "error", "message": f"unknown filter category '{k}'"}), 400
        if not isinstance(v, str) or len(v) > 300:
            return jsonify({"status": "error", "message": "filter must be a short text list"}), 400
        if v.strip():
            clean_filters[k] = v.strip()
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        for k, v in (('bin_enabled', '1' if enabled else '0'),
                     ('bin_scan_minutes', str(scan)),
                     ('bin_min_discount', str(disc)),
                     ('bin_filters', json.dumps(clean_filters))):
            cur.execute("INSERT INTO Scraper.AppConfig (K, V) VALUES (%s, %s) "
                        "ON DUPLICATE KEY UPDATE V = VALUES(V)", (k, v))
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        log.error("bin_settings_post error: %s", e)
        return jsonify({"status": "error", "message": "internal error"}), 500
    finally:
        if conn:
            conn.close()


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


PAGE_CATEGORIES = ('gpu', 'cpu', 'mobo', 'hdd', 'ssd', 'ram')


@app.route("/")
def index():
    return redirect("/deals")


@app.route("/deals")
def deals_page_all():
    # Unified deals page — all categories, filterable by chip + context filters.
    return render_template("deals.html", preselect="all")


@app.route("/deals/<cat>")
def deals_page(cat):
    # Deep link kept working: opens the unified page with the chip preselected.
    if cat not in PAGE_CATEGORIES:
        abort(404)
    return render_template("deals.html", preselect=cat)


@app.route("/outcomes")
def outcomes_page():
    return render_template("outcomes.html")


@app.route("/prices")
def prices_page():
    return render_template("prices.html")


@app.route("/deal/<int:ebay_id>")
def deal_page(ebay_id):
    return render_template("deal.html", ebay_id=ebay_id)


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
    product_type = request.args.get('type', 'all').lower()
    if product_type != 'all' and product_type not in PAGE_CATEGORIES:
        return jsonify({"status": "error", "message": f"Unknown type '{product_type}'. Use all, gpu, cpu, hdd, ssd, or ram."}), 400

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

    cats = list(PAGE_CATEGORIES) if product_type == 'all' else [product_type]
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        # One premiums fetch covers every category's prediction annotation.
        pcur = conn.cursor()
        pcur.execute(queries.SNIPE_PREMIUM_QUERY)
        premiums = queries.median_ratios(pcur.fetchall())

        # NOTE: deal surfacing (DealOutcomes first-sighting capture) moved to
        # the scheduler (EbayScraper.SurfaceDeals) — this endpoint is now
        # read-only, so page loads no longer have DB write side effects and
        # deals are tracked even when nobody has the dashboard open.
        deals = []
        for cat in cats:
            cur.execute(get_deals_query(cat, window_hours, min_discount))
            rows = cur.fetchall()
            # Outcome-calibrated predictions: re-rank on the PREDICTED discount
            # (contested auctions get bid past their current price) and drop
            # rows history says will close at/above market — a deal in name
            # only. Must run before the ISO conversion — the annotator needs
            # EndTime as a datetime.
            queries.annotate_predictions(rows, cat, premiums)
            rows = queries.filter_predicted_deals(rows)
            for row in rows:
                row['_cat'] = cat
                row['_label'] = queries.model_label_for_row(cat, row)
                for col in ("EndTime", "SurfacedAt"):
                    if row.get(col):
                        row[col] = _iso_utc(row[col])
            deals.extend(rows)

        # CPU+motherboard bundles — SCORED against the sum of the bare CPU and
        # bare board medians, then folded into the deal list with a BUNDLE badge
        # (dual-membership: they carry both the CPU and mobo attributes, so they
        # filter and render under either chip). Excluded from medians and never
        # tracked as an outcome, so they don't touch the win rate. Deduped by ID
        # so the all-view shows each once.
        if product_type in ('all', 'cpu', 'mobo'):
            cur.execute(queries.build_bundle_deals_query(min_discount))
            for b in cur.fetchall():
                b['_bundle'] = 1
                b['_cat'] = 'cpu'    # carries both sides; the page labels by chip
                if b.get('EndTime'):
                    b['EndTime'] = _iso_utc(b['EndTime'])
                deals.append(b)

        # Best predicted discount first, so the all-view leads with the
        # strongest deals across every category (bundles interleave by discount).
        deals.sort(key=lambda r: float(r.get('PredictedDiscountPct') or r.get('DiscountPct') or 0),
                   reverse=True)
        return jsonify({"status": "ok", "deals": deals})
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
        for key in PAGE_CATEGORIES:
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

        # "Listings" = genuinely-live + all-time sold, using the SAME definitions
        # as the health page's category table (else the header double-counts a
        # tail of ended/stale/no-end-time rows that aren't live and aren't sold).
        # Live = not sold, not ended, seen this scrape cycle; both scoped to rows
        # that made it into a category (uncategorised junk isn't a "listing").
        cats_union = " UNION ALL ".join(
            f"SELECT ID FROM Scraper.{queries.CATEGORIES[c]['table']}" for c in PAGE_CATEGORIES)
        cur.execute(f"""
            SELECT
                COALESCE(SUM(e.SoldDate IS NULL AND e.EndTime > NOW()
                    AND e.LastSeenAt > NOW() - INTERVAL {queries.STALE_DEAL_MINUTES} MINUTE), 0) AS live,
                COALESCE(SUM(e.SoldDate IS NOT NULL), 0) AS sold
            FROM Scraper.EBAY e
            WHERE e.ID IN ({cats_union})
        """)
        counts = cur.fetchone()
        active = int(counts["live"] or 0)
        sold = int(counts["sold"] or 0)

        cur.execute("SELECT LastScrapeAt FROM Scraper.ScrapeMeta WHERE id = 1")
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
        # Opportunistic — a lock conflict with the scraper's writes must
        # not fail the whole response (it deadlocked once at deploy time).
        try:
            cur.execute("""
                UPDATE Scraper.DealOutcomes d
                JOIN Scraper.EBAY e ON e.ID = d.EbayID
                SET d.FinalPrice = e.Price
                WHERE e.SoldDate IS NOT NULL AND d.FinalPrice IS NULL AND d.EndedUnsold = 0
            """)
            if cur.rowcount > 0:
                conn.commit()
        except mariadb.Error as e:
            log.warning("FinalPrice backfill skipped: %s", e)
            conn.rollback()

        cur.execute(OUTCOMES_RESOLVED_QUERY)
        resolved = cur.fetchall()

        cur.execute(OUTCOMES_PENDING_QUERY)
        pending = cur.fetchall()

        cur.execute(GAVE_UP_COUNT_QUERY)
        gave_up = cur.fetchone()['n']

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


VALID_CATEGORIES = ('GPU', 'CPU', 'MOBO', 'HDD', 'SSD', 'RAM')


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
        for cat in PAGE_CATEGORIES:
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
                # Guide groups CPUs by Model / mobos by Chipset+FormFactor, so
                # neither carries a Socket column — derive it so the shared
                # socket filter works on the price guide too.
                if cat == 'cpu':
                    r['Socket'] = queries.socket_for(r.get('Model'))
                elif cat == 'mobo':
                    r['Socket'] = queries.chipset_socket(r.get('Chipset'))
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
            SELECT e.ID, e.Title, ROUND((e.Price + COALESCE(e.Shipping, 0)) / 100, 2) AS Price,
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

        # Outcome-calibrated predicted finals for the live listings (drawn on
        # the trend chart as hollow markers, shown in the live table).
        pcur = conn.cursor()
        pcur.execute(queries.SNIPE_PREMIUM_QUERY)
        premiums = queries.median_ratios(pcur.fetchall())
        for r in live:
            eff = float(r['ItemPrice'] or 0) + float(r['Shipping'] or 0)
            ratio, samples = queries.premium_for(
                premiums, cat, int(r['Bids'] or 0), _hours_left(r.get('EndTime')))
            if samples and eff > 0:
                r['PredictedFinalPrice'] = round(eff * ratio, 2)
                r['PremiumSamples'] = samples

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
                        "trend": trend, "sold": sold[:40], "live": live,
                        "window_start": _iso_utc(cutoff_120),
                        "window_days": queries.MARKET_STATS_DAYS})
    except Exception as e:
        log.error("model_detail error: %s", e)
        return jsonify({"status": "error", "message": "internal error"}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/deal/<int:ebay_id>")
def deal_detail(ebay_id):
    """Everything we know about one listing: the row, its category
    attributes, its market group, its outcome record, its price/bid
    trajectory, and the outcome-calibrated prediction."""
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)

        cur.execute("""
            SELECT ID, Title, ROUND(Price / 100, 2) AS ItemPrice,
                   ROUND(COALESCE(Shipping, 0) / 100, 2) AS Shipping,
                   COALESCE(Quantity, 1) AS Quantity, Bids, EndTime,
                   COALESCE(EndTimeExact, 0) AS EndTimeExact, SoldDate, URL,
                   SellerFeedbackPct, SellerFeedbackCount, LastSeenAt,
                   COALESCE(ReserveNotMet, 0) AS ReserveNotMet,
                   COALESCE(ListingType, 'auction') AS ListingType,
                   HasBid, HasBin, HasBestOffer
            FROM Scraper.EBAY WHERE ID = %s
        """, (ebay_id,))
        listing = cur.fetchone()
        if listing is None:
            return jsonify({"status": "error", "message": "unknown listing"}), 404

        category, attrs = None, None
        for cat in PAGE_CATEGORIES:
            cfg = queries.CATEGORIES[cat]
            cur.execute(f"SELECT * FROM Scraper.{cfg['table']} WHERE ID = %s", (ebay_id,))
            row = cur.fetchone()
            if row:
                category, attrs = cat, row
                break

        group, stats = None, None
        if category:
            cfg = queries.CATEGORIES[category]
            group = {col: attrs.get(col) for col, _ in cfg['group_cols']}
            cond, values = queries.model_where(category, group)
            a, table = cfg['alias'], cfg['table']
            pcur = conn.cursor()
            pcur.execute(f"""
                SELECT (e.Price + COALESCE(e.Shipping, 0)) / 100
                FROM Scraper.{table} {a}
                JOIN Scraper.EBAY e ON e.ID = {a}.ID
                WHERE e.SoldDate IS NOT NULL AND e.Price IS NOT NULL
                  AND e.SoldDate > NOW() - INTERVAL {queries.MARKET_STATS_DAYS} DAY
                  AND COALESCE(e.Quantity, 1) = 1 AND {cond}
            """, tuple(values))
            prices = [float(r[0]) for r in pcur.fetchall()]
            if prices:
                stats = {"median": round(statistics.median(prices), 2),
                         "min": round(min(prices), 2), "max": round(max(prices), 2),
                         "n": len(prices)}

        cur.execute("""
            SELECT SurfacedAt, ROUND(SurfacedPrice / 100, 2) AS SurfacedPrice,
                   ROUND(AvgMarketPrice / 100, 2) AS AvgMarketPrice, DiscountPct,
                   BidCount AS BidsAtSurfacing,
                   ROUND(PredictedFinal / 100, 2) AS PredictedFinal,
                   ROUND(FinalPrice / 100, 2) AS FinalPrice,
                   EndedUnsold, GaveUp, NearMiss, ItemLocation, Epid,
                   CategoryPath, ItemCondition, EnrichNote, Watchers, Model
            FROM Scraper.DealOutcomes WHERE EbayID = %s
        """, (ebay_id,))
        outcome = cur.fetchone()

        pcur = conn.cursor()
        pcur.execute("""
            SELECT SnapAt, ROUND(EffPrice / 100, 2), Bids
            FROM Scraper.DealSnapshots WHERE EbayID = %s ORDER BY SnapAt
        """, (ebay_id,))
        snapshots = [[_iso_utc(t), float(p), int(b or 0)] for t, p, b in pcur.fetchall()]

        prediction = None
        # No prediction for Buy-It-Now — the asking price IS the final price;
        # snipe premiums only model bidding dynamics.
        if category and listing["SoldDate"] is None and listing["ListingType"] != 'bin':
            pcur.execute(queries.SNIPE_PREMIUM_QUERY)
            premiums = queries.median_ratios(pcur.fetchall())
            eff = float(listing["ItemPrice"] or 0) + float(listing["Shipping"] or 0)
            ratio, samples = queries.premium_for(
                premiums, category, int(listing["Bids"] or 0), _hours_left(listing["EndTime"]))
            if samples and eff > 0:
                prediction = {"final": round(eff * ratio, 2), "n": samples, "ratio": ratio}

        for col in ("EndTime", "SoldDate", "LastSeenAt"):
            if listing.get(col):
                listing[col] = _iso_utc(listing[col])
        if outcome and outcome.get("SurfacedAt"):
            outcome["SurfacedAt"] = _iso_utc(outcome["SurfacedAt"])

        label = queries.model_label_for_row(category, {**(attrs or {}), "Quantity": listing["Quantity"]}) if category else None

        return jsonify({"status": "ok", "listing": listing, "category": category,
                        "attrs": attrs, "group": group, "group_label": label,
                        "stats": stats, "outcome": outcome,
                        "snapshots": snapshots, "prediction": prediction})
    except Exception as e:
        log.error("deal_detail error: %s", e)
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
