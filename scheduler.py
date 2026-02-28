import time
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
import sys
import os

load_dotenv("credentials.env")

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
]

# ── Scheduler state ────────────────────────────────────────────────────────────

_last_full_scrape: datetime | None = None

# Maps str(ebay_id) → datetime of last targeted scrape for that item.
_last_targeted: dict = {}

# ── Scrape functions ───────────────────────────────────────────────────────────

def run_full_scrape():
    """Run the full query-list scrape for all categories + outcome verification."""
    global _last_full_scrape
    log.info("Starting full scrape run...")
    # Fresh curl-cffi session per full run so Akamai cookies are re-established.
    EbayScraper.reset_direct_session()
    common = dict(country='uk', condition='used', listing_type='auction', cache=False)
    for query_list, product_type in [
        (GPU_QUERY_LIST, 'GPU'),
        (CPU_QUERY_LIST, 'CPU'),
        (HDD_QUERY_LIST, 'HDD'),
    ]:
        try:
            log.info("Scraping %s...", product_type)
            EbayScraper.ScrapeAndUpload(query_list, product_type=product_type, **common)
            log.info("%s scrape complete.", product_type)
        except Exception as e:
            log.error("%s scrape failed: %s", product_type, e)

    # Verify outcomes for items past their end time that are still unresolved.
    try:
        EbayScraper.VerifyPendingOutcomes(hours_after=OUTCOME_VERIFY_HOURS, give_up_days=OUTCOME_GIVE_UP_DAYS)
    except Exception as e:
        log.error("Outcome verification failed: %s", e)

    _last_full_scrape = datetime.now()
    try:
        EbayScraper.RecordScrapeCompleted()
    except Exception as e:
        log.error("Failed to record scrape timestamp: %s", e)
    log.info("Full scrape run complete.")


def run_targeted_scrapes():
    """Check active tracked deals and run targeted per-item scrapes as needed."""
    global _last_targeted

    active_deals = EbayScraper.GetActiveDeals()
    if not active_deals:
        return

    now = datetime.now()
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

if __name__ == "__main__":
    log.info(
        "Scheduler starting — full scrape every %d min; targeted tiers: %s",
        FULL_SCRAPE_INTERVAL_MINUTES,
        _TARGETED_TIERS,
    )

    # Run full scrape immediately on startup so data is fresh before the first interval.
    run_full_scrape()

    while True:
        time.sleep(60)
        now = datetime.now()

        # Full scrape: due if interval has elapsed since last run.
        if _last_full_scrape is None or \
                (now - _last_full_scrape) >= timedelta(minutes=FULL_SCRAPE_INTERVAL_MINUTES):
            run_full_scrape()

        # Targeted scrapes: checked every loop tick (every 60 s).
        run_targeted_scrapes()
