import time
import logging
from apscheduler.schedulers.blocking import BlockingScheduler
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

# Hours after auction end before a targeted sold-listing search is run
# to resolve any outcomes the regular scraper missed. Configurable via env var.
OUTCOME_VERIFY_HOURS = int(os.environ.get('OUTCOME_VERIFY_HOURS', '6'))

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

def run_scraper():
    log.info("Starting scrape run...")
    # Fresh curl-cffi session per run so Akamai cookies are re-established.
    EbayScraper.reset_direct_session()
    common = dict(country='uk', condition='used', listing_type='auction', cache=False)
    for query_list, product_type in [
        (GPU_QUERY_LIST, 'GPU'),
        (CPU_QUERY_LIST, 'CPU'),
        (HDD_QUERY_LIST, 'HDD'),
    ]:
        try:
            log.info(f"Scraping {product_type}...")
            EbayScraper.ScrapeAndUpload(query_list, product_type=product_type, **common)
            log.info(f"{product_type} scrape complete.")
        except Exception as e:
            log.error(f"{product_type} scrape failed: {e}")

    # Verify outcomes for items past their end time that are still unresolved.
    try:
        EbayScraper.VerifyPendingOutcomes(hours_after=OUTCOME_VERIFY_HOURS)
    except Exception as e:
        log.error("Outcome verification failed: %s", e)

if __name__ == "__main__":
    log.info("Scheduler starting — running immediately then every 30 minutes.")
    run_scraper()

    scheduler = BlockingScheduler()
    scheduler.add_job(run_scraper, 'interval', minutes=30)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")
