"""One-off: resolve the ended-auction backlog left by eBay's sign-in wall.

From ~23 Jul 2026 the sold-search scrape and the per-ID verifier both saw
eBay's sign-in page, so (a) no ended auction got a sold price and (b) the
verifier closed tracked deals as "cancelled" after two misses. This walks both
pools through the item-page resolver, newest first, then exits:

  1. repair  — tracked deals wrongly closed unsold / gave up (item pages last
               ~60-90 days, so older ones come back 'gone' and are left as-is)
  2. backlog — untracked ended auctions (the market-median history)

Run DETACHED inside the scraper container (the SSH wrapper kills long execs):
    docker exec -d dealfinder-scraper sh -c \
        'python -u scripts/resolve_backlog.py > /tmp/backlog.log 2>&1'

Paced slower than the scheduler (it shares the IP), and waits out the
circuit breaker instead of hammering eBay. Idempotent — safe to re-run.
"""
import logging
import os
import sys
import time

os.environ.setdefault('DIRECT_MIN_DELAY_S', '5')
os.environ.setdefault('DIRECT_JITTER_S', '4')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
import EbayScraper as E  # noqa: E402  (env must be set before import)

log = logging.getLogger('backlog')


def drain(repair: bool, max_age_days: int) -> dict:
    totals: dict = {}
    while True:
        stats = E.ResolveEndedAuctions(limit=50, min_age_minutes=15,
                                       max_age_days=max_age_days, repair=repair)
        for k, v in stats.items():
            totals[k] = totals.get(k, 0) + v
        wait = E._breaker_open_until - time.monotonic()
        if wait > 0:
            log.warning("circuit open — sleeping %ds", int(wait) + 5)
            time.sleep(wait + 5)
            continue
        if stats['checked'] + stats['failed'] == 0:
            return totals
        log.info("%s running totals: %s", 'repair' if repair else 'backlog',
                 {k: v for k, v in totals.items() if v})


if __name__ == '__main__':
    log.info("repair pass done: %s", drain(repair=True, max_age_days=75))
    log.info("backlog pass done: %s", drain(repair=False, max_age_days=14))
