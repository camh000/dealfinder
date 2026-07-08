import time
import logging
import signal
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import sys
import os

load_dotenv("credentials.env")


def _utcnow() -> datetime:
    """Naive UTC now — the single time frame the whole stack stores."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Local timezone for human-facing notification text only.
_LOCAL_TZ = ZoneInfo(os.environ.get('TZ', 'Europe/London'))

# Add parent dir to path so EbayScraper is importable
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import EbayScraper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────

# Hours after auction end before a targeted sold-listing search is run
# to resolve any outcomes the regular scraper missed.
OUTCOME_VERIFY_HOURS = int(os.environ.get('OUTCOME_VERIFY_HOURS', '6'))

# Days after auction end before a still-unresolved outcome is permanently
# marked as gave-up (GaveUp=1) and excluded from future retries.
OUTCOME_GIVE_UP_DAYS = int(os.environ.get('OUTCOME_GIVE_UP_DAYS', '7'))

# Minutes between full query-list scrapes.
FULL_SCRAPE_INTERVAL_MINUTES = int(os.environ.get('FULL_SCRAPE_INTERVAL_MINUTES', '60'))

# eBay search parameters — override per-deployment without a code change.
# Accepted values match EbayScraper.countryDict / conditionDict / typeDict.
SCRAPE_COUNTRY      = os.environ.get('SCRAPE_COUNTRY',      'uk')
SCRAPE_CONDITION    = os.environ.get('SCRAPE_CONDITION',    'used')
SCRAPE_LISTING_TYPE = os.environ.get('SCRAPE_LISTING_TYPE', 'auction')

# Uptime Kuma push monitor URL. Pinged ONLY after a full scrape that touched
# at least one row — so eBay markup drift, total fetch failure or a dead DB
# all read as "down" in Kuma instead of rotting silently.
KUMA_PUSH_URL = os.environ.get('KUMA_PUSH_URL', '')

# Home Assistant push notifications for newly surfaced deals (all optional —
# leave unset to disable). HA_NOTIFY_SERVICE is the part after 'notify.'.
HA_URL            = os.environ.get('HA_URL', '').rstrip('/')
HA_TOKEN          = os.environ.get('HA_TOKEN', '')
HA_NOTIFY_SERVICE = os.environ.get('HA_NOTIFY_SERVICE', '')

# Server-side deal surfacing parameters (mirrors the UI defaults).
SURFACE_WINDOW_HOURS = int(os.environ.get('SURFACE_WINDOW_HOURS', '2'))
SURFACE_MIN_DISCOUNT = float(os.environ.get('SURFACE_MIN_DISCOUNT', '20'))

# Targeted-scrape tiers: (threshold_minutes, interval_minutes)
# When a tracked deal has <= threshold_minutes remaining, scrape it every interval_minutes.
# Evaluated in ascending threshold order — first matching tier wins.
# Deals with > 60 min remaining are covered by the hourly full scrape.
_TARGETED_TIERS = [
    (5,  1),   # < 5 min remaining  → every 1 min
    (15, 5),   # < 15 min remaining → every 5 min
    (60, 15),  # < 60 min remaining → every 15 min
]

# ── Query lists ────────────────────────────────────────────────────────────────

GPU_QUERY_LIST = [
    "NVIDIA GTX 9",
    "NVIDIA GTX 10",
    "NVIDIA RTX 20",
    "NVIDIA RTX 30",
    "NVIDIA RTX 40",
    "AMD RX 5000",
    "AMD RX 6000",
    "AMD RX 7000",
]

CPU_QUERY_LIST = [
    "Intel Core i3",
    "Intel Core i5",
    "Intel Core i7",
    "Intel Core i9",
    "AMD Ryzen 3",
    "AMD Ryzen 5",
    "AMD Ryzen 7",
    "AMD Ryzen 9",
]

HDD_QUERY_LIST = [
    "SAS hard drive TB",
    "SATA hard drive TB",
    # Job lots — parsed with a per-unit quantity and valued against the
    # single-item medians (lots themselves never enter the market stats).
    "hard drive job lot TB",
]

RAM_QUERY_LIST = [
    "8gb ddr3 ram",
    "16gb ddr3 ram",
    "8gb ddr4 ram",
    "16gb ddr4 ram",
    "32gb ddr4 ram",
    "16gb ddr5 ram",
    "32gb ddr5 ram",
    "64gb ddr5 ram",
]

# ── Scheduler state ────────────────────────────────────────────────────────────

_last_full_scrape: datetime | None = None

# Maps str(ebay_id) → datetime of last targeted scrape for that item.
_last_targeted: dict = {}

# ── Scrape functions ───────────────────────────────────────────────────────────

def notify_new_deals(deals: list) -> None:
    """Push Home Assistant notifications for newly surfaced deals.

    Recipients + per-recipient category filters live in the NotifyRecipients
    table (managed from the dashboard's SETTINGS tab). Each enabled recipient
    gets one notification per new deal in a category they've opted into.
    """
    if not deals:
        return
    recipients = EbayScraper.GetNotifyRecipients()
    if not recipients:
        return
    # Outcome-calibrated premiums: median FinalPrice/SurfacedPrice per
    # (category, bid-bucket). Fetched once per batch; {} when history is thin.
    premiums = EbayScraper.GetSnipePremiums()
    for row in deals:
        category = (row.get('_category') or '').upper()
        for r in recipients:
            cats = [c.strip().upper() for c in (r.get('Categories') or '').split(',') if c.strip()]
            if category not in cats:
                continue
            try:
                label = row.get('_label') or 'deal'
                price = float(row.get('CurrentPrice'))
                avg = float(row.get('AvgMarketPrice'))
                disc = float(row.get('DiscountPct'))
                bids = row.get('Bids') or 0
                end = row.get('EndTime')
                if hasattr(end, 'strftime'):
                    # Stored UTC-naive; show the recipient local wall time.
                    end_txt = end.replace(tzinfo=timezone.utc).astimezone(_LOCAL_TZ).strftime('%H:%M')
                else:
                    end_txt = str(end)
                message = f"Market avg £{avg:.2f} · {bids} bid(s) · ends {end_txt}"
                entry = (premiums.get((category, EbayScraper._bid_bucket(bids)))
                         or premiums.get((category, 'all')))
                if entry:
                    ratio, n = entry
                    message += f" · predicted final ~£{price * ratio:.0f} (n={n})"
                requests.post(
                    f"{r['HaUrl'].rstrip('/')}/api/services/notify/{r['NotifyService']}",
                    headers={"Authorization": f"Bearer {r['HaToken']}"},
                    json={
                        "title": f"Deal: {label} £{price:.2f} ({disc:.0f}% off)",
                        "message": message,
                        "data": {"url": row.get('URL'), "tag": f"dealfinder-{row.get('ID')}"},
                    },
                    timeout=10,
                )
            except Exception as e:
                log.warning("Deal notification to %s failed for %s: %s",
                            r.get('Name'), row.get('ID'), e)


def kuma_heartbeat(ok: bool, msg: str) -> None:
    """Ping the Uptime Kuma push monitor. Only called for healthy runs."""
    if not KUMA_PUSH_URL or not ok:
        return
    try:
        requests.get(KUMA_PUSH_URL, params={"status": "up", "msg": msg[:250]}, timeout=10)
    except Exception as e:
        log.warning("Kuma heartbeat failed: %s", e)


def run_full_scrape():
    """Run the full query-list scrape for all categories + outcome verification."""
    global _last_full_scrape
    log.info("Starting full scrape run...")
    # Fresh curl-cffi session per full run so Akamai cookies are re-established.
    EbayScraper.reset_direct_session()
    common = dict(
        country=SCRAPE_COUNTRY,
        condition=SCRAPE_CONDITION,
        listing_type=SCRAPE_LISTING_TYPE,
        cache=False,
    )
    total_rows = 0
    categories_ok = 0
    for query_list, product_type in [
        (GPU_QUERY_LIST, 'GPU'),
        (CPU_QUERY_LIST, 'CPU'),
        (HDD_QUERY_LIST, 'HDD'),
        (RAM_QUERY_LIST, 'RAM'),
    ]:
        try:
            log.info("Scraping %s...", product_type)
            inserted, updated = EbayScraper.ScrapeAndUpload(query_list, product_type=product_type, **common)
            total_rows += inserted + updated
            categories_ok += 1
            log.info("%s scrape complete.", product_type)
        except Exception as e:
            log.error("%s scrape failed: %s", product_type, e)

    # Verify outcomes for items past their end time that are still unresolved.
    try:
        EbayScraper.VerifyPendingOutcomes(hours_after=OUTCOME_VERIFY_HOURS, give_up_days=OUTCOME_GIVE_UP_DAYS)
    except Exception as e:
        log.error("Outcome verification failed: %s", e)

    # Server-side deal surfacing + push notifications — deals are captured
    # and alerted even when nobody has the dashboard open.
    try:
        new_deals = EbayScraper.SurfaceDeals(SURFACE_WINDOW_HOURS, SURFACE_MIN_DISCOUNT)
        notify_new_deals(new_deals)
    except Exception as e:
        log.error("Deal surfacing failed: %s", e)

    # Housekeeping: drop zombie active listings (ended >14d, never resolved,
    # not deal-tracked). Sold rows are never pruned — they're the price history.
    try:
        EbayScraper.PruneStaleListings(days=14)
    except Exception as e:
        log.error("Stale-listing prune failed: %s", e)

    _last_full_scrape = _utcnow()
    try:
        EbayScraper.RecordScrapeCompleted()
    except Exception as e:
        log.error("Failed to record scrape timestamp: %s", e)

    # Heartbeat only when the run was genuinely healthy: at least one category
    # succeeded AND at least one row was inserted/updated. A healthy hourly run
    # always touches rows (existing active listings get re-upserted), so zero
    # rows means the parser or every fetch is broken — let Kuma flag it.
    kuma_heartbeat(
        ok=(categories_ok > 0 and total_rows > 0),
        msg=f"full scrape ok: {total_rows} rows across {categories_ok}/4 categories",
    )
    if categories_ok > 0 and total_rows == 0:
        log.error(
            "Full scrape touched 0 rows — eBay markup may have changed "
            "(parser drift) or all fetches are being blocked. Kuma heartbeat withheld."
        )
    log.info("Full scrape run complete.")


def run_targeted_scrapes():
    """Check active tracked deals and run targeted per-item scrapes as needed."""
    global _last_targeted

    active_deals = EbayScraper.GetActiveDeals()
    if not active_deals:
        return

    # DB EndTimes are UTC-naive — compare in the same frame (datetime.now()
    # here previously skewed every countdown by an hour during BST).
    now = _utcnow()
    items_to_scrape = []

    for ebay_id, category, title, end_time in active_deals:
        minutes_remaining = (end_time - now).total_seconds() / 60

        if minutes_remaining <= 0:
            # Already ended — WHERE clause should exclude these, but guard defensively.
            continue

        # Find applicable tier (ascending threshold list — first match wins).
        applicable_interval = None
        for threshold_mins, interval_mins in _TARGETED_TIERS:
            if minutes_remaining <= threshold_mins:
                applicable_interval = interval_mins
                break

        if applicable_interval is None:
            # > 60 min remaining — covered by the hourly full scrape.
            continue

        key = str(ebay_id)
        last_scraped = _last_targeted.get(key)

        if last_scraped is None or (now - last_scraped) >= timedelta(minutes=applicable_interval):
            items_to_scrape.append((ebay_id, category, title))
            _last_targeted[key] = now

    if items_to_scrape:
        log.info(
            "Targeted scrapes triggered for %d item(s): %s",
            len(items_to_scrape),
            [str(i[0]) for i in items_to_scrape],
        )
        try:
            EbayScraper.ScrapeTargeted(items_to_scrape)
        except Exception as e:
            log.error("Targeted scrape failed: %s", e)
    else:
        log.debug("Targeted scrapes: no items due yet (%d active deal(s) checked)", len(active_deals))


# ── Main loop ──────────────────────────────────────────────────────────────────

def _handle_sigterm(signum, frame):
    """Exit promptly and cleanly on docker stop (we run as PID 1)."""
    log.info("Received signal %s — shutting down.", signum)
    raise SystemExit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    log.info(
        "Scheduler starting — full scrape every %d min; targeted tiers: %s",
        FULL_SCRAPE_INTERVAL_MINUTES,
        _TARGETED_TIERS,
    )

    # One-time schema migration: EBAY.Shipping (pence) for postage-inclusive pricing.
    try:
        EbayScraper.EnsureShippingColumn()
    except Exception as e:
        log.error("Shipping column migration failed: %s", e)

    # Sub-type attributes: HDD Internal/External + RAM DIMM/SODIMM (+ backfill).
    try:
        EbayScraper.EnsureCategoryAttributes()
    except Exception as e:
        log.error("Category attribute migration failed: %s", e)

    # Job-lot support: EBAY.Quantity (+ HDD title backfill so historical lot
    # sales stop polluting the single-unit medians).
    try:
        EbayScraper.EnsureQuantityColumn()
    except Exception as e:
        log.error("Quantity column migration failed: %s", e)

    # Seller feedback columns (fill in as listings are re-scraped).
    try:
        EbayScraper.EnsureSellerFeedbackColumns()
    except Exception as e:
        log.error("Seller feedback migration failed: %s", e)

    # Notification recipients table (+ bootstrap the default recipient from env).
    try:
        EbayScraper.EnsureNotifyRecipients()
    except Exception as e:
        log.error("NotifyRecipients setup failed: %s", e)

    # Run full scrape immediately on startup so data is fresh before the first interval.
    run_full_scrape()

    while True:
        time.sleep(60)
        now = _utcnow()

        # Full scrape: due if interval has elapsed since last run.
        if _last_full_scrape is None or \
                (now - _last_full_scrape) >= timedelta(minutes=FULL_SCRAPE_INTERVAL_MINUTES):
            run_full_scrape()

        # Targeted scrapes: checked every loop tick (every 60 s).
        run_targeted_scrapes()
