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

# Notification gate on the outcome-calibrated prediction: skip the push when
# history says bidding will close the gap (predicted discount below this).
# Deals are still recorded in DealOutcomes either way — the validation dataset
# must include the ones the gate suppressed, or we can't tell if it works.
SURFACE_MIN_PREDICTED_DISCOUNT = float(os.environ.get('SURFACE_MIN_PREDICTED_DISCOUNT', '10'))

# Near-miss control cohort: also record listings in the
# [SURFACE_NEARMISS_DISCOUNT, SURFACE_MIN_DISCOUNT) band, flagged NearMiss=1
# and never notified. Their resolved outcomes validate the 20%% threshold.
# Set equal to SURFACE_MIN_DISCOUNT to disable the cohort.
SURFACE_NEARMISS_DISCOUNT = float(os.environ.get('SURFACE_NEARMISS_DISCOUNT', '12'))

# BIN watcher: a separate fast lane sweeping newly-listed Buy-It-Now items.
# Fixed prices can't be bid past their value, so a good BIN find is real the
# moment it's seen — and gone in minutes, hence a cadence faster than the full
# scrape. The discount bar is stricter than the auction feed's: every find
# pings a phone immediately, so it has to be worth the interruption.
# Enabled/interval/threshold live in EbayScraper.GetBinConfig(): the Settings
# page (AppConfig) wins, BIN_SCAN_MINUTES / BIN_MIN_DISCOUNT / BIN_ENABLED
# env vars are the defaults.

# Data-quality audit: re-parse every stored row through the current parser to
# catch classification/lot pollution + price outliers BEFORE they skew a
# median, and push an HA alert when findings cross the threshold. Full-table
# scan, so its own slow cadence. See EbayScraper.audit_data_quality.
AUDIT_INTERVAL_HOURS = float(os.environ.get('AUDIT_INTERVAL_HOURS', '6'))
AUDIT_ALERT_THRESHOLD = int(os.environ.get('AUDIT_ALERT_THRESHOLD', '15'))

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
    "NVIDIA GTX 16",
    "NVIDIA RTX 20",
    "NVIDIA RTX 30",
    "NVIDIA RTX 40",
    "NVIDIA RTX 50",
    "AMD RX 5000",
    "AMD RX 6000",
    "AMD RX 7000",
    "AMD RX 9000",
    "Intel Arc",
]

CPU_QUERY_LIST = [
    "Intel Core i3",
    "Intel Core i5",
    "Intel Core i7",
    "Intel Core i9",
    "Intel Xeon E3",
    "Intel Xeon E5",
    "Intel Xeon Silver",
    "Intel Xeon Gold",
    "AMD Ryzen 3",
    "AMD Ryzen 5",
    "AMD Ryzen 7",
    "AMD Ryzen 9",
]

HDD_QUERY_LIST = [
    "SAS hard drive TB",
    "SATA hard drive TB",
    # Widen the auction pool: the HDD funnel is inventory-starved (~113 live
    # vs ~280+ for other categories) because used drives skew Buy-It-Now.
    "NAS hard drive TB",
    "enterprise hard drive",
    "hard drive 8TB",
    "hard drive 12TB",
    # Job lots — parsed with a per-unit quantity and valued against the
    # single-item medians (lots themselves never enter the market stats).
    # Two phrasings: the TB-anchored one misses small-capacity lots
    # ("Job Lot 6x 2.5\" Hard Drives 500GB").
    "hard drive job lot TB",
    "hard drives job lot",
]

SSD_QUERY_LIST = [
    "NVMe M.2 SSD",
    "SATA 2.5 SSD",
    "SSD 2TB",
    "SSD job lot",
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
_last_bin_scan: datetime | None = None
_last_audit: datetime | None = None

# Maps str(ebay_id) → datetime of last targeted scrape for that item.
_last_targeted: dict = {}

# Exact end-time refinement: one item-page fetch per tracked deal once it's
# inside REFINE_UNDER_S — search countdowns truncate to the minute, but item
# pages embed the millisecond-exact "endDate". str(id) → True once attempted.
REFINE_UNDER_S = 3600
_refined: dict = {}

# Sub-minute one-shots timed against the refined end: a final look as close
# to the hammer as polling allows. (seconds_remaining, tag) — each fires once.
_FINAL_ONESHOTS = [(90, 't90'), (25, 't25')]
_oneshot_fired: dict = {}

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
    for row in deals:
        category = (row.get('_category') or '').upper()
        # Prediction gate: history says this one gets bid past its value —
        # don't wake anyone up for it. (Still recorded in DealOutcomes.)
        pred_disc = row.get('PredictedDiscountPct')
        if (row.get('PremiumSamples') and pred_disc is not None
                and pred_disc < SURFACE_MIN_PREDICTED_DISCOUNT):
            log.info(
                "Deal %s suppressed: predicted discount %.1f%% < %.0f%% gate "
                "(current %.1f%%, predicted final £%.2f)",
                row.get('ID'), pred_disc, SURFACE_MIN_PREDICTED_DISCOUNT,
                row.get('DiscountPct') or 0, row.get('PredictedFinalPrice') or 0,
            )
            continue
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
                if row.get('PremiumSamples'):
                    message += (f" · predicted final ~£{row['PredictedFinalPrice']:.0f}"
                                f" (n={row['PremiumSamples']})")
                requests.post(
                    f"{r['HaUrl'].rstrip('/')}/api/services/notify/{r['NotifyService']}",
                    headers={"Authorization": f"Bearer {r['HaToken']}"},
                    json={
                        "title": f"Deal: {label} £{price:.2f} ({disc:.0f}% off)",
                        "message": message,
                        "data": {"url": EbayScraper.deal_page_url(row.get('ID'), row.get('URL')),
                                 "tag": f"dealfinder-{row.get('ID')}"},
                    },
                    timeout=10,
                )
            except Exception as e:
                log.warning("Deal notification to %s failed for %s: %s",
                            r.get('Name'), row.get('ID'), e)


# A fixed price this far under market is almost never real — hijacked-account
# fakes, empty boxes, "read description" — and unlike auctions nothing corrects
# it. Shown flagged on the /bin page, but never worth waking a phone for.
BIN_SCAM_DISCOUNT = float(os.environ.get('BIN_SCAM_DISCOUNT', '60'))


def notify_bin_finds(finds: list) -> None:
    """Push notifications for new Buy-It-Now bargains — immediately and
    ungated: no bidding means no prediction, the listed price is final and
    the first person to click buy wins."""
    if not finds:
        return
    finds = [f for f in finds if float(f.get('DiscountPct') or 0) < BIN_SCAM_DISCOUNT]
    if not finds:
        return
    recipients = EbayScraper.GetNotifyRecipients()
    if not recipients:
        return
    filters = EbayScraper.GetBinConfig().get('filters') or {}
    for row in finds:
        category = (row.get('_category') or '').upper()
        # Model filter from the BIN watcher settings — e.g. HDD "6TB, 8TB,
        # 10TB" silences every other capacity. Finds are still recorded in
        # BinNotified either way (a filter change must not replay old finds).
        if not EbayScraper.bin_find_passes_filters(row.get('_label'), category, filters):
            log.info("BIN find %s (%s) silenced by model filter", row.get('ID'), row.get('_label'))
            continue
        for r in recipients:
            cats = [c.strip().upper() for c in (r.get('Categories') or '').split(',') if c.strip()]
            if category not in cats:
                continue
            try:
                label = row.get('_label') or 'find'
                price = float(row.get('CurrentPrice'))
                avg = float(row.get('AvgMarketPrice'))
                disc = float(row.get('DiscountPct'))
                qty = int(row.get('Quantity') or 1)
                message = f"Market avg £{avg:.2f}" + (f" × {qty} units" if qty > 1 else '')
                message += " · fixed price — first to buy wins"
                requests.post(
                    f"{r['HaUrl'].rstrip('/')}/api/services/notify/{r['NotifyService']}",
                    headers={"Authorization": f"Bearer {r['HaToken']}"},
                    json={
                        "title": f"BIN: {label} £{price:.2f} ({disc:.0f}% off)",
                        "message": message,
                        "data": {"url": EbayScraper.deal_page_url(row.get('ID'), row.get('URL')),
                                 "tag": f"dealfinder-bin-{row.get('ID')}"},
                    },
                    timeout=10,
                )
            except Exception as e:
                log.warning("BIN notification to %s failed for %s: %s",
                            r.get('Name'), row.get('ID'), e)


def run_bin_scan(min_discount: float):
    """Sweep newly-listed BIN items for all categories, then push new finds."""
    global _last_bin_scan
    _last_bin_scan = _utcnow()      # set first: a crashing scan must not hot-loop
    log.info("Starting BIN scan (min discount %.0f%%)...", min_discount)
    for query_list, product_type in [
        (GPU_QUERY_LIST, 'GPU'),
        (CPU_QUERY_LIST, 'CPU'),
        (HDD_QUERY_LIST, 'HDD'),
        (SSD_QUERY_LIST, 'SSD'),
        (RAM_QUERY_LIST, 'RAM'),
    ]:
        try:
            EbayScraper.ScrapeBinAndUpload(query_list, product_type=product_type,
                                           country=SCRAPE_COUNTRY, condition=SCRAPE_CONDITION)
        except Exception as e:
            log.error("BIN scan %s failed: %s", product_type, e)
    try:
        notify_bin_finds(EbayScraper.SurfaceBinDeals(min_discount))
    except Exception as e:
        log.error("BIN surfacing failed: %s", e)
    log.info("BIN scan complete.")


def run_data_audit():
    """Re-parse every stored row through the current parser; alert on pollution
    (systems in a category, mislabelled lots, GPU 'lots') and price outliers
    before they skew a median. Read-only — never mutates data."""
    global _last_audit
    _last_audit = _utcnow()
    try:
        a = EbayScraper.audit_data_quality()
    except Exception as e:
        log.error("Data audit failed: %s", e)
        return
    rej, mis = len(a['reparse_rejects']), len(a['lot_mismatch'])
    gl, out = len(a['gpu_lots']), len(a['price_outliers'])
    log.info("Data audit: %d parser-reject, %d lot-mismatch, %d gpu-lot, %d price-outlier",
             rej, mis, gl, out)
    problems = []
    if gl:
        problems.append(f"{gl} GPU row(s) wrongly marked as lots")
    if rej >= AUDIT_ALERT_THRESHOLD:
        problems.append(f"{rej}+ row(s) the parser would now reject (a gate is leaking)")
    if mis >= AUDIT_ALERT_THRESHOLD:
        problems.append(f"{mis}+ mislabelled lot quantities")
    if out >= AUDIT_ALERT_THRESHOLD:
        problems.append(f"{out}+ sold rows priced way above their group median")
    if not problems:
        return
    # one example per finding type, so the push is actionable
    ex = (a['gpu_lots'] or a['reparse_rejects'] or a['price_outliers'] or [{}])[0]
    body = "; ".join(problems)
    sample = ex.get('title') or ex.get('label') or ''
    log.warning("Data audit ALERT: %s | e.g. %s", body, sample)
    for r in EbayScraper.GetNotifyRecipients():
        try:
            requests.post(
                f"{r['HaUrl'].rstrip('/')}/api/services/notify/{r['NotifyService']}",
                headers={"Authorization": f"Bearer {r['HaToken']}"},
                json={"title": "PC/DEALS data audit",
                      "message": f"{body}. e.g. {sample}"[:230],
                      "data": {"tag": "dealfinder-audit"}},
                timeout=10,
            )
        except Exception as e:
            log.warning("Audit alert to %s failed: %s", r.get('Name'), e)


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
    EbayScraper.reset_field_coverage()
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
        (SSD_QUERY_LIST, 'SSD'),
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
        new_deals = EbayScraper.SurfaceDeals(SURFACE_WINDOW_HOURS, SURFACE_MIN_DISCOUNT,
                                             nearmiss_discount=SURFACE_NEARMISS_DISCOUNT)
        notify_new_deals(new_deals)
    except Exception as e:
        log.error("Deal surfacing failed: %s", e)

    # User price alerts (created on model pages) — checked once per full
    # scrape, when the data they watch has just been refreshed.
    try:
        EbayScraper.EvaluateAlerts()
    except Exception as e:
        log.error("Alert evaluation failed: %s", e)

    # Housekeeping: drop zombie active listings (ended >14d, never resolved,
    # not deal-tracked). Sold rows are never pruned — they're the price history.
    try:
        EbayScraper.PruneStaleListings(days=14)
    except Exception as e:
        log.error("Stale-listing prune failed: %s", e)

    _last_full_scrape = _utcnow()
    coverage = EbayScraper.get_field_coverage()
    alerts = EbayScraper.coverage_alerts(coverage)
    try:
        EbayScraper.RecordScrapeCompleted(stats={
            'rows': total_rows,
            'categories_ok': categories_ok,
            'coverage': coverage,
            'alerts': alerts,
        })
    except Exception as e:
        log.error("Failed to record scrape timestamp: %s", e)

    # Heartbeat only when the run was genuinely healthy: at least one category
    # succeeded AND at least one row was inserted/updated. A healthy hourly run
    # always touches rows (existing active listings get re-upserted), so zero
    # rows means the parser or every fetch is broken — let Kuma flag it.
    # Field-coverage collapse ALSO withholds the heartbeat: partial markup
    # drift (a single field's parser going blind, like the old silent-£0
    # shipping bug) is invisible to the row count.
    for a in alerts:
        log.error("Field-coverage alert: %s — eBay markup has likely drifted "
                  "for this field. Kuma heartbeat withheld.", a)
    kuma_heartbeat(
        ok=(categories_ok > 0 and total_rows > 0 and not alerts),
        msg=f"full scrape ok: {total_rows} rows across {categories_ok}/5 categories",
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

    live_keys = set()
    for ebay_id, category, title, end_time in active_deals:
        key = str(ebay_id)
        live_keys.add(key)
        seconds_remaining = (end_time - now).total_seconds()

        # One-time exact end refinement once inside the targeted window.
        if 0 < seconds_remaining <= REFINE_UNDER_S and key not in _refined:
            _refined[key] = True
            exact = EbayScraper.RefineEndTime(ebay_id)
            if exact is not None:
                end_time = exact
                seconds_remaining = (end_time - now).total_seconds()

        # Sub-minute one-shots (fire once each, timed on the exact end).
        fired = _oneshot_fired.setdefault(key, set())
        oneshot_due = False
        for threshold_s, tag in _FINAL_ONESHOTS:
            if 0 < seconds_remaining <= threshold_s and tag not in fired:
                fired.add(tag)
                oneshot_due = True
        if oneshot_due:
            items_to_scrape.append((ebay_id, category, title))
            _last_targeted[key] = now
            continue

        minutes_remaining = seconds_remaining / 60

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

    # Drop bookkeeping for deals no longer live.
    for d in (_last_targeted, _refined, _oneshot_fired):
        for k in [k for k in d if k not in live_keys]:
            del d[k]

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

    # Dual-VRAM GPU models: split historical rows into per-variant groups.
    try:
        EbayScraper.EnsureGpuVramSplit()
    except Exception as e:
        log.error("GPU VRAM split migration failed: %s", e)

    # LastSeenAt: freshness stamp so cancelled listings drop out of deals.
    try:
        EbayScraper.EnsureLastSeenColumn()
    except Exception as e:
        log.error("LastSeenAt migration failed: %s", e)

    # FirstSeenAt: first-insert stamp powering the BIN feed's "added within" window.
    try:
        EbayScraper.EnsureFirstSeenColumn()
    except Exception as e:
        log.error("FirstSeenAt migration failed: %s", e)

    # Strip tracking params from stored listing URLs (new rows are
    # canonicalised at parse time).
    try:
        EbayScraper.EnsureCanonicalUrls()
    except Exception as e:
        log.error("URL canonicalisation failed: %s", e)

    # DealOutcomes columns the scheduler itself writes.
    try:
        EbayScraper.EnsureOutcomeColumns()
    except Exception as e:
        log.error("Outcome column migration failed: %s", e)

    # RAM kit composition (2x8 vs 1x16 markets) + title backfill.
    try:
        EbayScraper.EnsureRamKitConfig()
    except Exception as e:
        log.error("RAM KitConfig migration failed: %s", e)

    # SSD satellite table (new category).
    try:
        EbayScraper.EnsureSsdTable()
    except Exception as e:
        log.error("SSD table setup failed: %s", e)

    # ReserveNotMet flag (item-page enrichment).
    try:
        EbayScraper.EnsureEnrichmentColumns()
    except Exception as e:
        log.error("Enrichment column migration failed: %s", e)

    # HasBin / HasBestOffer flags (item-page purchase routes → price advisor).
    try:
        EbayScraper.EnsureOfferColumns()
    except Exception as e:
        log.error("Offer column migration failed: %s", e)

    # EndTimeExact flag (exact end refinement from item pages).
    try:
        EbayScraper.EnsureEndTimeExact()
    except Exception as e:
        log.error("EndTimeExact migration failed: %s", e)

    # Deal price-trajectory table (feeds future time-aware premiums).
    try:
        EbayScraper.EnsureDealSnapshots()
    except Exception as e:
        log.error("DealSnapshots setup failed: %s", e)

    # Notification recipients table (+ bootstrap the default recipient from env).
    try:
        EbayScraper.EnsureNotifyRecipients()
    except Exception as e:
        log.error("NotifyRecipients setup failed: %s", e)

    # BIN watcher: EBAY.ListingType + the once-only notification table.
    try:
        EbayScraper.EnsureListingType()
        EbayScraper.EnsureBinNotified()
    except Exception as e:
        log.error("BIN watcher setup failed: %s", e)

    # Run full scrape immediately on startup so data is fresh before the first interval.
    run_full_scrape()

    while True:
        # 10 s tick: sub-minute one-shots need finer scheduling than the
        # old 60 s heartbeat. Idle ticks cost one small DB query.
        time.sleep(10)
        now = _utcnow()

        # Full scrape: due if interval has elapsed since last run.
        if _last_full_scrape is None or \
                (now - _last_full_scrape) >= timedelta(minutes=FULL_SCRAPE_INTERVAL_MINUTES):
            run_full_scrape()

        # Data-quality audit: catch classification/lot pollution before it
        # skews a median (own slow cadence — full-table re-parse).
        if _last_audit is None or \
                (now - _last_audit) >= timedelta(hours=AUDIT_INTERVAL_HOURS):
            run_data_audit()

        # BIN fast lane: sweep newly-listed fixed-price items between full
        # runs. Settings are re-read each tick (60s cache) so the Settings
        # page can retune or pause the lane without a restart.
        bin_cfg = EbayScraper.GetBinConfig()
        if bin_cfg['enabled'] and (_last_bin_scan is None or
                (now - _last_bin_scan) >= timedelta(minutes=bin_cfg['scan_minutes'])):
            run_bin_scan(bin_cfg['min_discount'])

        # Targeted scrapes: checked every loop tick.
        run_targeted_scrapes()
