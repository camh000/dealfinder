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

QUERY_LIST = [
    "NVIDIA GTX 9",
    "NVIDIA GTX 10",
    "NVIDIA RTX 20",
    "NVIDIA RTX 30",
    "NVIDIA RTX 40",
    "AMD RX 5000",
    "AMD RX 6000",
    "AMD RX 7000"
]

def run_scraper():
    log.info("Starting scrape run...")
    try:
        EbayScraper.ScrapeAndUpload(
            QUERY_LIST,
            product_type='GPU',
            country='uk',
            condition='used',
            listing_type='auction',
            cache=False
        )
        log.info("Scrape run complete.")
    except Exception as e:
        log.error(f"Scrape run failed: {e}")

if __name__ == "__main__":
    log.info("Scheduler starting — running immediately then every 30 minutes.")
    run_scraper()

    scheduler = BlockingScheduler()
    scheduler.add_job(run_scraper, 'interval', minutes=30)
    scheduler.start()
