import re
import time
import logging
import requests
import urllib.parse
from bs4 import BeautifulSoup
import os.path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import mariadb
import os
from dataclasses import dataclass
from typing import Optional

import queries

log = logging.getLogger(__name__)

load_dotenv("credentials.env")

countryDict = {
    'au': '.com.au',
    'at': '.at',
    'be': '.be',
    'ca': '.ca',
    'ch': '.ch',
    'de': '.de',
    'es': '.es',
    'fr': '.fr',
    'hk': '.com.hk',
    'ie': '.ie',
    'it': '.it',
    'my': '.com.my',
    'nl': '.nl',
    'nz': '.co.nz',
    'ph': '.ph',
    'pl': '.pl',
    'sg': '.com.sg',
    'uk': '.co.uk',
    'us': '.com',
}

conditionDict = {
    'all': '',
    'new': '&LH_ItemCondition=1000',
    'opened': '&LH_ItemCondition=1500',
    'refurbished': '&LH_ItemCondition=2500',
    'used': '&LH_ItemCondition=3000'
}

typeDict = {
    'all': '&LH_All=1',
    'auction': '&LH_Auction=1',
    'bin': '&LH_BIN=1',
    'offers': '&LH_BO=1'
}

# Timezone in which eBay renders "time-end" strings for anonymous scrapes
# (no account/cookies -> eBay defaults to US Pacific). Parsed wall-clock times
# are made tz-aware in this zone and converted to UTC, so DST transitions on
# both sides come from the tz database. The previous fixed +7h offset was
# only correct during Pacific daylight time - an hour wrong every winter.
EBAY_DISPLAY_TZ = os.environ.get('EBAY_DISPLAY_TZ', 'America/Los_Angeles')

# Persistent curl-cffi session — reused across requests within a scrape run so
# that Akamai cookies set on the homepage warmup are carried to search requests.
# Call reset_direct_session() before each run to get a fresh identity.
_direct_session = None

# Full browser header set that Akamai inspects.  curl-cffi sets the TLS/HTTP2
# fingerprint; we supply the application-layer headers to match.
_DIRECT_HEADERS_BASE = {
    'Accept': (
        'text/html,application/xhtml+xml,application/xml;q=0.9,'
        'image/avif,image/webp,image/apng,*/*;q=0.8,'
        'application/signed-exchange;v=b3;q=0.7'
    ),
    'Accept-Language':          'en-GB,en-US;q=0.9,en;q=0.8',
    'Accept-Encoding':          'gzip, deflate, br',
    'Sec-Fetch-Dest':           'document',
    'Sec-Fetch-Mode':           'navigate',
    'Sec-Fetch-User':           '?1',
    'Upgrade-Insecure-Requests':'1',
    'DNT':                      '1',
}


def _close_direct_session() -> None:
    """Close the current curl-cffi session (if any) and drop the reference.

    Explicitly closing releases libcurl handles/connections — older curl-cffi
    releases leaked memory when sessions were abandoned without close().
    """
    global _direct_session
    if _direct_session is not None:
        try:
            _direct_session.close()
        except Exception:
            pass
        _direct_session = None


def reset_direct_session() -> None:
    """Discard the current curl-cffi session.

    Call at the start of each scrape run so a fresh Akamai identity
    (new cookies, new TLS session) is established via the homepage warmup.
    """
    _close_direct_session()


def _fetch_direct(url: str) -> str | None:
    """Fetch URL via a persistent curl-cffi session impersonating Chrome 131.

    On first call (or after reset_direct_session()), warms up by fetching the
    eBay homepage so Akamai bot-detection cookies (_abck, bm_sz, etc.) are
    established before any search request.

    Returns HTML string on success, or None if the request fails or the
    response looks like a bot-detection / block page.
    """
    global _direct_session

    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        log.warning("curl_cffi not installed — skipping direct fetch")
        return None

    # Initialise session + homepage warmup once per scrape run.
    if _direct_session is None:
        _direct_session = cffi_requests.Session(impersonate='chrome120')
        try:
            warmup = _direct_session.get(
                'https://www.ebay.co.uk/',
                headers={
                    **_DIRECT_HEADERS_BASE,
                    'Sec-Fetch-Site':  'none',
                    'Accept-Encoding': 'gzip, deflate',  # exclude br: homepage sends brotli
                },                                       # which fails on Windows libcurl (curl 23)
                timeout=15,
            )
            log.info(
                "Direct session warmed up (HTTP %s, %d cookies)",
                warmup.status_code, len(_direct_session.cookies),
            )
        except Exception as e:
            log.warning("Session warmup failed: %s", e)

    try:
        resp = _direct_session.get(
            url,
            headers={
                **_DIRECT_HEADERS_BASE,
                'Referer':        'https://www.ebay.co.uk/',
                'Sec-Fetch-Site': 'same-origin',
            },
            timeout=30,
        )
        if resp.status_code != 200:
            log.warning("Direct fetch: HTTP %s for %s", resp.status_code, url)
            return None
        html = resp.text
        # Real eBay search pages are >1 MB; block/CAPTCHA pages are tiny.
        if len(html) < 50_000:
            log.warning(
                "Direct fetch: response too small (%d chars) — possible block page", len(html)
            )
            _close_direct_session()  # session may be flagged; reset for next call
            return None
        log.info("Direct fetch OK (curl-cffi/chrome131, %d chars)", len(html))
        return html
    except Exception as e:
        log.warning("Direct fetch failed: %s", e)
        _close_direct_session()
        return None


def _fetch_zyte(url: str) -> str | None:
    """Fetch URL via Zyte API — pay-per-use fallback when direct fetch is blocked.

    Uses httpResponseBody mode (raw HTTP response, no JS rendering).
    eBay search pages are server-rendered HTML so JS execution is not required.
    Approx cost: $1.8 per 1,000 successful requests (no monthly fee).

    HTTP 520 (Zyte transient error) is retried with exponential back-off up to
    ZYTE_MAX_RETRIES attempts (default 3, sleeps 2 s / 4 s / 8 s between tries).

    If Akamai still blocks via Zyte (response too small), switch the payload to:
        {"url": url, "browserHtml": True, "geolocation": "GB"}
    and decode with resp.json()["browserHtml"] (no base64). Cost ~$9/1k.
    """
    import base64
    api_key = os.environ.get("ZYTE_API_KEY")
    if not api_key:
        log.warning("Zyte API key not configured — skipping Zyte fetch")
        return None

    max_retries = int(os.environ.get('ZYTE_MAX_RETRIES', '3'))

    # Redact query string before logging — search terms / future auth tokens
    # shouldn't be shipped to log aggregators.
    _parts = urllib.parse.urlsplit(url)
    _safe_url = f"{_parts.scheme}://{_parts.netloc}{_parts.path}"

    for attempt in range(max_retries):
        try:
            log.info("Fetching via Zyte API: %s", _safe_url)
            resp = requests.post(
                "https://api.zyte.com/v1/extract",
                auth=(api_key, ""),
                json={
                    "url": url,
                    "httpResponseBody": True,
                    "geolocation": "GB",
                },
                timeout=60,
            )

            if resp.status_code == 520:
                backoff = 2 ** (attempt + 1)
                log.warning(
                    "Zyte HTTP 520 (attempt %d/%d) — backing off %ds before retry",
                    attempt + 1, max_retries, backoff,
                )
                if attempt < max_retries - 1:
                    time.sleep(backoff)
                continue

            resp.raise_for_status()
            html = base64.b64decode(resp.json()["httpResponseBody"]).decode("utf-8", errors="replace")
            if len(html) < 50_000:
                log.warning("Zyte response too small (%d chars) — possible block page", len(html))
                return None
            log.info("Fetched via Zyte (%d chars)", len(html))
            return html

        except Exception as e:
            log.error("Zyte fetch failed: %s", e)
            return None

    log.error("Zyte fetch failed: HTTP 520 persisted after %d attempt(s)", max_retries)
    return None


def __GetHTML(query, country, condition='', listing_type='all', alreadySold=True, cache=False):
    # alreadySold values:
    #   True        → sold listings only       (&LH_Complete=1&LH_Sold=1)
    #   'completed' → all completed listings   (&LH_Complete=1)   sold + ended-unsold
    #   'new_first' → active, newest first     (&_sop=10)  BIN watcher: good
    #                 fixed-price bargains go in minutes, so recency IS the filter
    #   False       → active listings          (&_sop=1)
    if alreadySold == 'completed':
        cache_suffix = 'completed'
        alreadySoldString = '&LH_Complete=1'
    elif alreadySold == 'new_first':
        cache_suffix = 'new_first'
        alreadySoldString = '&_sop=10'
    elif alreadySold:
        cache_suffix = 'sold'
        alreadySoldString = '&LH_Complete=1&LH_Sold=1'
    else:
        cache_suffix = 'active'
        alreadySoldString = '&_sop=1'

    cache_file = f"{query}_{cache_suffix}.txt"

    if cache and os.path.isfile(cache_file):
        with open(cache_file, "r", encoding="utf-8") as f:
            responseHTML = f.read()
    else:
        parsedQuery = urllib.parse.quote(query).replace('%20', '+')
        url = (
            f'https://www.ebay{countryDict[country]}/sch/i.html?_from=R40&_nkw={parsedQuery}'
            f'{alreadySoldString}{conditionDict[condition]}{typeDict[listing_type]}'
        )
        log.debug("Fetching: %s", url)

        responseHTML = _fetch_direct(url) or _fetch_zyte(url)
        if responseHTML is None:
            raise RuntimeError(f"All fetch methods failed for: {url}")

        if cache:
            with open(cache_file, "w", encoding='utf-8') as f:
                f.write(responseHTML)

    return BeautifulSoup(responseHTML, 'html.parser')

# ── component sub-type classification ─────────────────────────────────────────
# External HDDs (portable/USB enclosures) sell for very different money than bare
# internal drives, and laptop SODIMM sticks differ from desktop DIMMs. These are
# module-level so the scraper, the backfill and the tests share one definition.

_HDD_EXTERNAL_RE = re.compile(
    r'\b(external|portable|my\s*passport|my\s*book|elements|expansion|'
    r'backup\s*plus|one\s*touch|canvio|easystore|game\s*drive|lacie|'
    r'enclosure|ext\.?\s*hdd|extern)\b', re.IGNORECASE)


def classify_drive_type(title: str) -> str:
    """'External' for portable/USB-enclosure drives, else 'Internal' (the default)."""
    return 'External' if _HDD_EXTERNAL_RE.search(title or '') else 'Internal'


# Flash media (USB sticks, SD cards) masquerades as storage in both the HDD
# and SSD searches — one shared skip list.
_FLASH_MEDIA_KEYWORDS = ['flash drive', 'memory stick', 'pen drive', 'pendrive',
                         'thumb drive', 'usb stick', 'sd card', 'micro sd', 'microsd']

# eBay's search is fuzzy across storage: "SSD job lot" returns SAS-HDD
# clearouts, and HDD searches return SSDs titled "Solid State Hard Drive".
# Cross-classified rows land in the wrong market's medians AND deal feed, so
# each branch must reject the other kind. Solid-state markers win when a
# title has both ("Solid State Hard Drive" is an SSD; "SAS SSD" is an SSD).
_SOLID_STATE_RE = re.compile(
    r'\bssd\b|solid[\s-]?state|\bnvme\b|\bm[.\s]?2\b|\bsshd\b', re.IGNORECASE)
_SPINNING_DISK_RE = re.compile(
    r'\bhdd\b|hard\s*dis[kc]|hard\s*drive|\brpm\b|\bsas\b', re.IGNORECASE)


def title_is_solid_state(title: str) -> bool:
    """True when a title carries any SSD/NVMe marker (incl. SSHD hybrids —
    those are skipped from BOTH storage categories)."""
    return bool(_SOLID_STATE_RE.search(title or ''))


def title_is_spinning_disk(title: str) -> bool:
    """True when a title carries spinning-drive markers (HDD/SAS/RPM/...)."""
    return bool(_SPINNING_DISK_RE.search(title or ''))


# Memory modules masquerade as storage/GPUs by capacity alone ("64GB DDR3
# Server RAM Kit" once landed as a 64GB drive; a SODIMM part number yielded a
# GPU "model"). Two strictness levels:
#  - storage: ANY memory token disqualifies — no drive title ever says DDR5,
#    PC4-3200, DIMM or MT/s ("Corsair Vengeance 64GB (2x32GB) DDR5 6000MHz"
#    has no 'ram' noun at all and still isn't a drive).
#  - GPU: needs the token NEAR a memory noun, because old cards legitimately
#    write VRAM as "2GB DDR3". \bddr\d\b never matches GDDR6 (no word
#    boundary inside GDDR), so modern VRAM specs are safe either way.
# no trailing \b after the PC-number: part codes run straight into letter
# suffixes ("PC4-3200AA")
_MEMORY_TOKEN = r'\bddr\d\b|\bpc[2-5]l?-?\d{3,5}|\b(?:so|r|u|lr)dimm\b|\bdimm\b|\bmt/s\b'
_MEMORY_TOKEN_RE = re.compile(_MEMORY_TOKEN, re.IGNORECASE)
_MEMORY_MODULE_RE = re.compile(
    rf'(?:{_MEMORY_TOKEN})[^,;]{{0,40}}?\b(?:ram|dimm|sodimm|rdimm|udimm|memory)\b|'
    rf'\b(?:ram|dimm|sodimm|rdimm|udimm|memory)\b[^,;]{{0,40}}?(?:{_MEMORY_TOKEN})',
    re.IGNORECASE)


def title_has_memory_token(title: str) -> bool:
    """True when a title carries any RAM-spec token (storage-branch guard)."""
    return bool(_MEMORY_TOKEN_RE.search(title or ''))


def title_is_memory_module(title: str) -> bool:
    """True for RAM-stick/kit titles (memory token near a memory noun)."""
    return bool(_MEMORY_MODULE_RE.search(title or ''))


# A component listing that names a CPU, a storage drive, or a laptop/prebuilt
# product line is a whole SYSTEM, not the part. These are the strong signals
# that catch systems whose titles carry no "laptop"/"PC" word at all — the
# RTX 3050 median was polluted by "Lenovo IdeaPad 5 PRO with RTX 3050" (£313,
# no keyword) and "ASUS TUF Gaming A15 Ryzen 5 512GB SSD" (£277). A bare
# graphics card / drive / RAM stick never names any of these.
_CPU_MENTION_RE = re.compile(
    r'\b(?:intel\s+)?core\s*i[3579]\b|\bi[3579][\s\-]\d{3,5}[a-z]{0,2}\b|'
    r'\bryzen\b|\bxeon\b|\bcore\s+ultra\b|\bceleron\b|\bpentium\b|'
    r'\bcore\s*2\s+(?:duo|quad)\b|\bathlon\b', re.IGNORECASE)
# storage inside a NON-storage listing = a system's spec sheet. Requires an
# ssd/hdd/nvme/emmc noun so a GPU's "8GB GDDR6" VRAM never trips it.
_SYS_STORAGE_RE = re.compile(r'\d+\s*(?:tb|gb)\s*(?:ssd|nvme|hdd|emmc)\b', re.IGNORECASE)
# Laptop / prebuilt PRODUCT LINES that never name a bare component. Curated to
# dodge GPU-brand collisions: no 'TUF'/'Aorus'/'ProArt'/'Ventus'/'Gaming'.
# NB numeric-ending alternatives use \d+ (consume ALL digits): with a bare
# \d the outer \b fails on multi-digit models ("Precision 7750" matched only
# "precision 7", then \b before "750" failed → the laptop slipped through).
_SYSTEM_LINE_RE = re.compile(
    r'\b(?:ideapad|thinkpad|thinkbook|legion|yoga|'                 # Lenovo
    r'victus|omen|elitebook|probook|spectre|envy|zbook|'            # HP
    r'inspiron|latitude|alienware|precision\s*\d+|xps\s*\d+|'       # Dell
    r'dell\s+g\d+|'                                                 # Dell G-series gaming
    r'zephyrus|vivobook|zenbook|expertbook|rog\s+(?:strix\s+)?g\d+|' # Asus
    r'predator|aspire|nitro\s*\d+|'                                 # Acer
    r'katana|stealth\s+gs\d+|raider\s+ge\d+|'                       # MSI laptops
    r'macbook|surface\s+(?:laptop|book|pro))\b', re.IGNORECASE)


# ONLY unambiguous whole-machine nouns. Deliberately NOT "desktop pc" /
# "gaming pc" / "desktop computer" — a real component describes what it fits
# ("Hard Drive for Desktop PC", "GT 710 Graphics Card for Desktop PC"). A
# genuine system carrying one of those phrases also carries a CPU/RAM/storage
# spec, which the standalone tells below already catch.
_SYSTEM_PHRASES = (
    'gaming rig', 'gaming setup', 'pre-built', 'prebuilt', 'pc bundle',
    'pc build', 'all-in-one', 'barebones', 'complete pc', 'full pc',
    'custom pc', 'compact pc', 'tower pc', 'desktop tower', 'gaming tower',
    'mid tower', 'full tower', 'midi tower', ' nuc')


# Server / workstation CHASSIS model codes — a whole machine, whatever part
# it's listed under. Model tells only (Dell R450/R660, HP Z/XW, HPE DL/ML) so
# legit 'Xeon ... for Server' CPU lots and 'server RAM' modules survive.
_SERVER_CHASSIS_RE = re.compile(
    r'\bdell\s+r\d{2,3}\b|\br[2-9]\d0\b|\bhp[e]?\s+z\d{3}\b|\bxw\d{3,4}\b|'
    r'\b[dm]l\d{2,3}\b', re.IGNORECASE)


def title_is_system(title: str, category: str) -> bool:
    """True for whole-machine listings (laptops, prebuilts) masquerading as a
    part. Category-aware in three ways:
      • bare 'laptop'/'notebook' and laptop PRODUCT LINES are tells only for
        GPU — a desktop card never names a laptop, but a "laptop hard drive",
        "laptop SODIMM" or an SSD-for-a-Dell is a legitimate replacement part;
      • storage size is the product for the HDD/SSD branches, so it isn't a
        tell there;
      • for GPUs a "2GB DDR3" is VRAM, so the RAM tell needs the word 'ram'.
    A CPU name is always a tell — no bare component names a processor."""
    t = title or ''
    tl = t.lower()
    if category == 'gpu':
        if 'laptop' in tl or 'notebook' in tl:
            return True
        if _SYSTEM_LINE_RE.search(t):
            return True
        # 'gaming pc'/'desktop pc' are ambiguous fit-phrases EXCEPT on GPUs: a
        # real card calls itself a 'graphics card'/'video card', a machine
        # ('RTX 3070 Custom White Gaming PC') doesn't.
        if ('graphics card' not in tl and 'video card' not in tl
                and ('gaming pc' in tl or 'desktop pc' in tl)):
            return True
    if _CPU_MENTION_RE.search(t) or _SERVER_CHASSIS_RE.search(t):
        return True
    if category not in ('hdd', 'ssd') and _SYS_STORAGE_RE.search(tl):
        return True
    ram_pat = r'\d+\s*gb\s+ram\b' if category == 'gpu' else r'\d+\s*gb\s*(?:ddr\d|ram)\b'
    if re.search(ram_pat, tl):
        return True
    if any(k in tl for k in _SYSTEM_PHRASES):
        return True
    return False


_RAM_SODIMM_RE = re.compile(
    r'so.?dimm|small\s*outline|\blaptop\b|\bnotebook\b', re.IGNORECASE)


def classify_ram_form_factor(title: str) -> str:
    """'SODIMM' for laptop/small-outline modules, else 'DIMM' (desktop, the default)."""
    return 'SODIMM' if _RAM_SODIMM_RE.search(title or '') else 'DIMM'


# Kit composition is a real market dimension: at the same total capacity,
# fewer/bigger sticks command a premium (2x8 DDR4-16GB sold ~31% above 1x16
# and ~56% above 4x4 on 120d data) — upgrade headroom and platform age.
_RAM_KIT_RE = re.compile(r'(\d+)\s*[xX×]\s*(\d+)\s*GB', re.IGNORECASE)
_RAM_KIT_REV_RE = re.compile(r'(\d+)\s*GB\s*[xX×]\s*(\d+)', re.IGNORECASE)


def extract_ram_kit(title: str):
    """('2x8', 16) — kit config + total GB; (None, None) when unstated.
    Handles both '2x8GB' and '8GB x2' orderings; implausible stick counts
    or sizes are treated as unstated rather than guessed."""
    m = _RAM_KIT_RE.search(title or '')
    if m:
        sticks, size = int(m.group(1)), int(m.group(2))
    else:
        m = _RAM_KIT_REV_RE.search(title or '')
        if not m:
            return None, None
        size, sticks = int(m.group(1)), int(m.group(2))
    if 1 <= sticks <= 8 and 1 <= size <= 128:
        return f"{sticks}x{size}", sticks * size
    return None, None


# GPU models genuinely sold in two memory variants with different markets.
# For these, VRAM becomes part of the model label ("RTX 3060 12GB") so each
# variant prices against its own comparables — a 3060 12GB judged against a
# blended 8/12GB median can look like a phantom deal (or hide a real one).
# Listings of these models WITHOUT a parseable VRAM keep the bare name, which
# groups them into a thin bucket that never reaches the 5-sold stats floor —
# safer excluded than mispriced. Keys match extract_model() output exactly.
# Models that shipped in more than one VRAM size at materially different
# prices, so the size must be part of the market group. Chosen from the sold
# data (models actually blended with a >=~20% median gap) + their known
# same-split siblings — cheap old cards whose 2/4GB variants sell within a few
# pounds are deliberately left blended (splitting only thins the group).
# NB some entries (RTX 4070 TI 16GB, RX 7900 XT 24GB) mainly isolate the
# higher SKU (Ti SUPER / XTX) that mislabelled listings blend in.
_DUAL_VRAM_MODELS = {
    'GTX 1060':     (3, 6),
    'RTX 2060':     (6, 12),
    'RTX 3050':     (6, 8),
    'RTX 3060':     (8, 12),
    'RTX 3080':     (10, 12),
    'RTX 4060 TI':  (8, 16),
    'RTX 4070 TI':  (12, 16),
    'RTX 5060 TI':  (8, 16),
    'RX 470':       (4, 8),
    'RX 480':       (4, 8),
    'RX 570':       (4, 8),
    'RX 580':       (4, 8),
    'RX 5500 XT':   (4, 8),
    'RX 7600':      (8, 16),
    'RX 7900 XT':   (20, 24),
    'RX 9060 XT':   (8, 16),
    'ARC A770':     (8, 16),
}


def qualify_gpu_model(model: str | None, vram: int | None) -> str | None:
    """Append the VRAM size to dual-memory-variant models ('RTX 3060 12GB')."""
    if model and vram and vram in _DUAL_VRAM_MODELS.get(model, ()):
        return f"{model} {vram}GB"
    return model


# ── job-lot detection ──────────────────────────────────────────────────────────
# Multi-unit listings ("5 x 4TB", "job lot of 10") are valued per unit against
# the single-item market stats, and excluded from those stats (bulk discounts
# are structural — letting lots in would drag the median down and mask the
# very discount being hunted). HDD-only for now: drives are homogeneous and
# quantity sits in the title; RAM kit notation (2x8GB) already means ONE kit
# of summed capacity, so NxM there must NOT be read as a lot.

# Quantity must sit adjacent to a capacity token or an explicit lot phrase —
# a stray number misread as a quantity inflates a valuation N-fold and
# manufactures a phantom mega-deal. (?!\.\d) rejects form factors: the 3 in
# 'lot of 3.5" drives' is not a quantity — and in the trailing-x patterns the
# x must stand alone ((?<=[\s,\-])): model codes end in X ("WD30EZRX 3.5")
# and once parsed a lone drive as a x3 lot.
_LOT_QTY_PATTERNS = [
    re.compile(r'\b(\d{1,2})\s*[x×]\s*\d+(?:\.\d+)?\s*(?:tb|gb)\b', re.IGNORECASE),          # 5 x 4TB
    re.compile(r'\b(?:job\s*lot|joblot|lot|bundle|pack)\s+of\s+(\d{1,2})(?!\.\d)\b', re.IGNORECASE),  # job lot of 10
    re.compile(r'\b\d+(?:\.\d+)?\s*(?:tb|gb)\b[^,;(]{0,20}?(?<=[\s,\-])[x×]\s*(\d{1,2})(?!\.\d)\b', re.IGNORECASE),   # 4TB ... x5
    re.compile(r'\b(?:job\s*lot|joblot|lot|bundle)\b[^,;(]{0,15}?(?<=[\s,\-])[x×]\s*(\d{1,2})(?!\.\d)\b', re.IGNORECASE),  # job lot x6
    re.compile(r'\b(?:job\s*lot|joblot|lot|bundle)\b[^,;(]{0,15}?\b(\d{1,2})\s*[x×](?![A-Za-z0-9])', re.IGNORECASE),  # job lot 5x 2.5" drives
    # Quantity-first titles: "20x Assorted 2TB ... JOB LOT". Anchored to the
    # title START — a leading count is how sellers head a lot listing, and
    # the anchor keeps model codes / "2x faster" marketing mid-title out.
    re.compile(r'^\s*(\d{1,2})\s*[x×](?![A-Za-z0-9])', re.IGNORECASE),
    # Trailing "x2 Units" / "x4 drives": the unit noun right after the count
    # makes this unambiguous wherever it sits in the title. 'SSD'/'HDD' are
    # deliberately NOT accepted nouns: "PCIe Gen3 x4 SSD" is a lane width.
    re.compile(r'(?<=[\s,\-.])[x×]\s*(\d{1,2})\s*(?:units?|drives?|sticks?|pcs?|pieces?)\b', re.IGNORECASE),
]

# PCIe lane specs ("PCIe Gen3 x4", "PCIe 3.0 x4 NVMe SSD") look exactly like
# the capacity-then-xN lot form, and so does Seagate's Exos family naming
# ("Seagate 18TB Exos X18" is one drive, not eighteen). A match whose span
# mentions either is a spec, not a count.
_LOT_LANE_RE = re.compile(r'pcie|\bgen\s*\d|\blanes?\b|\bexos\b', re.IGNORECASE)

# Strip family tokens like "Exos X18" down to "Exos" before quantity matching
# — belt for orderings the span guard can't see.
_MODEL_FAMILY_X_RE = re.compile(r'\b(exos)\s*x\s*\d+\b', re.IGNORECASE)

# Above this a "quantity" is more likely a misparse than a real lot; treat the
# listing as a single so it prices itself out of the deal feed (false negative
# beats a phantom N-fold valuation). 40-drive enterprise clearouts are real,
# so the cap sits above them.
LOT_MAX_QTY = 50

_LOT_RISK_RE = re.compile(
    r'\buntested\b|\bspares?\b|\brepairs?\b|\bfaulty\b|\bnot\s+working\b|'
    r'\bfor\s+parts\b|\bas[-\s]is\b|\bdead\b|\bdamaged\b|\bbroken\b|'
    r'\bnon[-\s]?functional\b', re.IGNORECASE)

# Accessory listings masquerading as the component: "RTX 4090 Founders
# Edition Heatsink, with fans and box (no GPU)" parses as a 4090 and shows
# as 98% off. Only explicit tells — a real card saying "with backplate"
# must not be skipped. Tuned against real false positives from the DB audit:
# "CPU plus Heatsink and Fan" is a CPU with its cooler, "Card, with box,
# manual and..." is a boxed card, "(Dog not included)" is a joke — so
# "not included" must name the component, the heatsink-bundle tell must not
# follow plus/with/incl/+, and box-and-manual only counts with an "only".
_ACCESSORY_RE = re.compile(
    r'no\s+gpu\b|\b(?:gpu|card|cpu|processor|drive)\s+not\s+included\b|'
    r'\bno\s+(?:graphics\s+)?card\b|\bempty\s+box\b|'
    r'\b(?:box|heatsink|cooler|fans?|shroud|backplate|bracket|stand)\s*only\b|'
    r'(?<!plus )(?<!with )(?<!incl )(?<!\+ )\bheatsink\s*(?:&|and|\+|,)\s*(?:box|fans?|shroud)\b|'
    r'\bbox\s*(?:&|and|\+|,)\s*(?:manual|heatsink)[\w\s]{0,12}?\bonly\b|'
    # connectivity accessories sold under the card's name ("Dell SLI Bridge
    # T77002 for GTX 1080" surfaced at 72% off a 1080)
    r'\bsli\s*bridge\b|\bnvlink\s*bridge\b|\b(?:pcie?|gpu)\s*riser\b|\briser\s*cable\b',
    re.IGNORECASE)


# Drive-lot sellers head the title with the LOT's total capacity — either in
# parens "(9.6TB) 8x Seagate 1.2TB SAS" or bare "24TB 6x Toshiba 4TB" — which
# then (a) becomes the parsed capacity (it's the first TB token) and (b) pushes
# the "8x" count off the ^ anchor so the lot quantity is missed. Stripping that
# leading total fixes both: the per-drive size becomes the capacity and the
# count leads the title again. The BARE form only strips when a "Nx" count
# immediately follows, so a plain single-drive "4TB Seagate…" is untouched.
_LEADING_TOTAL_RE = re.compile(
    r'^\s*(?:'
    r'\(\s*\d+(?:\.\d+)?\s*(?:TB|GB)\s*\)'                    # (9.6TB)
    r'|\d+(?:\.\d+)?\s*(?:TB|GB)(?=\s*\d{1,2}\s*[x×])'        # 24TB 6x…
    r')\s*', re.IGNORECASE)


def strip_leading_total(title: str) -> str:
    """Drop a leading lot-total prefix ("(9.6TB)" or "24TB 6x…") so per-drive
    capacity and the quantity-first count parse correctly. No-op otherwise."""
    return _LEADING_TOTAL_RE.sub('', title or '', count=1)


# A drive title never names a GPU — "geforce"/"radeon"/a bare RTX/GTX/Quadro
# token means a graphics card or a whole gaming machine leaked into HDD/SSD
# (its "2TB" is the system's drive, not the product). title_is_system catches
# most, but a bare GPU listing ("NVIDIA Quadro RTX 8000 48GB") isn't a system.
# \brtx[\s-]*[a-z]?\d catches both consumer "RTX 3090" and workstation
# "RTX A5000" (A-series) — a Lenovo P17 laptop with an RTX A5000 slipped the
# \brtx\d form and parsed its "128GB-RAM" as a 128GB drive.
_GPU_TELL_RE = re.compile(r'geforce|radeon|\bgtx\b|\bquadro\b|\brtx[\s-]*[a-z]?\d', re.IGNORECASE)


# ── Motherboard parsing ─────────────────────────────────────────────────────
# Chipset vocabulary comes straight from queries._CHIPSET_SOCKET (longest-first
# so "X670E" beats "X670"). A full prebuilt PC also lists a motherboard, so a
# storage drive, a GPU model or a build/OS keyword disqualifies the listing —
# a bare board or a CPU+mobo bundle names none of those.
# Optional trailing letter captures board-name variants ("B450M", "B660M") where
# the letter is the form factor, not part of the chipset. Longest-first ordering
# keeps "X670E"/"B650E" from being read as "X670"+E / "B650"+E.
_MOBO_CHIPSET_RE = re.compile(r'\b(' + '|'.join(queries.CHIPSETS) + r')[A-Za-z]?\b', re.IGNORECASE)
_MOBO_SYSTEM_RE = re.compile(
    r'\bgaming pc\b|\bdesktop pc\b|\btower pc\b|\bpre-?built\b|\bwindows\s*1[01]\b|'
    r'\b\d{3,4}\s*gb\s+ssd\b|\b\d+\s*tb\s+ssd\b|\b\d{3,4}\s*gb\s+hdd\b|\b\d+\s*tb\s+hdd\b',
    re.IGNORECASE)
_MOBO_BRANDS = ['ASROCK', 'ASUS', 'ROG', 'GIGABYTE', 'AORUS', 'MSI', 'BIOSTAR',
                'EVGA', 'NZXT', 'COLORFUL', 'SUPERMICRO', 'FOXCONN']


def extract_chipset(title: str) -> str | None:
    m = _MOBO_CHIPSET_RE.search(title or '')
    return m.group(1).upper() if m else None


def extract_mobo_form_factor(title: str) -> str:
    """ATX by default — unstated boards are almost always full ATX; the smaller
    (pricier) form factors are near-always called out in the title."""
    t = (title or '').lower()
    if 'e-atx' in t or 'eatx' in t or 'extended atx' in t:
        return 'E-ATX'
    if 'mini-itx' in t or 'mini itx' in t or re.search(r'\bitx\b', t):
        return 'ITX'
    if any(k in t for k in ('micro-atx', 'micro atx', 'matx', 'm-atx', 'µatx', 'uatx')):
        return 'mATX'
    return 'ATX'


def extract_mobo_brand(title: str) -> str:
    u = (title or '').upper()
    for b in _MOBO_BRANDS:
        if b in u:
            return {'ROG': 'Asus', 'AORUS': 'Gigabyte'}.get(b, b.title())
    return ''


# A CPU+motherboard bundle needs an EXPLICIT pairing signal — "bundle", "combo",
# or "<join> motherboard" — not just a chipset mention (a bare CPU listing often
# says "compatible with B550 motherboards"). Paired with a chipset + a CPU model,
# this marks the listing for dual CPU/MOBO membership.
_BUNDLE_RE = re.compile(
    r'\bbundle\b|\bcombo\b|'
    r'(?:\+|&|\band\b|\bwith\b|\bincl(?:uding|\.)?\b|\binc\b)\s*(?:a\s+)?'
    r'(?:motherboard|mother\s*board|mobo|mainboard|m/?board)\b',
    re.IGNORECASE)


def is_cpu_mobo_bundle(title: str) -> bool:
    return bool(_BUNDLE_RE.search(title or ''))


def extract_lot_quantity(title: str) -> int:
    """Number of units in a multi-item listing; 1 when not confidently a lot."""
    t = _MODEL_FAMILY_X_RE.sub(r'\1', title or '')
    for pat in _LOT_QTY_PATTERNS:
        m = pat.search(t)
        if m and not _LOT_LANE_RE.search(m.group(0)):
            qty = int(m.group(1))
            if 2 <= qty <= LOT_MAX_QTY:
                return qty
    return 1


def title_capacity_values(title: str) -> set:
    """Distinct storage capacities named in a title, normalised to GB.

    More than one distinct value in an HDD/SSD BIN title means a
    "choose your capacity" variation listing (or an inherently ambiguous
    mixed lot) — the card's single price can't be matched to a spec.
    '1TB (1000GB)' normalises to ONE value and stays fine.
    """
    vals = set()
    for num, unit in re.findall(r'(\d+(?:\.\d+)?)\s*(TB|GB)\b', title or '', re.IGNORECASE):
        vals.add(int(float(num) * (1000 if unit.upper() == 'TB' else 1)))
    return vals


# Interface speeds ("12Gb/s", "6 Gbps") read as capacities by the regex above —
# strip them before counting distinct sizes for the mixed-lot test.
_IFACE_SPEED_RE = re.compile(r'\d+\s*gb\s*/?\s*s(?:ec)?\b|\d+\s*gbps\b', re.IGNORECASE)


def is_mixed_capacity_lot(title: str) -> bool:
    """A job lot of drives in 3+ different sizes ("12TB … 2x 2TB, 2x 3TB, 2x
    1TB") — no single capacity fits and it can't be valued per unit, so skip it.
    Only applies to actual lots: a 'choose your capacity' variation (quantity 1)
    is a single drive handled by the price-range flag, not a mixed lot. A clean
    lot names at most the total + one per-unit size (two values)."""
    if extract_lot_quantity(title) <= 1:
        return False
    return len(title_capacity_values(_IFACE_SPEED_RE.sub('', title or ''))) >= 3


def lot_is_risky(title: str) -> bool:
    """True for untested/spares-or-repairs/damaged wording — the market median
    assumes working units, so these must not be valued against it. Applied to
    every listing at parse time (not just lots): a *DAMAGED* 4090 at £565 vs
    a £1,787 working median is a phantom 68%-off deal, and its sold price
    would drag the working-card median down."""
    return bool(_LOT_RISK_RE.search(title or ''))


def is_accessory_listing(title: str) -> bool:
    """True for boxes/heatsinks/brackets sold under the component's name."""
    return bool(_ACCESSORY_RE.search(title or ''))


def __ParseItems(soup, query, productType):
    rawItems = soup.find_all('div', {'class': 'su-card-container su-card-container--horizontal'})
    if not rawItems:
        log.warning("No items found for query '%s' - eBay may have changed their HTML structure", query)
    data = []
    # eBay's search layout usually puts a "tile" ad / sponsored slot as the
    # first card in this container; it has a different inner structure and
    # never parses cleanly. Only skip it when it actually looks like the ad
    # slot (no /itm/ listing link) so a real first result isn't lost on pages
    # where eBay serves no ad.
    start_idx = 0
    if rawItems and rawItems[0].find('a', href=re.compile(r'/itm/\d+')) is None:
        start_idx = 1
    for item in rawItems[start_idx:]:

        # Get item data — skip item entirely if critical fields can't be parsed.
        # Each field tries the 2026-07 su-item-card markup first, then falls
        # back to the older s-card layout (eBay churns these class names every
        # few months and A/B-tests variants, so both must keep parsing).
        title = None
        title_el = item.find(class_='su-item-card__title')
        if title_el is not None:
            title = title_el.get_text(strip=True)
            if title.lower().startswith('new listing'):
                title = title[len('new listing'):].strip()
        else:
            try:
                spans = item.find(class_="s-card__title").find_all('span')
                if spans[0].get_text(strip=True) == "New listing":
                    title = spans[1].get_text(strip=True)
                else:
                    title = spans[0].get_text(strip=True)
            except (AttributeError, IndexError):
                pass
        if not title:
            log.warning("[%s] Skipping item - could not parse title", query)
            continue
        # Promoted "Shop on eBay" tiles carry a real-looking /itm/ link, so the
        # ad-slot guard above misses them; the placeholder title is the tell.
        if title == 'Shop on eBay':
            continue

        try:
            price_el = (item.find('span', {'class': 'su-item-card__price'})
                        or item.find('span', {'class': 's-card__price'}))
            price_text = price_el.get_text(strip=True)
            price = __ParseRawPrice(price_text)
            if price is None:
                raise ValueError("Price pattern not found in text")
        except (AttributeError, TypeError, ValueError) as e:
            log.warning("[%s] Skipping item '%s...' - could not parse price: %s", query, title[:40], e)
            continue
        # Multi-variation listings ("choose a capacity" dropdowns) show a
        # price RANGE ("£3.84 to £59.99") — the number we parse is the
        # CHEAPEST variant's, which has nothing to do with the title's spec.
        # Auctions can't have variations; the BIN path skips these entirely.
        price_is_range = bool(re.search(r'\d\s+to\s+£?\s*\d', price_text))

        shipping = __ExtractShipping(item)

        timeLeft = ""
        tl_el = item.find(class_="s-card__time-left")
        if tl_el is not None:
            timeLeft = tl_el.get_text(strip=True)
        else:
            # 2026-07 markup: "<n> bids · Time left <2m|1d 3h>" countdown div.
            cd = item.find(class_='su-bid-countdown')
            if cd is not None:
                m = re.search(r'time\s*left\s*((?:\d+\s*[dhms]\s*)+)',
                              cd.get_text(' ', strip=True), re.IGNORECASE)
                if m:
                    timeLeft = m.group(1).strip()

        timeEnd = None
        try:
            te_el = item.find(class_="s-card__time-end")
            if te_el is not None:
                timeEnd = parse_ebay_endtime(te_el.get_text(strip=True))
        except (AttributeError, TypeError):
            timeEnd = None
        if timeEnd is None and timeLeft:
            # 2026-07 markup shows only a relative countdown — derive the
            # absolute end from it. Minute resolution is fine: the targeted
            # scrape tiers refresh tracked items as the clock runs down.
            delta = parse_ebay_timeleft(timeLeft)
            if delta is not None:
                timeEnd = datetime.now(timezone.utc).replace(tzinfo=None) + delta

        # Sold date: new markup uses a "signal" chip ("Sold  8 Jul 2026");
        # older markup a positive styled-text span.
        soldDate = None
        for el in item.find_all('span', class_='signal'):
            t = el.get_text(strip=True)
            if t.startswith('Sold'):
                soldDate = parse_soldDate(t.removeprefix('Sold').strip())
                break
        if soldDate is None:
            try:
                t = item.find(class_="su-styled-text positive default").get_text(strip=True)
                soldDate = parse_soldDate(t.removeprefix('Sold').strip())
            except AttributeError:
                soldDate = None

        # Bid count: markup-agnostic — any span whose whole text is "N bids".
        bidCount = 0
        for el in item.find_all('span'):
            if re.fullmatch(r'\d+\s*bids?', el.get_text(strip=True)):
                bidCount = int("".join(filter(str.isdigit, el.get_text(strip=True))))
                break

        # Seller feedback — every card in both markups carries
        # "seller 100% positive (290)". A (0) count means a no-history
        # seller, not a bad one; consumers must gate on the count.
        feedback_pct = feedback_count = None
        fb = _FEEDBACK_RE.search(item.get_text(' ', strip=True))
        if fb:
            feedback_pct = float(fb.group(1))
            feedback_count = int(fb.group(2).replace(',', ''))

        try:
            reviewCount = int("".join(filter(str.isdigit, item.find(class_="s-item__reviews-count").find('span').get_text(strip=True))))
        except (AttributeError, TypeError, ValueError):
            reviewCount = 0
        
        try:
            a_tag = item.find('a')
            if a_tag is None:
                raise ValueError("No anchor tag found")
            url = a_tag['href']
            id_match = re.search(r'/itm/(\d+)', url)
            if id_match is None:
                raise ValueError(f"Could not extract item ID from URL: {url}")
            id = id_match.group(1)
            # Canonicalise: search-page hrefs carry ~800 chars of tracking
            # params, overflowing the VARCHAR(500) URL column (truncated
            # links). The bare /itm/<id> form is stable and sufficient.
            parts = urllib.parse.urlsplit(url)
            if parts.scheme and parts.netloc:
                url = f"{parts.scheme}://{parts.netloc}/itm/{id}"
            else:
                url = f"https://www.ebay.co.uk/itm/{id}"
        except (TypeError, KeyError, ValueError) as e:
            log.warning("[%s] Skipping item '%s...' - could not parse URL/ID: %s", query, title[:40], e)
            continue

        # Junk gate (all categories): damaged/untested/for-parts items can't
        # be valued against working-unit medians — as live listings they're
        # phantom deals, as sold history they drag the medians down. And
        # accessory listings ("Heatsink, with fans and box (no GPU)") aren't
        # the component at all.
        if lot_is_risky(title) or is_accessory_listing(title):
            log.debug("[%s] Skipping risky/accessory listing: %s", query, title[:60])
            continue

        socket = cores = capacity_gb = interface = form_factor = rpm = ram_type = speed = None
        drive_type = ram_format = pcie_gen = kit_config = chipset = None
        is_bundle = False
        quantity = 1

        if productType == 'GPU':

            BRANDS = [
                "ASUS", "MSI", "GIGABYTE", "ZOTAC", "PALIT",
                "EVGA", "PNY", "SAPPHIRE", "XFX", "INNO3D",
                "GAINWARD", "AORUS", "SPARKLE", "ASROCK", "ACER"
            ]

            # Flexible GPU model pattern. The variant MUST end on a word
            # boundary (\b) and be tried longest-first, or board-name words
            # get misread as a variant: "AORUS RTX 3090 XTREME" parsed as
            # "RTX 3090 XT" (XT is an AMD suffix; NVIDIA has no XT card), and
            # "RTX 4070 Ti SUPER" must beat the bare "Ti".
            model_pattern = re.compile(
                r'(?P<series>RTX|GTX|TITAN|RX)\s*'      # series
                r'(?P<number>\d{2,4})'                  # number
                r'(?:\s*(?P<variant>Ti\s*SUPER|SUPER|XTX|XT|Ti)\b)?',  # optional variant
                re.IGNORECASE
            )

            # Intel Arc: letter-prefixed numbers (A750, B580) — separate
            # pattern since the letter is part of the model, not a variant.
            arc_pattern = re.compile(r'\bARC\s*-?\s*([AB]\d{3})\b', re.IGNORECASE)

            # VRAM pattern
            vram_pattern = re.compile(r'(\d{1,2})\s*GB', re.IGNORECASE)

            def extract_model(title: str):
                m = arc_pattern.search(title)
                if m:
                    return f"ARC {m.group(1).upper()}"
                match = model_pattern.search(title)
                if match:
                    series = match.group('series').upper()
                    number = match.group('number')
                    variant = match.group('variant').upper().replace("  ", " ") if match.group('variant') else ""
                    return f"{series} {number} {variant}".strip()
                return None

            def extract_vram(title: str):
                match = vram_pattern.search(title)
                if match:
                    return int(match.group(1))
                return None

            def extract_brand(title: str):
                title_upper = title.upper()
                for brand in BRANDS:
                    if brand in title_upper:
                        return brand.title()
                # Intel detection (Arc) — before the AMD heuristics, which
                # would otherwise never be reached for an Intel card.
                if re.search(r'\bARC\b', title_upper) or "INTEL" in title_upper:
                    return "Intel"
                # AMD detection
                if "RX" in title_upper or "RADEON" in title_upper or "XT" in title_upper or "XTX" in title_upper:
                    return "AMD"
                return "NVIDIA"

            # Whole systems (gaming laptops, prebuilts) mention their card and
            # parse AS the card — an Alienware selling for £463 is not a GTX
            # 1080 sale. The shared detector catches them via laptop/prebuilt
            # product lines, a CPU name, or a storage drive — none of which a
            # bare graphics card ever states (its "8GB GDDR6" is VRAM, safe).
            if title_is_system(title, 'gpu'):
                log.debug("[%s] Skipping system listing in GPU: %s", query, title[:60])
                continue
            if title_is_memory_module(title):
                log.debug("[%s] Skipping memory module in GPU: %s", query, title[:60])
                continue

            vram  = extract_vram(title)
            model = qualify_gpu_model(extract_model(title), vram)
            brand = extract_brand(title)
        elif productType == 'CPU':

            # Drop complete-system listings (mini PCs, whole servers,
            # CPU+motherboard combos) that mention a CPU
            _tl = title.lower()
            _is_system = (
                any(k in _tl for k in ['mini pc', 'mini-pc', ' nuc', 'barebones',
                                        'desktop pc', 'all-in-one', 'laptop', 'notebook',
                                        'gaming pc', 'gaming computer', 'custom pc',
                                        'full pc', 'complete pc', 'pc bundle', 'pc build',
                                        'compact pc', 'prebuilt', 'pre-built',
                                        'desktop computer', 'gaming rig', 'gaming setup',
                                        'poweredge', 'proliant', 'supermicro', 'thinkserver',
                                        'thinksystem', 'primergy', 'vxrail', 'optiplex',
                                        'idrac', 'rack server', 'tower server', 'server bundle'])
                # server/workstation CHASSIS models (Dell R450/R660, HP Z/XW,
                # HPE DL/ML) — the audit found these leaking in as their Xeon.
                # NB model tells only, never bare "workstation"/"for Server" —
                # a "Xeon W-2145 Workstation CPU" is a CPU, and legit Xeon lots
                # say "Xeon ... for Server and Networking".
                or bool(_SERVER_CHASSIS_RE.search(title))
                # a bare CPU never names a DISCRETE graphics card — an
                # "i5-12400F ... RTX 3060 PC" is a prebuilt (the audit found
                # these). RTX/GTX/GeForce/Quadro/RX-4-digit only, so an APU's
                # "Radeon Vega Graphics" stays a legit CPU.
                or bool(re.search(r'\brtx\b|\bgtx\b|\bgeforce\b|\bquadro\b|'
                                  r'\brx\s?[5-9]\d00\b', _tl))
                # A bare CPU never SHIPS with storage or an OS. A drive spec
                # ("400GB SSD"), a Windows install ("Win 11 Pro") or the phrase
                # "Workstation PC" means a whole machine — HP Z-series and Dell
                # T-series workstations kept slipping through as their Xeon/i9.
                or bool(_SYS_STORAGE_RE.search(title))
                or bool(re.search(r'\bwin(?:dows)?\s*1[01]\b', _tl))
                or 'workstation pc' in _tl
                or (('motherboard' in _tl or ' mobo' in _tl)
                    and bool(re.search(r'\bddr\d|\bram\b', _tl)))
            )
            if _is_system:
                log.debug("[%s] Skipping system listing: %s", query, title[:60])
                continue

            # CPU job lots ("10x Intel Xeon Gold 6132", "Joblot x8 i5-4590")
            # are multi-unit — same per-unit treatment as HDD lots, so they're
            # marked ×N, priced per unit, and kept OUT of the single-unit
            # median (Cam: lot totals were counting as single Xeon sales).
            quantity = extract_lot_quantity(title)
            if quantity == 1:
                # dual-socket matched pairs mid-title ("Dell R720 2x Xeon E5")
                pair_m = re.search(r'\b(\d)\s*[x×]\s*(?:intel\s+)?xeon\b', _tl)
                if pair_m and 2 <= int(pair_m.group(1)) <= 8:
                    quantity = int(pair_m.group(1))

            def extract_cpu_brand(title: str):
                t = title.upper()
                if 'AMD' in t:
                    return 'AMD'
                if 'INTEL' in t or 'XEON' in t:
                    return 'Intel'
                return ''

            # AMD: "Ryzen 5 3400G", "Ryzen 9 7940HS", "Ryzen R9 7940HS" (R-prefix variant)
            amd_model_pattern = re.compile(
                r'Ryzen\s*(?:Threadripper\s*(?:PRO\s*)?)?R?(\d+)\s+(\d+[A-Z0-9]*)',
                re.IGNORECASE
            )

            # Intel: handles all of:
            #   "Core i5-6600K"  "i5 9400F"  "I5-6600K"  "i5 CPU 6500"  "i5 650"
            intel_model_pattern = re.compile(
                r'[iI]([3579])[\s\-](?:CPU\s+)?(\d{3,5}[A-Z0-9]*)',
                re.IGNORECASE
            )

            # Xeon families, in matching priority order:
            #   Scalable: "Xeon Silver 4114", "Gold 6248R", "Platinum 8168"
            #   E3/E5/E7: "Xeon E5-2680 v4" (the vN is part of the market —
            #             a 2680 v3 and v4 are different chips at different money)
            #   W/E/D dash: "Xeon W-2145", "E-2224G", "D-1541"
            #   Legacy:   "Xeon X5670", "L5640", "E5620", "W3680"
            xeon_scalable_pattern = re.compile(
                r'XEON\s+(BRONZE|SILVER|GOLD|PLATINUM)\s*-?\s*(\d{4}[A-Z]{0,2})', re.IGNORECASE)
            # Suffix letter only when not followed by a digit — "2690v3" is
            # model 2690 + revision v3, not model "2690V".
            xeon_e_pattern = re.compile(
                r'XEON\s+(E[357])[-\s](\d{3,4}(?:[A-Z](?!\d))?)(?:\s*(V\s*\d))?', re.IGNORECASE)
            xeon_dash_pattern = re.compile(
                r'XEON\s+([WED])-(\d{4,5}[A-Z]{0,2})', re.IGNORECASE)
            xeon_legacy_pattern = re.compile(
                r'XEON\s+([XLEW]\d{4}[A-Z]?)\b', re.IGNORECASE)

            def extract_cpu_model(title: str):
                # AMD — normalise to "Ryzen 9 7940HS"
                m = amd_model_pattern.search(title)
                if m:
                    return f"Ryzen {m.group(1)} {m.group(2).upper()}"
                # Intel Core — normalise to "i5-6600K"
                m = intel_model_pattern.search(title)
                if m:
                    return f"i{m.group(1)}-{m.group(2).upper()}"
                # Intel Xeon
                m = xeon_scalable_pattern.search(title)
                if m:
                    return f"Xeon {m.group(1).title()} {m.group(2).upper()}"
                m = xeon_e_pattern.search(title)
                if m:
                    v = f" {m.group(3).upper().replace(' ', '')}" if m.group(3) else ""
                    return f"Xeon {m.group(1).upper()}-{m.group(2).upper()}{v}"
                m = xeon_dash_pattern.search(title)
                if m:
                    return f"Xeon {m.group(1).upper()}-{m.group(2).upper()}"
                m = xeon_legacy_pattern.search(title)
                if m:
                    return f"Xeon {m.group(1).upper()}"
                return None

            socket_pattern = re.compile(r'(LGA\s*\d{3,4}|AM\s*[2345]|FM[12]|TR[X]?\d+)', re.IGNORECASE)

            def extract_socket(title: str):
                m = socket_pattern.search(title)
                if m:
                    return re.sub(r'\s+', '', m.group(0)).upper()
                return None

            cores_num_pattern = re.compile(r'(\d+)\s*[Cc]ore')
            cores_named_map   = {'dual':2,'triple':3,'quad':4,'hexa':6,'octa':8,'deca':10,'dodeca':12}

            def extract_cores(title: str):
                m = cores_num_pattern.search(title)
                if m:
                    return int(m.group(1))
                t = title.lower()
                for name, count in cores_named_map.items():
                    if name in t:
                        return count
                return None

            brand  = extract_cpu_brand(title)
            model  = extract_cpu_model(title)
            vram   = None
            # Title socket wins when the seller states it; otherwise derive it
            # from the model (family+generation) so every CPU carries a socket.
            socket = extract_socket(title) or queries.socket_for(model)
            cores  = extract_cores(title)
            # CPU + motherboard bundle: a chipset + an explicit pairing signal
            # (and no storage/GPU that would make it a whole PC). Marked for dual
            # CPU/MOBO membership — _upload also writes the MOBO side.
            _chip = extract_chipset(title)
            if (model and _chip and is_cpu_mobo_bundle(title)
                    and not _MOBO_SYSTEM_RE.search(title) and not _GPU_TELL_RE.search(title)):
                chipset     = _chip
                is_bundle   = True
                form_factor = extract_mobo_form_factor(title)

        elif productType == 'HDD':

            # Flash media isn't a hard drive — the job-lot search in particular
            # returns USB-stick lots that would otherwise pollute HDD groups.
            _tl = title.lower()
            if any(k in _tl for k in _FLASH_MEDIA_KEYWORDS):
                log.debug("[%s] Skipping flash media: %s", query, title[:60])
                continue
            # Neither is an SSD ("Solid State Hard Drive", NVMe "hard drives")
            # or an SSHD hybrid — those price like a different market entirely.
            if title_is_solid_state(title):
                log.debug("[%s] Skipping solid-state listing in HDD: %s", query, title[:60])
                continue
            if title_has_memory_token(title):
                log.debug("[%s] Skipping memory module in HDD: %s", query, title[:60])
                continue
            # Whole systems mention their drive too ("Gaming Laptop ... 1TB
            # HDD"). NB neither bare 'laptop' NOR the PC phrases are tells
            # here — "laptop hard drive" and "Desktop PC NAS Hard Drive" are
            # real drives describing what they fit. The reliable evidence is
            # a RAM spec or a CPU model: no bare-drive title ever states those.
            if title_is_system(title, 'hdd') or 'graphics card' in _tl or _GPU_TELL_RE.search(title):
                log.debug("[%s] Skipping system/GPU listing in HDD: %s", query, title[:60])
                continue
            # Mixed-capacity job lot ("12TB ... 2x 2TB, 2x 3TB, 2x 1TB") — no
            # single capacity fits and it can't be valued per unit.
            if is_mixed_capacity_lot(title):
                log.debug("[%s] Skipping mixed-capacity lot in HDD: %s", query, title[:60])
                continue

            HDD_BRANDS = ['SEAGATE','TOSHIBA','SAMSUNG','HITACHI','HGST','FUJITSU','MAXTOR']

            def extract_hdd_brand(title: str):
                t = title.upper()
                if 'WESTERN DIGITAL' in t or t.startswith('WD ') or ' WD ' in t:
                    return 'Western Digital'
                for b in HDD_BRANDS:
                    if b in t:
                        return b.title()
                return ''

            cap_pattern = re.compile(r'(\d+(?:\.\d+)?)\s*(TB|GB)', re.IGNORECASE)

            def extract_capacity_gb(title: str):
                m = cap_pattern.search(title)
                if m:
                    val, unit = float(m.group(1)), m.group(2).upper()
                    return int(val * 1000) if unit == 'TB' else int(val)
                return None

            def extract_interface(title: str):
                return 'SAS' if 'SAS' in title.upper() else 'SATA'

            ff_pattern = re.compile(r'(3\.5|2\.5)\s*["\']?')

            def extract_form_factor(title: str):
                m = ff_pattern.search(title)
                return f'{m.group(1)}"' if m else '3.5"'

            rpm_num_pattern = re.compile(r'(\d{4,5})\s*rpm', re.IGNORECASE)
            rpm_k_pattern   = re.compile(r'(\d+(?:\.\d+)?)\s*[Kk](?:\s*rpm|\b)', re.IGNORECASE)

            def extract_rpm(title: str):
                m = rpm_num_pattern.search(title)
                if m:
                    return int(m.group(1))
                m = rpm_k_pattern.search(title)
                if m:
                    return int(float(m.group(1)) * 1000)
                return None

            # A leading "(9.6TB)" lot total would otherwise become the capacity
            # and hide the "8x" count — strip it so the per-drive size + count win.
            _tcap = strip_leading_total(title)
            brand       = extract_hdd_brand(title)
            model       = None
            vram        = None
            socket      = None
            cores       = None
            capacity_gb = extract_capacity_gb(_tcap) or extract_capacity_gb(title)
            # No real drive is under ~40GB — a small figure means the title's
            # first capacity belongs to something else (an eGPU's "16GB
            # Laptop Graphics Card" once landed here as a 16GB drive).
            if capacity_gb is not None and capacity_gb < 40:
                log.debug("[%s] Skipping implausible drive capacity %sGB: %s",
                          query, capacity_gb, title[:60])
                continue
            interface   = extract_interface(title)
            form_factor = extract_form_factor(title)
            rpm         = extract_rpm(title)
            drive_type  = classify_drive_type(title)
            quantity    = extract_lot_quantity(_tcap)

        elif productType == 'SSD':

            _tl = title.lower()
            # Flash media isn't an SSD; SSHDs are hybrids priced like neither;
            # whole machines ("laptop, 1TB SSD") mention SSDs constantly.
            if any(k in _tl for k in _FLASH_MEDIA_KEYWORDS) or 'sshd' in _tl:
                log.debug("[%s] Skipping non-SSD storage: %s", query, title[:60])
                continue
            # Spinning drives leak in via the fuzzy "SSD job lot" search
            # ("20x Assorted 2TB SAS HDD JOB LOT"). Solid-state markers win
            # when both appear — "Solid State Hard Drive" IS an SSD.
            if title_is_spinning_disk(title) and not title_is_solid_state(title):
                log.debug("[%s] Skipping spinning drive in SSD: %s", query, title[:60])
                continue
            if title_has_memory_token(title):
                log.debug("[%s] Skipping memory module in SSD: %s", query, title[:60])
                continue
            # No bare drive names a CPU, RAM or a graphics card — those tell a
            # whole system (an "HP Pavilion i5 3330 128GB SSD DDR3" tower sat
            # in SSD). Storage size is the product here, so it isn't a tell.
            if title_is_system(title, 'ssd') or 'graphics card' in _tl or _GPU_TELL_RE.search(title):
                log.debug("[%s] Skipping system/GPU listing in SSD: %s", query, title[:60])
                continue
            if is_mixed_capacity_lot(title):                # mixed-capacity lot
                log.debug("[%s] Skipping mixed-capacity lot in SSD: %s", query, title[:60])
                continue

            ssd_cap_pattern = re.compile(r'(\d+(?:\.\d+)?)\s*(TB|GB)', re.IGNORECASE)

            def extract_ssd_capacity_gb(title: str):
                m = ssd_cap_pattern.search(title)
                if m:
                    val, unit = float(m.group(1)), m.group(2).upper()
                    return int(val * 1000) if unit == 'TB' else int(val)
                return None

            # Strip a leading "(N TB)" lot total (same drive-lot convention as
            # HDD) so per-drive capacity + the "Nx" count parse correctly.
            _tcap = strip_leading_total(title)
            capacity_gb = extract_ssd_capacity_gb(_tcap) or extract_ssd_capacity_gb(title)
            if capacity_gb is None or capacity_gb < 60 or capacity_gb > 8000:
                continue

            # Interface: NVMe unless it's explicitly SATA. Bare "M.2" without
            # "SATA" is treated as NVMe — M.2 SATA drives always say so.
            _has_m2 = bool(re.search(r'\bm[.\s]?2\b', _tl))
            if 'nvme' in _tl or (_has_m2 and 'sata' not in _tl):
                interface = 'NVMe'
            else:
                interface = 'SATA'
            form_factor = 'M.2' if (_has_m2 or 'nvme' in _tl) else '2.5"'

            # PCIe generation — display-only, not a market-group dimension
            # (most titles omit it; grouping on it would fragment forever).
            gen_m = re.search(r'(?:pcie|pci-e)?\s*gen\s*([345])\b|pcie\s*([345])\.0', _tl)
            pcie_gen = int(gen_m.group(1) or gen_m.group(2)) if gen_m else None

            SSD_BRAND_MAP = {
                'SAMSUNG': 'Samsung', 'CRUCIAL': 'Crucial', 'WESTERN DIGITAL': 'Western Digital',
                'WD ': 'Western Digital', 'SANDISK': 'SanDisk', 'KINGSTON': 'Kingston',
                'SEAGATE': 'Seagate', 'CORSAIR': 'Corsair', 'ADATA': 'ADATA', 'PNY': 'PNY',
                'LEXAR': 'Lexar', 'INTEL': 'Intel', 'SK HYNIX': 'Hynix', 'HYNIX': 'Hynix',
                'NETAC': 'Netac', 'FANXIANG': 'Fanxiang', 'INTEGRAL': 'Integral',
                'TEAMGROUP': 'TeamGroup', 'TEAM GROUP': 'TeamGroup', 'PATRIOT': 'Patriot',
                'SABRENT': 'Sabrent', 'GIGABYTE': 'Gigabyte', 'KIOXIA': 'Kioxia',
            }
            title_up = title.upper()
            brand = next((v for k, v in SSD_BRAND_MAP.items() if k in title_up), '')
            model = None
            vram = None
            drive_type = classify_drive_type(title)
            if drive_type == 'External':
                # Portable SSDs (T7, Extreme, enclosures) present over USB —
                # their internal protocol is invisible and irrelevant to price.
                interface = 'USB'
                form_factor = 'Ext'
            quantity = extract_lot_quantity(_tcap)

        elif productType == 'RAM':

            _tl = title.lower()
            # 'laptop'/'notebook' alone no longer disqualifies a listing — that
            # was dropping legitimate laptop (SODIMM) memory. Only treat those as
            # a whole-machine sale when paired with a storage device; the other
            # keywords are unambiguous complete systems.
            _is_system_ram = (
                any(k in _tl for k in [
                    'mini pc', 'mini-pc', ' nuc', 'barebones',
                    'desktop pc', 'all-in-one', 'gaming pc', 'gaming computer',
                    'custom pc', 'full pc', 'complete pc', 'pc bundle', 'pc build',
                    'compact pc', 'prebuilt', 'pre-built', 'desktop computer',
                    'gaming rig', 'gaming setup', 'vxrail', 'poweredge', 'proliant',
                ])
                or (('laptop' in _tl or 'notebook' in _tl)
                    and bool(re.search(r'\d+\s*(tb|gb)\s*(ssd|nvme|hdd|emmc)', _tl)))
                # a bare RAM kit never states drive storage — "16GB DDR4, 1TB
                # SSD" is a tower's spec sheet (they were parsing as the RAM)
                or bool(re.search(r'\d+\s*(?:tb|gb)\s*(?:ssd|nvme|hdd)\b', _tl))
                # server chassis with its RAM ("Dell R440 ... 8GB DDR4 ...")
                # — the audit found these polluting the RAM groups
                or bool(_SERVER_CHASSIS_RE.search(title))
                # a "+ CPU + motherboard" combo bundle isn't a RAM kit
                or ('combo' in _tl and bool(_CPU_MENTION_RE.search(title)))
            )
            if _is_system_ram:
                log.debug("[%s] Skipping system listing: %s", query, title[:60])
                continue

            # Type — DDR3 / DDR4 / DDR5 (mandatory; skip if absent)
            type_m = re.search(r'\b(DDR[345])\b', title, re.IGNORECASE)
            if not type_m:
                continue
            ram_type = type_m.group(1).upper()

            # Capacity — total kit GB; kit composition is its own market
            # dimension (2x8 vs 1x16 price differently).
            title_up = title.upper()
            kit_config, kit_total = extract_ram_kit(title)
            if kit_total:
                capacity_gb = kit_total
            else:
                all_gb = [int(m) for m in re.findall(r'(\d+)\s*GB', title_up)]
                capacity_gb = max(all_gb) if all_gb else None

            if capacity_gb is None or capacity_gb < 2 or capacity_gb > 256:
                continue

            # Speed — optional MHz
            spd_m = re.search(r'(\d{3,5})\s*[Mm][Hh][Zz]', title)
            speed = int(spd_m.group(1)) if spd_m else None

            # Brand
            RAM_BRAND_MAP = {
                'CORSAIR': 'Corsair', 'G.SKILL': 'G.Skill', 'GSKILL': 'G.Skill',
                'KINGSTON': 'Kingston', 'SAMSUNG': 'Samsung', 'CRUCIAL': 'Crucial',
                'HYPERX': 'HyperX', 'PATRIOT': 'Patriot', 'TEAMGROUP': 'TeamGroup',
                'TEAM GROUP': 'TeamGroup', 'ADATA': 'ADATA', 'PNY': 'PNY',
                'SK HYNIX': 'Hynix', 'HYNIX': 'Hynix', 'MICRON': 'Micron',
                'LEXAR': 'Lexar', 'BALLISTIX': 'Ballistix',
            }
            brand = next((v for k, v in RAM_BRAND_MAP.items() if k in title_up), None)
            model = None
            vram  = None
            ram_format = classify_ram_form_factor(title)

        elif productType == 'MOBO':
            # A bare board or a CPU+mobo bundle — never a whole prebuilt PC
            # (those add storage + a graphics card). No chipset → can't group it.
            if _MOBO_SYSTEM_RE.search(title) or _GPU_TELL_RE.search(title):
                log.debug("[%s] Skipping system listing in MOBO: %s", query, title[:60])
                continue
            chipset = extract_chipset(title)
            if not chipset:
                log.debug("[%s] No chipset in MOBO listing: %s", query, title[:60])
                continue
            brand       = extract_mobo_brand(title)
            model       = None
            vram        = None
            socket      = queries.chipset_socket(chipset)
            form_factor = extract_mobo_form_factor(title)

        else:
            brand = ''
            model = ''
            vram  = None

        log.debug("Parsed: brand=%s model=%s vram=%s", brand, model, vram)

        itemData = {
            'id': id,
            'title': title,
            'price': price,
            'price-range': price_is_range,
            'shipping': shipping,
            'time-left': timeLeft,
            'time-end': timeEnd,
            'sold-date': soldDate,
            'bid-count': bidCount,
            'reviews-count': reviewCount,
            'url': url,
            'brand': brand,
            'model': model,
            'vram': vram,
            'socket': socket,
            'cores': cores,
            'chipset': chipset,
            'is-bundle': is_bundle,
            'capacity-gb': capacity_gb,
            'interface': interface,
            'form-factor': form_factor,
            'rpm': rpm,
            'drive-type': drive_type,
            'ram-type': ram_type,
            'speed': speed,
            'ram-format': ram_format,
            'quantity': quantity,
            'pcie-gen': pcie_gen,
            'kit-config': kit_config,
            'feedback-pct': feedback_pct,
            'feedback-count': feedback_count,
        }
        
        data.append(itemData)
    
    # Remove item with prices too high or too low (also drop any items with unparsed prices)
    data = [item for item in data if item['price'] is not None]
    priceList = [item['price'] for item in data]
    parsedPriceList = __StDevParse(priceList)
    data = [item for item in data if item['price'] in parsedPriceList]
    
    return data

def __ParsePrices(soup):
    
    # Get item prices
    rawPriceList = [price.get_text(strip=True) for price in soup.find_all(class_="s-item__price")]
    priceList = [price for price in map(lambda rawPrice:__ParseRawPrice(rawPrice), rawPriceList) if price != None]
    
    # Get shipping prices
    rawShippingList = [item.get_text(strip=True) for item in soup.find_all(class_="su-styled-text secondary large")]
    shippingList = map(lambda rawPrice:__ParseRawPrice(rawPrice), rawShippingList)
    shippingList = [0 if price == None else price for price in shippingList]

    # Remove prices too high or too low
    priceList = __StDevParse(priceList)
    shippingList = __StDevParse(shippingList)

    data = {
        'price-list': priceList,
        'shipping-list': shippingList
    }
    return data

def __ParseRawPrice(string):
    parsedPrice = re.search(r'(\d+(\.\d+)?)', string.replace(',', ''))
    if (parsedPrice):
        return float(parsedPrice.group())
    else:
        return None

_FEEDBACK_RE = re.compile(r'(\d{1,3}(?:\.\d+)?)%\s*positive\s*\((\d[\d,]*)\)', re.IGNORECASE)

_SHIPPING_KEYWORD_RE = re.compile(r'delivery|postage', re.IGNORECASE)
_GBP_AMOUNT_RE = re.compile(r'£\s*([\d,]+(?:\.\d+)?)')

def __ExtractShipping(item) -> float:
    """Postage in pounds from a result card; 0.0 for free or absent.

    Handles both markups: a single span ("+£4.06 delivery" / "Free delivery")
    and the 2026-07 active-card split, where the amount ("+£36.95 ") and the
    wording ("delivery in 2-3 days") are sibling spans. The amount parse is
    £-anchored so "delivery in 2-3 days" can never read as £2; spans that
    mention delivery without a findable amount (e.g. a title) are skipped in
    favour of a later span that has one.
    """
    for el in item.find_all('span'):
        if not _SHIPPING_KEYWORD_RE.search(el.get_text(' ', strip=True)):
            continue
        m = _GBP_AMOUNT_RE.search(el.get_text(' ', strip=True))
        if m is None:
            prev = el.find_previous_sibling('span')
            if prev is not None:
                m = _GBP_AMOUNT_RE.search(prev.get_text(' ', strip=True))
        if m:
            return float(m.group(1).replace(',', ''))
    return 0.0

def __Average(numberList):

    if len(list(numberList)) == 0: return 0
    return sum(numberList) / len(list(numberList))

def __StDev(numberList):
    
    if len(list(numberList)) <= 1: return 0
    
    nominator = sum(map(lambda x: (x - sum(numberList) / len(numberList)) ** 2, numberList))
    stdev = (nominator / ( len(numberList) - 1)) ** 0.5

    return stdev

def __StDevParse(numberList):
    # Small samples don't have enough data to reliably identify outliers —
    # trimming would throw away legitimate spread. Return the list unchanged.
    if len(numberList) < 5:
        return numberList

    avg = __Average(numberList)
    stdev = __StDev(numberList)

    # Trim prices further than 2 SD from the mean. 1 SD was too aggressive
    # (dropped ~30% of samples even on clean data); 2 SD keeps ~95% of a
    # normal distribution and still catches wild eBay misformats.
    numberList = [nmbr for nmbr in numberList if (avg + 2 * stdev >= nmbr >= avg - 2 * stdev)]

    return numberList

def _display_wall_to_utc(wall: datetime, tz: ZoneInfo) -> datetime:
    """Attach the eBay display timezone to a wall-clock datetime and return
    the equivalent NAIVE UTC datetime (the frame everything is stored in)."""
    return wall.replace(tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)


def parse_ebay_endtime(endtime_str: str, reference_date: datetime = None):
    """Parse an eBay displayed end-time string into a naive UTC datetime.

    All comparisons happen in the display timezone's wall-clock frame, then
    the result is converted to UTC once at the end.

    reference_date: naive wall-clock "now" IN THE DISPLAY TIMEZONE — injected
    by tests for determinism; defaults to the current moment.
    """
    if not endtime_str:
        return None

    tz = ZoneInfo(EBAY_DISPLAY_TZ)
    if not reference_date:
        reference_date = datetime.now(tz).replace(tzinfo=None)

    # Clean input
    endtime_str = endtime_str.strip().strip("() ")

    weekdays = {"Mon":0, "Tue":1, "Wed":2, "Thu":3, "Fri":4, "Sat":5, "Sun":6}

    # Case 1: Today 21:44
    if endtime_str.lower().startswith("today"):
        time_part = endtime_str.split()[1]
        hour, minute = map(int, time_part.split(":"))
        wall = reference_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return _display_wall_to_utc(wall, tz)

    # Case 2: Sun, 14:28
    match = re.match(r"([A-Za-z]{3}),\s*(\d{1,2}):(\d{2})", endtime_str)
    if match:
        weekday_abbr, hour, minute = match.groups()
        hour, minute = int(hour), int(minute)

        target_weekday = weekdays[weekday_abbr]
        days_ahead = (target_weekday - reference_date.weekday() + 7) % 7

        if days_ahead == 0 and (
            hour < reference_date.hour or
            (hour == reference_date.hour and minute <= reference_date.minute)
        ):
            days_ahead = 7

        wall = (reference_date + timedelta(days=days_ahead)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        return _display_wall_to_utc(wall, tz)

    # Case 3: 05/03, 07:05
    match = re.match(r"(\d{2})/(\d{2}),\s*(\d{1,2}):(\d{2})", endtime_str)
    if match:
        day, month, hour, minute = map(int, match.groups())
        year = reference_date.year

        wall = datetime(year, month, day, hour, minute)
        if wall < reference_date:
            wall = wall.replace(year=year + 1)

        return _display_wall_to_utc(wall, tz)

    return None

_TIMELEFT_TOKEN_RE = re.compile(r'(\d+)\s*([dhms])', re.IGNORECASE)


def parse_ebay_timeleft(timeleft_str: str) -> timedelta | None:
    """Parse a relative eBay countdown ("2m", "1d 3h", "6d 23h left") into a
    timedelta. None when no time tokens are present. Used to derive an
    absolute EndTime on the 2026-07 markup, which no longer shows one."""
    if not timeleft_str:
        return None
    units = {'d': 'days', 'h': 'hours', 'm': 'minutes', 's': 'seconds'}
    kwargs = {}
    for num, unit in _TIMELEFT_TOKEN_RE.findall(timeleft_str):
        kwargs.setdefault(units[unit.lower()], int(num))
    return timedelta(**kwargs) if kwargs else None


def parse_soldDate(date_str: str):
    if not date_str:
        return None
    try:
        # convert e.g., "1 Dec 2025" to datetime object
        return datetime.strptime(date_str, "%d %b %Y")
    except ValueError:
        # fallback for weird formats
        return None

@dataclass
class Product:
    id: int
    title: str
    price: float
    time_left: Optional[str]
    time_end: Optional[datetime]
    sold_date: Optional[datetime]
    bid_count: int
    reviews_count: int
    url: str
    brand: Optional[str]
    model: Optional[str]
    vram: Optional[int]
    # Postage in pounds; folded into effective price by the deal queries.
    shipping: float = 0.0
    # CPU fields
    socket: Optional[str] = None
    cores: Optional[int] = None
    # Motherboard fields (Socket is shared with CPU; FormFactor with HDD/RAM)
    chipset: Optional[str] = None
    # CPU+motherboard bundle → dual CPU/MOBO membership, excluded from medians.
    is_bundle: bool = False
    # HDD fields
    capacity_gb: Optional[int] = None
    interface: Optional[str] = None
    form_factor: Optional[str] = None
    rpm: Optional[int] = None
    drive_type: Optional[str] = None   # Internal / External
    # RAM fields
    ram_type: Optional[str] = None
    speed: Optional[int] = None
    ram_format: Optional[str] = None   # DIMM / SODIMM
    # Units in the listing (job lots); deal queries price per unit.
    quantity: int = 1
    # Seller feedback from the result card; count 0 = no-history seller.
    feedback_pct: Optional[float] = None
    feedback_count: Optional[int] = None
    # SSD field — PCIe generation when the title states it (display only).
    pcie_gen: Optional[int] = None
    # RAM kit composition ('2x8'); None when the title doesn't state it.
    kit_config: Optional[str] = None

def _get_connection():
    conn = mariadb.connect(
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", 3305)),
        database=os.environ["DB_NAME"]
    )
    # Pin the session to UTC so NOW()/CURRENT_TIMESTAMP match the UTC-naive
    # datetimes we store (the server otherwise runs in host-local time, which
    # skewed every time comparison by an hour during BST).
    cur = conn.cursor()
    cur.execute("SET time_zone = '+00:00'")
    cur.close()
    return conn

def _upload(cur, p: Product, product_type: str, listing_kind: str = 'auction',
            kind_authoritative: bool = False) -> int:
    """Returns the EBAY rowcount: 1 = inserted, 2 = updated, 0 = no change."""
    # LastSeenAt = the last time a scrape actually saw this listing on eBay.
    # Seller-cancelled listings vanish from search but keep a future EndTime —
    # the deal queries use this stamp to drop them instead of showing phantom
    # deals until the original end time.
    # ListingType only changes when the caller actually KNOWS the type
    # (kind_authoritative): the auction-only search and the BIN sweep know;
    # targeted/sold re-scrapes (listing_type='all') must preserve whatever is
    # stored. Authoritative in BOTH directions — auctions that offer a BIN
    # option appear in LH_BIN results and were getting stuck as 'bin',
    # invisible to the auction pipeline.
    # FirstSeenAt = when we first inserted this listing; set once on INSERT and
    # never touched on UPDATE, so the BIN feed can offer an "added within" window
    # (LastSeenAt keeps moving as the 30-min sweep re-sees the same listing).
    cur.execute("""
        INSERT INTO EBAY (ID, Title, Price, Shipping, Quantity, Bids, EndTime, SoldDate, URL,
                          SellerFeedbackPct, SellerFeedbackCount, ListingType, FirstSeenAt, LastSeenAt)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
        ON DUPLICATE KEY UPDATE
            Title = VALUES(Title),
            Price = VALUES(Price),
            Shipping = VALUES(Shipping),
            Quantity = VALUES(Quantity),
            Bids = VALUES(Bids),
            EndTime = IF(EndTimeExact = 1, EndTime, VALUES(EndTime)),
            SoldDate = VALUES(SoldDate),
            URL = VALUES(URL),
            SellerFeedbackPct = VALUES(SellerFeedbackPct),
            SellerFeedbackCount = VALUES(SellerFeedbackCount),
            ListingType = IF(%s = 1, VALUES(ListingType), ListingType),
            LastSeenAt = NOW();
        """, (p.id, p.title, p.price * 100, int(round((p.shipping or 0) * 100)),
              p.quantity or 1, p.bid_count, p.time_end, p.sold_date, p.url,
              p.feedback_pct, p.feedback_count, listing_kind,
              1 if kind_authoritative else 0)
    )
    ebay_rc = cur.rowcount
    if product_type == 'GPU':
        cur.execute("""
            INSERT INTO GPU (ID, Brand, Model, VRAM)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                Brand = VALUES(Brand),
                Model = VALUES(Model),
                VRAM = VALUES(VRAM);
            """, (p.id, p.brand, p.model, p.vram)
        )
    elif product_type == 'CPU':
        cur.execute("""
            INSERT INTO CPU (ID, Brand, Model, Socket, Cores, IsBundle)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                Brand = VALUES(Brand),
                Model = VALUES(Model),
                Socket = VALUES(Socket),
                Cores = VALUES(Cores),
                IsBundle = GREATEST(IsBundle, VALUES(IsBundle));
            """, (p.id, p.brand, p.model, p.socket, p.cores, 1 if p.is_bundle else 0)
        )
        # Bundle: also write the motherboard side so it joins the MOBO category.
        if p.is_bundle and p.chipset:
            _upsert_mobo(cur, p.id, p.brand, p.chipset,
                         queries.chipset_socket(p.chipset), p.form_factor, is_bundle=True)
    elif product_type == 'HDD':
        cur.execute("""
            INSERT INTO HDD (ID, Brand, CapacityGB, Interface, FormFactor, RPM, DriveType)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                Brand = VALUES(Brand),
                CapacityGB = VALUES(CapacityGB),
                Interface = VALUES(Interface),
                FormFactor = VALUES(FormFactor),
                RPM = VALUES(RPM),
                DriveType = VALUES(DriveType);
            """, (p.id, p.brand, p.capacity_gb, p.interface, p.form_factor, p.rpm, p.drive_type)
        )
    elif product_type == 'SSD':
        cur.execute("""
            INSERT INTO SSD (ID, Brand, CapacityGB, Interface, FormFactor, DriveType, Gen)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                Brand      = VALUES(Brand),
                CapacityGB = VALUES(CapacityGB),
                Interface  = VALUES(Interface),
                FormFactor = VALUES(FormFactor),
                DriveType  = VALUES(DriveType),
                Gen        = VALUES(Gen);
            """, (p.id, p.brand, p.capacity_gb, p.interface, p.form_factor, p.drive_type, p.pcie_gen)
        )
    elif product_type == 'RAM':
        cur.execute("""
            INSERT INTO RAM (ID, Brand, CapacityGB, Type, Speed, FormFactor, KitConfig)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                Brand      = VALUES(Brand),
                CapacityGB = VALUES(CapacityGB),
                Type       = VALUES(Type),
                Speed      = VALUES(Speed),
                FormFactor = VALUES(FormFactor),
                KitConfig  = VALUES(KitConfig);
            """, (p.id, p.brand, p.capacity_gb, p.ram_type, p.speed, p.ram_format, p.kit_config)
        )
    elif product_type == 'MOBO':
        _upsert_mobo(cur, p.id, p.brand, p.chipset, p.socket, p.form_factor,
                     is_bundle=p.is_bundle)
    return ebay_rc


def _upsert_mobo(cur, ebay_id, brand, chipset, socket, form_factor, is_bundle=False):
    """MOBO upsert used by both the MOBO branch and the CPU-bundle dual write.
    IsBundle uses GREATEST so a plain-board re-scrape of a bundle listing can't
    downgrade the flag the CPU branch set."""
    cur.execute("""
        INSERT INTO MOBO (ID, Brand, Chipset, Socket, FormFactor, IsBundle)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            Brand      = VALUES(Brand),
            Chipset    = VALUES(Chipset),
            Socket     = VALUES(Socket),
            FormFactor = VALUES(FormFactor),
            IsBundle   = GREATEST(IsBundle, VALUES(IsBundle));
        """, (ebay_id, brand, chipset, socket, form_factor, 1 if is_bundle else 0)
    )

def _scrape_item_by_id(ebay_id: int, category: str, *, sold: bool) -> dict | None:
    """Fetch a single eBay listing by its item ID.

    sold=True  → searches completed/sold results  (for outcome verification)
    sold=False → searches active listing results  (for targeted price refresh)

    Uses the numeric item ID as the search term so eBay returns only that
    specific item.  Returns the parsed item dict on a match, None if not found.
    """
    soup = __GetHTML(str(ebay_id), 'uk', 'all', 'all', alreadySold=sold)
    items = __ParseItems(soup, str(ebay_id), category)
    for item in items:
        if str(item['id']) == str(ebay_id):
            return item
    return None


def _scrape_item_completed(ebay_id: int, category: str) -> dict | None:
    """Fetch a single eBay listing from all-completed results (sold + ended-unsold).

    Used as a fallback in VerifyPendingOutcomes when _scrape_item_by_id(sold=True)
    finds nothing.  Items that ended without selling appear in LH_Complete=1 results
    but NOT in LH_Complete=1&LH_Sold=1 results — this function catches those.

    Returns the parsed item dict on a match, None if not found.
    """
    soup = __GetHTML(str(ebay_id), 'uk', 'all', 'all', alreadySold='completed')
    items = __ParseItems(soup, str(ebay_id), category)
    for item in items:
        if str(item['id']) == str(ebay_id):
            return item
    return None


# ── field-coverage canary ──────────────────────────────────────────────────────
# Zero-row runs are caught by the scheduler's Kuma guard, but PARTIAL markup
# drift is silent: the pre-audit shipping parser returned £0 for everything
# for weeks without a single error. Per-field coverage over a full scrape run
# turns that rot into a same-day alert (heartbeat withheld → Kuma flags down).

_coverage: dict | None = None


def reset_field_coverage() -> None:
    """Start a fresh coverage window (call at the top of each full run)."""
    global _coverage
    _coverage = {'items': 0, 'feedback': 0, 'shipping': 0,
                 'sold_items': 0, 'sold_date': 0,
                 'active_items': 0, 'end_time': 0, 'bids': 0}


def _record_coverage(items: list, sold: bool) -> None:
    if _coverage is None:
        return
    c = _coverage
    c['items'] += len(items)
    c['feedback'] += sum(1 for i in items if i.get('feedback-pct') is not None)
    c['shipping'] += sum(1 for i in items if (i.get('shipping') or 0) > 0)
    if sold:
        c['sold_items'] += len(items)
        c['sold_date'] += sum(1 for i in items if i.get('sold-date'))
    else:
        c['active_items'] += len(items)
        c['end_time'] += sum(1 for i in items if i.get('time-end'))
        c['bids'] += sum(1 for i in items if (i.get('bid-count') or 0) > 0)


def get_field_coverage() -> dict | None:
    return dict(_coverage) if _coverage is not None else None


def coverage_alerts(cov: dict | None, min_items: int = 100) -> list[str]:
    """Fields whose parse coverage collapsed this run, as alert strings.

    Floors are deliberately loose — they alert on collapse (a parser going
    blind), not on natural variation. Free postage is common, so the shipping
    floor is low; plenty of auctions legitimately sit at 0 bids. Each check
    also needs a meaningful denominator so one bad page can't flap the alarm.
    """
    if not cov or cov['items'] < min_items:
        return []
    checks = [
        ('sold-date',       cov['sold_date'], cov['sold_items'],   0.80),
        ('end-time',        cov['end_time'],  cov['active_items'], 0.70),
        ('seller-feedback', cov['feedback'],  cov['items'],        0.50),
        ('shipping',        cov['shipping'],  cov['items'],        0.10),
        ('bid-count',       cov['bids'],      cov['active_items'], 0.05),
    ]
    alerts = []
    for name, got, of, floor in checks:
        if of >= 50 and got / of < floor:
            alerts.append(f"{name} coverage {got}/{of} ({got/of:.0%}) below {floor:.0%} floor")
    return alerts


def Scrape(query, product_type, country='us', condition='all', listing_type='all', cache=False):
    if country not in countryDict:
        raise Exception('Country not supported, please use one of the following: ' + ', '.join(countryDict.keys()))
    if condition not in conditionDict:
        raise Exception('Condition not supported, please use one of the following: ' + ', '.join(conditionDict.keys()))
    if listing_type not in typeDict:
        raise Exception('Type not supported, please use one of the following: ' + ', '.join(typeDict.keys()))

    sold_soup = __GetHTML(query, country, condition, listing_type, alreadySold=True, cache=cache)
    active_soup = __GetHTML(query, country, condition, listing_type, alreadySold=False, cache=cache)

    sold_items = __ParseItems(sold_soup, query, product_type)
    active_items = __ParseItems(active_soup, query, product_type)

    _record_coverage(sold_items, sold=True)
    _record_coverage(active_items, sold=False)

    return sold_items + active_items


def VerifyPendingOutcomes(hours_after: int = 6, give_up_days: int = 7) -> int:
    """Search eBay sold listings for DealOutcomes past their end time that
    still have SoldDate IS NULL in the EBAY table.

    Two-phase logic:
      Phase 1 — mark give-up: any item past `give_up_days` that is still
                unresolved is flagged GaveUp=1 and will never be retried.
      Phase 2 — verify in-window items: items between `hours_after` hours
                and `give_up_days` days old are looked up by ID in eBay
                sold results, then all-completed results.

    Cancelled listings appear in NEITHER search (sellers pull auctions that
    are going too cheap — the best deals attract cancellations). Two
    consecutive not-found passes with successful fetches ⇒ the listing was
    removed: close it as EndedUnsold instead of retrying for a week.

    Returns the number of outcomes successfully resolved this run.
    """
    conn = _get_connection()
    cur = conn.cursor()
    try:
        # ── Phase 1: mark items past the give-up threshold ───────────────────
        cur.execute("""
            UPDATE Scraper.DealOutcomes o
            JOIN   Scraper.EBAY e ON e.ID = o.EbayID
            SET    o.GaveUp = 1
            WHERE  o.GaveUp = 0
              AND  e.SoldDate IS NULL
              AND  o.EndTime < NOW() - INTERVAL %s DAY
        """, (give_up_days,))
        gave_up = cur.rowcount
        if gave_up:
            log.warning(
                "Outcome verification: gave up on %d item(s) past %dd threshold",
                gave_up, give_up_days,
            )

        # ── Phase 2: verify in-window items ──────────────────────────────────
        cur.execute("""
            SELECT o.EbayID, o.Category, e.Title, o.EndTime, o.VerifyMisses
            FROM   Scraper.DealOutcomes o
            JOIN   Scraper.EBAY e ON e.ID = o.EbayID
            WHERE  o.EndTime < NOW() - INTERVAL %s HOUR
              AND  o.EndTime > NOW() - INTERVAL %s DAY
              AND  e.SoldDate IS NULL
              AND  o.GaveUp = 0
        """, (hours_after, give_up_days))
        pending = cur.fetchall()

        if not pending:
            log.info("Outcome verification: no unresolved outcomes in window (%dh–%dd)", hours_after, give_up_days)
            conn.commit()
            return 0

        log.info("Outcome verification: checking %d item(s) in window (%dh–%dd)", len(pending), hours_after, give_up_days)
        resolved = 0

        for ebay_id, category, title, end_time, verify_misses in pending:
            try:
                # ── Pass 1: sold-only search ──────────────────────────────────
                item = _scrape_item_by_id(ebay_id, category, sold=True)
                if item and item.get('sold-date'):
                    cur.execute("""
                        UPDATE Scraper.EBAY
                        SET    SoldDate = %s,
                               Price    = %s,
                               Bids     = %s
                        WHERE  ID       = %s
                          AND  SoldDate IS NULL
                    """, (item['sold-date'], int(round(item['price'] * 100)), item['bid-count'], ebay_id))
                    log.info(
                        "Outcome verified: ID=%s sold for £%.2f on %s",
                        ebay_id, item['price'], item['sold-date'],
                    )
                    resolved += 1
                    continue

                # ── Pass 2: all-completed search (sold + ended-unsold) ────────
                completed_item = _scrape_item_completed(ebay_id, category)
                if completed_item:
                    # Found in completed results but NOT in sold results →
                    # the auction ended without a buyer.  Record the end time
                    # and NULL the price so it is excluded from market-price
                    # averages.
                    cur.execute("""
                        UPDATE Scraper.EBAY
                        SET    SoldDate = %s,
                               Price    = NULL
                        WHERE  ID       = %s
                          AND  SoldDate IS NULL
                    """, (end_time, ebay_id))
                    cur.execute("""
                        UPDATE Scraper.DealOutcomes
                        SET    EndedUnsold = 1
                        WHERE  EbayID = %s
                    """, (ebay_id,))
                    log.info(
                        "Outcome ended unsold: ID=%s category=%s end_time=%s title='%s'",
                        ebay_id, category, end_time, title[:80],
                    )
                    resolved += 1
                else:
                    # Both fetches succeeded and the listing is in neither
                    # sold nor completed results — removed/cancelled by the
                    # seller. One miss could be indexing lag; two consecutive
                    # passes (~an hour apart, 6h+ after end) is conclusive.
                    misses = (verify_misses or 0) + 1
                    if misses >= 2:
                        cur.execute("""
                            UPDATE Scraper.EBAY
                            SET    SoldDate = %s,
                                   Price    = NULL
                            WHERE  ID       = %s
                              AND  SoldDate IS NULL
                        """, (end_time, ebay_id))
                        cur.execute("""
                            UPDATE Scraper.DealOutcomes
                            SET    EndedUnsold = 1, VerifyMisses = %s
                            WHERE  EbayID = %s
                        """, (misses, ebay_id))
                        log.info(
                            "Outcome closed as removed/cancelled after %d not-found passes: "
                            "ID=%s category=%s end_time=%s title='%s'",
                            misses, ebay_id, category, end_time, title[:80],
                        )
                        resolved += 1
                    else:
                        cur.execute("""
                            UPDATE Scraper.DealOutcomes
                            SET    VerifyMisses = %s
                            WHERE  EbayID = %s
                        """, (misses, ebay_id))
                        log.warning(
                            "Outcome not found in sold or completed results (pass %d/2) — "
                            "ID=%s category=%s title='%s'; will close as removed on next miss",
                            misses, ebay_id, category, title[:80],
                        )
            except Exception as e:
                log.warning("Outcome verification skipped for item %s: %s", ebay_id, e)

        conn.commit()
        log.info("Outcome verification complete: %d/%d resolved", resolved, len(pending))
        return resolved

    except Exception as e:
        log.error("Outcome verification error: %s", e)
        conn.rollback()
        return 0
    finally:
        conn.close()


def _product_from_dict(d: dict) -> Product:
    """Parsed item dict → Product (the single dict/dataclass field mapping)."""
    return Product(
        id=d["id"], title=d["title"], price=d["price"],
        shipping=d.get("shipping") or 0,
        time_left=d["time-left"], time_end=d["time-end"],
        sold_date=d["sold-date"], bid_count=d["bid-count"],
        reviews_count=d["reviews-count"], url=d["url"],
        brand=d["brand"], model=d["model"], vram=d["vram"],
        socket=d["socket"], cores=d["cores"], chipset=d.get("chipset"),
        is_bundle=bool(d.get("is-bundle")),
        capacity_gb=d["capacity-gb"], interface=d["interface"],
        form_factor=d["form-factor"], rpm=d["rpm"],
        drive_type=d.get("drive-type"),
        ram_type=d["ram-type"], speed=d["speed"],
        ram_format=d.get("ram-format"),
        quantity=d.get("quantity") or 1,
        pcie_gen=d.get("pcie-gen"),
        kit_config=d.get("kit-config"),
        feedback_pct=d.get("feedback-pct"),
        feedback_count=d.get("feedback-count"),
    )


def ScrapeAndUpload(query_list: list[str], product_type: str, country='us', condition='all', listing_type='all', cache=False):
    conn = _get_connection()
    cur = conn.cursor()

    try:
        inserted = updated = 0
        for query in query_list:
            items = Scrape(query, product_type, country, condition, listing_type, cache=cache)

            # An auction-only search KNOWS every result is an auction (and can
            # heal rows the BIN sweep once mis-tagged); an 'all' search knows
            # nothing about type and must preserve what's stored.
            authoritative = (listing_type == 'auction')
            for p in map(_product_from_dict, items):
                try:
                    rc = _upload(cur, p, product_type,
                                 kind_authoritative=authoritative)
                    if rc == 1:
                        inserted += 1
                    elif rc >= 2:
                        updated += 1
                except mariadb.Error as e:
                    log.error("DB error uploading item %s: %s", p.id, e)

        conn.commit()
        log.info("Scrape complete [%s]: %d new, %d updated", product_type, inserted, updated)
        return inserted, updated

    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def ScrapeBinAndUpload(query_list: list[str], product_type: str, country='us', condition='all'):
    """Newly-listed Buy-It-Now sweep for one category.

    One fetch per query: active fixed-price listings, newest first (_sop=10)
    — a good BIN bargain is gone in minutes, so only the fresh end of the
    results matters. Rows are upserted with ListingType='bin' so the deal
    queries can treat them separately from auctions (no bids, no end time,
    listed price IS the final price).

    Deliberately does NOT record field coverage: the canary window belongs to
    the hourly full scrape, and BIN cards legitimately lack end times/bids —
    counting them would fake a parser collapse.
    """
    conn = _get_connection()
    cur = conn.cursor()
    try:
        inserted = updated = 0
        for query in query_list:
            # One blocked fetch must not kill the category's remaining
            # queries — the lane reruns in minutes anyway.
            try:
                soup = __GetHTML(query, country, condition, 'bin', alreadySold='new_first')
            except Exception as e:
                log.warning("BIN scan: fetch failed for %r (%s) — skipping query", query, e)
                continue
            items = __ParseItems(soup, query, product_type)
            for d in items:
                # "Choose a capacity" variation listings: the card's price is
                # the CHEAPEST variant's — pair it with the title's spec and
                # you invent a 90%-off phantom. Skip on the price-range tell,
                # plus a multi-capacity title backstop for storage.
                if d.get('price-range'):
                    log.debug("BIN scan: skipping variation listing (price range): %s", d['title'][:60])
                    continue
                if product_type in ('HDD', 'SSD') and len(title_capacity_values(d['title'])) > 1:
                    log.debug("BIN scan: skipping multi-capacity title: %s", d['title'][:60])
                    continue
                # LH_BIN also returns AUCTIONS that offer a BIN option — their
                # card shows the current BID as the price (a £4.21 "RX 7900
                # XTX BIN" was a reserve auction). A countdown or a bid count
                # is the tell; upsert those as the auctions they are.
                is_auction = bool(d.get('time-end')) or (d.get('bid-count') or 0) > 0
                p = _product_from_dict(d)
                try:
                    rc = _upload(cur, p, product_type,
                                 listing_kind='auction' if is_auction else 'bin',
                                 kind_authoritative=True)
                    if rc == 1:
                        inserted += 1
                    elif rc >= 2:
                        updated += 1
                except mariadb.Error as e:
                    log.error("DB error uploading BIN item %s: %s", p.id, e)
        conn.commit()
        log.info("BIN scrape complete [%s]: %d new, %d updated", product_type, inserted, updated)
        return inserted, updated
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


_scrape_meta_ensured = False


def _ensure_scrape_meta_table(cur) -> None:
    """Create ScrapeMeta if missing. Runs once per process."""
    global _scrape_meta_ensured
    if _scrape_meta_ensured:
        return
    cur.execute("""
        CREATE TABLE IF NOT EXISTS Scraper.ScrapeMeta (
            id          TINYINT  NOT NULL DEFAULT 1 PRIMARY KEY,
            LastScrapeAt DATETIME NULL
        )
    """)
    _scrape_meta_ensured = True


def RecordScrapeCompleted(stats: dict | None = None):
    """Persist the completion time and (optionally) a JSON summary of the run
    — rows touched, category successes, field coverage, alerts. The health
    page reads this so scrape observability doesn't require docker logs."""
    import json
    DUP_COLUMN_ERRNO = 1060
    conn = _get_connection()
    try:
        cur = conn.cursor()
        _ensure_scrape_meta_table(cur)
        try:
            cur.execute("ALTER TABLE Scraper.ScrapeMeta ADD COLUMN LastRunStats TEXT NULL")
            conn.commit()
        except mariadb.Error as e:
            if getattr(e, "errno", None) != DUP_COLUMN_ERRNO:
                raise
        cur.execute("""
            INSERT INTO Scraper.ScrapeMeta (id, LastScrapeAt, LastRunStats) VALUES (1, NOW(), %s)
            ON DUPLICATE KEY UPDATE LastScrapeAt = NOW(), LastRunStats = VALUES(LastRunStats)
        """, (json.dumps(stats) if stats else None,))
        conn.commit()
    finally:
        conn.close()


def GetActiveDeals() -> list:
    """Return active tracked deals that haven't sold and haven't ended yet.

    Returns a list of (ebay_id, category, title, end_time) tuples, one per
    row in DealOutcomes that is still live.  Returns [] on any error so a
    transient DB failure never breaks the scheduler loop.
    """
    try:
        conn = _get_connection()
        cur = conn.cursor()
        try:
            # e.EndTime is the live (and possibly exact-refined) end; the
            # DealOutcomes copy is the surfacing-time approximation.
            cur.execute("""
                SELECT o.EbayID, o.Category, e.Title, COALESCE(e.EndTime, o.EndTime)
                FROM   Scraper.DealOutcomes o
                JOIN   Scraper.EBAY e ON e.ID = o.EbayID
                WHERE  COALESCE(e.EndTime, o.EndTime) > NOW()
                  AND  e.SoldDate IS NULL
            """)
            rows = cur.fetchall()
            if rows:
                log.info("Active deals: %d item(s) currently tracked", len(rows))
            return list(rows)
        finally:
            conn.close()
    except Exception as e:
        log.error("GetActiveDeals failed: %s", e)
        return []


def ScrapeTargeted(items: list) -> int:
    """Scrape specific tracked items by title and upsert results to the DB.

    `items` is a list of (ebay_id, category, title) tuples — the same
    three-element prefix returned by GetActiveDeals (end_time is dropped
    by the caller before passing here).

    Returns the number of items successfully found and upserted.
    """
    if not items:
        return 0

    conn = _get_connection()
    cur = conn.cursor()
    updated = 0

    try:
        for ebay_id, category, title in items:
            try:
                item = _scrape_item_by_id(ebay_id, category, sold=False)
                if item:
                    product = Product(
                        id=item['id'],
                        title=item['title'],
                        price=item['price'],
                        shipping=item.get('shipping') or 0,
                        time_left=item['time-left'],
                        time_end=item['time-end'],
                        sold_date=item['sold-date'],
                        bid_count=item['bid-count'],
                        reviews_count=item['reviews-count'],
                        url=item['url'],
                        brand=item['brand'],
                        model=item['model'],
                        vram=item['vram'],
                        socket=item['socket'],
                        cores=item['cores'],
                        capacity_gb=item['capacity-gb'],
                        interface=item['interface'],
                        form_factor=item['form-factor'],
                        rpm=item['rpm'],
                        drive_type=item.get('drive-type'),
                        ram_type=item['ram-type'],
                        speed=item['speed'],
                        ram_format=item.get('ram-format'),
                        quantity=item.get('quantity') or 1,
                        pcie_gen=item.get('pcie-gen'),
                        kit_config=item.get('kit-config'),
                        feedback_pct=item.get('feedback-pct'),
                        feedback_count=item.get('feedback-count'),
                    )
                    _upload(cur, product, category)
                    _record_snapshot(
                        cur, ebay_id,
                        round((item['price'] + (item.get('shipping') or 0)) * 100),
                        item['bid-count'], item.get('time-end'),
                    )
                    log.info(
                        "Targeted scrape updated: ID=%s '%.50s' price=£%.2f bids=%d",
                        ebay_id, title, item['price'], item['bid-count'],
                    )
                    updated += 1
                else:
                    log.debug(
                        "Targeted scrape: ID=%s not found in active results (may have ended)",
                        ebay_id,
                    )
            except Exception as e:
                log.warning("Targeted scrape failed for item %s: %s", ebay_id, e)

        conn.commit()
        log.info("Targeted scrape complete: %d/%d item(s) updated", updated, len(items))
        return updated

    except Exception as e:
        log.error("ScrapeTargeted DB error: %s", e)
        conn.rollback()
        return 0
    finally:
        conn.close()


def EnsureShippingColumn() -> None:
    """Add EBAY.Shipping (pence) on installations that predate it.

    MariaDB errno 1060 (ER_DUP_FIELDNAME) means the column already exists and
    is the expected outcome on every run after the first.
    """
    DUP_COLUMN_ERRNO = 1060
    conn = _get_connection()
    try:
        cur = conn.cursor()
        try:
            cur.execute("ALTER TABLE Scraper.EBAY ADD COLUMN Shipping INT NULL")
            conn.commit()
            log.info("EBAY: added Shipping column")
        except mariadb.Error as e:
            if getattr(e, "errno", None) != DUP_COLUMN_ERRNO:
                log.error("EBAY: unexpected error adding Shipping column: %s", e)
    finally:
        conn.close()


def EnsureQuantityColumn() -> None:
    """Add EBAY.Quantity (units per listing, default 1) on installations that
    predate it, then backfill HDD rows by re-parsing titles so historical lot
    sales stop polluting the single-unit market medians. NULL elsewhere is
    treated as 1 by the queries.
    """
    DUP_COLUMN_ERRNO = 1060
    conn = _get_connection()
    try:
        cur = conn.cursor()
        added = False
        try:
            cur.execute("ALTER TABLE Scraper.EBAY ADD COLUMN Quantity INT NULL")
            conn.commit()
            added = True
            log.info("EBAY: added Quantity column")
        except mariadb.Error as e:
            if getattr(e, "errno", None) != DUP_COLUMN_ERRNO:
                log.error("EBAY: unexpected error adding Quantity column: %s", e)

        if added:
            cur.execute("""
                SELECT e.ID, e.Title FROM Scraper.EBAY e
                JOIN Scraper.HDD h ON h.ID = e.ID
                WHERE e.Quantity IS NULL
            """)
            rows = cur.fetchall()
            lots = 0
            for ebay_id, title in rows:
                qty = extract_lot_quantity(title or '')
                cur.execute("UPDATE Scraper.EBAY SET Quantity = %s WHERE ID = %s", (qty, ebay_id))
                if qty > 1:
                    lots += 1
            if rows:
                conn.commit()
                log.info("EBAY: backfilled Quantity for %d HDD row(s) (%d lot(s) found)", len(rows), lots)
    except mariadb.Error as e:
        log.error("EnsureQuantityColumn failed: %s", e)
        conn.rollback()
    finally:
        conn.close()


def EnsureRamKitConfig() -> None:
    """Add RAM.KitConfig and backfill from stored titles. NULL = unstated
    (a valid value), so the backfill only runs when the column is new."""
    DUP_COLUMN_ERRNO = 1060
    conn = _get_connection()
    try:
        cur = conn.cursor()
        added = False
        try:
            cur.execute("ALTER TABLE Scraper.RAM ADD COLUMN KitConfig VARCHAR(10) NULL")
            conn.commit()
            added = True
            log.info("RAM: added KitConfig column")
        except mariadb.Error as e:
            if getattr(e, "errno", None) != DUP_COLUMN_ERRNO:
                log.error("RAM: unexpected error adding KitConfig: %s", e)
        if added:
            cur.execute("""
                SELECT r.ID, e.Title FROM Scraper.RAM r
                JOIN Scraper.EBAY e ON e.ID = r.ID
            """)
            rows = cur.fetchall()
            stated = 0
            for ram_id, title in rows:
                cfg, _total = extract_ram_kit(title or '')
                if cfg:
                    cur.execute("UPDATE Scraper.RAM SET KitConfig = %s WHERE ID = %s", (cfg, ram_id))
                    stated += 1
            conn.commit()
            log.info("RAM: backfilled KitConfig for %d/%d row(s)", stated, len(rows))
    finally:
        conn.close()


def EnsureSsdTable() -> None:
    """Create the SSD satellite table (Gen = PCIe generation, display-only)."""
    conn = _get_connection()
    try:
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
    except mariadb.Error as e:
        log.error("EnsureSsdTable failed: %s", e)
    finally:
        conn.close()


def EnsureDealSnapshots() -> None:
    """Create the DealSnapshots price-trajectory table.

    One row per OBSERVATION of a live in-window deal: the hourly surfacing
    pass snapshots every deal it sees, and the targeted scraper snapshots
    every refresh (1–15 min cadence in the final hour). This is the dataset
    for time-to-end-aware snipe premiums — the current single ratio is
    trained at ≤2h-to-end and misjudges listings viewed further out.
    """
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS Scraper.DealSnapshots (
                ID          BIGINT   AUTO_INCREMENT PRIMARY KEY,
                EbayID      BIGINT   NOT NULL,
                SnapAt      DATETIME NOT NULL,
                EffPrice    INT      NOT NULL,
                Bids        INT      NOT NULL DEFAULT 0,
                MinutesLeft INT      NULL,
                KEY idx_item_time (EbayID, SnapAt)
            )
        """)
        conn.commit()
    except mariadb.Error as e:
        log.error("EnsureDealSnapshots failed: %s", e)
    finally:
        conn.close()


def _record_snapshot(cur, ebay_id, eff_price_pence, bids, end_time) -> None:
    """Append one trajectory observation. Best-effort: a snapshot failure
    must never break the scrape or surfacing pass that carries it."""
    try:
        minutes_left = None
        if end_time is not None:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            minutes_left = int((end_time - now).total_seconds() // 60)
        cur.execute("""
            INSERT INTO Scraper.DealSnapshots (EbayID, SnapAt, EffPrice, Bids, MinutesLeft)
            VALUES (%s, NOW(), %s, %s, %s)
        """, (ebay_id, int(eff_price_pence), int(bids or 0), minutes_left))
    except mariadb.Error as e:
        log.warning("Snapshot insert failed for %s: %s", ebay_id, e)


def EnsureOutcomeColumns() -> None:
    """DealOutcomes columns the scheduler writes (PredictedFinal, VerifyMisses)
    — App.py's ensure_outcomes_table also adds these, but the scraper must not
    depend on the web container having booted first."""
    DUP_COLUMN_ERRNO = 1060
    conn = _get_connection()
    try:
        cur = conn.cursor()
        for col_sql in ("PredictedFinal INT NULL",
                        "VerifyMisses INT NOT NULL DEFAULT 0",
                        "NearMiss TINYINT(1) NOT NULL DEFAULT 0",
                        "PredMargin TINYINT(1) NOT NULL DEFAULT 0",
                        "ItemLocation VARCHAR(80) NULL",
                        "ItemCondition VARCHAR(40) NULL",
                        "Epid VARCHAR(20) NULL",
                        "CategoryPath VARCHAR(200) NULL",
                        "EnrichNote VARCHAR(60) NULL",
                        "Watchers INT NULL"):
            try:
                cur.execute(f"ALTER TABLE Scraper.DealOutcomes ADD COLUMN {col_sql}")
                conn.commit()
                log.info("DealOutcomes: added %s column", col_sql.split()[0])
            except mariadb.Error as e:
                if getattr(e, "errno", None) != DUP_COLUMN_ERRNO:
                    log.error("DealOutcomes: unexpected error adding %s: %s", col_sql.split()[0], e)
    finally:
        conn.close()


def EnsureCanonicalUrls() -> None:
    """Strip tracking query strings from stored listing URLs.

    Search-page hrefs carried ~800 chars of tracking params that overflowed
    the VARCHAR(500) URL column (truncated, sometimes broken links). New rows
    are canonicalised at parse time; this cleans what's already stored.
    Idempotent — after the first pass no row contains a '?'.
    """
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE Scraper.EBAY
            SET URL = SUBSTRING_INDEX(URL, '?', 1)
            WHERE URL LIKE '%?%'
        """)
        conn.commit()
        if cur.rowcount:
            log.info("EBAY: canonicalised %d stored URL(s)", cur.rowcount)
    except mariadb.Error as e:
        log.error("EnsureCanonicalUrls failed: %s", e)
        conn.rollback()
    finally:
        conn.close()


# Item pages embed the auction's exact end ("endDate":"...Z") — search
# results only show a truncated countdown ("44m"), so every derived EndTime
# is up to 59s early. One item-page fetch per tracked deal fixes that.
_END_DATE_RE = re.compile(r'"endDate"\s*:\s*"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)"')


def _parse_end_date(html: str):
    """Exact naive-UTC end datetime from an item page, or None."""
    m = _END_DATE_RE.search(html or '')
    if not m:
        return None
    try:
        dt = datetime.fromisoformat(m.group(1).replace('Z', '+00:00'))
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None
    # Sanity: an auction end more than 14 days out (or long past) is a
    # parse of the wrong field.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if not (now - timedelta(days=1) < dt < now + timedelta(days=14)):
        return None
    return dt


def RefineEndTime(ebay_id: int):
    """Fetch the listing's item page and pin EndTime to the second.

    Returns the exact datetime (or None). EndTimeExact=1 stops later
    countdown-derived upserts from degrading it back to minute precision.
    """
    try:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT EndTimeExact FROM Scraper.EBAY WHERE ID = %s", (ebay_id,))
            row = cur.fetchone()
            if row and row[0]:
                return None  # already exact (surfacing enrichment did it)
        finally:
            conn.close()
        html = _fetch_direct(f'https://www.ebay.co.uk/itm/{ebay_id}')
        exact = _parse_end_date(html) if html else None
        if exact is None:
            log.warning("EndTime refinement: no endDate found for %s", ebay_id)
            return None
        conn = _get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                UPDATE Scraper.EBAY SET EndTime = %s, EndTimeExact = 1
                WHERE ID = %s
            """, (exact, ebay_id))
            conn.commit()
        finally:
            conn.close()
        log.info("EndTime refined to the second: ID=%s ends %s", ebay_id, exact)
        return exact
    except Exception as e:
        log.warning("EndTime refinement failed for %s: %s", ebay_id, e)
        return None


# ── item-page enrichment ──────────────────────────────────────────────────────
# One fetch per newly surfaced deal pulls eBay's own structured facts — the
# final validation gate before a notification: eBay's category (accessory
# detector), structured condition (unlabelled for-parts detector), reserve
# status (unmeetable-price detector), plus location/ePID for the record.

_RESERVE_NOT_MET_RE = re.compile(r'reserve\s+(?:price\s+)?not\s+met', re.IGNORECASE)
_LOCATED_IN_RE = re.compile(r'Located in:\s*([^<]{2,80})<')
_EPID_RE = re.compile(r'"epid"\s*:\s*"?(\d{6,15})')

# Watcher count = demand signal. The "Add to Watchlist - N watchers" aria-label
# on the heart button is the stable figure (an anonymous fetch always sees "Add
# to", never "Remove from"); fall back to any "N watchers" social-proof text.
_WATCHERS_ARIA_RE = re.compile(r'Watchlist\s*-\s*([\d,]+)\s+watchers', re.IGNORECASE)
_WATCHERS_TEXT_RE = re.compile(r'([\d,]+)\s+watchers', re.IGNORECASE)


def _extract_watchers(html: str):
    """How many people are watching the listing, or None if not shown."""
    m = _WATCHERS_ARIA_RE.search(html) or _WATCHERS_TEXT_RE.search(html)
    if not m:
        return None
    try:
        return int(m.group(1).replace(',', ''))
    except ValueError:
        return None

# Which purchase actions a listing OFFERS — from the item page's call-to-action
# panels, not eBay's label dictionary (which lists every CTA name regardless of
# which are shown: "auction","buyItNow","bestOffer" all appear as translation
# strings even on a bid-only listing). The *-action div / *Btn_btn id is only
# emitted for a button the listing actually presents. Best-effort like the
# reserve flag: if the markup drifts, the flag stays false and the deal-page
# advisor falls back to ListingType.
_HAS_BID_RE   = re.compile(r'x-bid-action|\bbidBtn_btn\b', re.IGNORECASE)
_HAS_BIN_RE   = re.compile(r'x-bin-action|\bbinBtn_btn\b', re.IGNORECASE)
_HAS_OFFER_RE = re.compile(r'x-offer-action|\b(?:oiBtn|boBtn|ofrBtn)_btn\b', re.IGNORECASE)

# eBay leaf-category tokens that legitimise a listing per our category.
_CAT_OK_TOKENS = {
    'GPU': ('graphics',),
    'CPU': ('processor', 'cpu'),
    'HDD': ('drive', 'storage'),
    'SSD': ('drive', 'storage'),
    'RAM': ('memory', 'ram'),
}


def category_matches(product_type: str, category_path: str) -> bool:
    """True when eBay's breadcrumb is consistent with our category (or when
    there's no breadcrumb to judge by — absence never suppresses)."""
    if not category_path:
        return True
    path = category_path.lower()
    return any(tok in path for tok in _CAT_OK_TOKENS.get(product_type.upper(), ()))


def _extract_condition(html: str):
    """The structured condition value ("Used", "For parts or not working")."""
    i = html.find('"condition":{')
    if i == -1:
        return None
    window = html[i:i + 2000]
    j = window.find('"values"')
    if j == -1:
        return None
    # Several text spans live here ("See all condition definitions" links,
    # the label itself); the condition value is the one shaped like
    # "Used: long explanation..." or a bare known label.
    for m in re.finditer(r'"text"\s*:\s*"([^"]{2,600})"', window[j:]):
        text = m.group(1).strip()
        if (text.lower().startswith(('see all', 'read more', 'read less'))
                or text == 'Condition'):
            continue
        return text.split(':')[0].strip()[:40] or None
    return None


def _extract_enrichment(html: str) -> dict:
    """Everything useful from one item page. All fields optional."""
    from bs4 import BeautifulSoup
    path = None
    try:
        soup = BeautifulSoup(html, 'html.parser')
        nav = soup.find(attrs={'data-testid': 'x-breadcrumb'})
        if nav:
            crumbs = [a.get_text(strip=True) for a in nav.find_all('a')]
            # eBay renders the crumb trail twice (mobile+desktop variants).
            crumbs = list(dict.fromkeys(c for c in crumbs if c))
            if crumbs:
                path = ' > '.join(crumbs)[:200]
    except Exception:
        pass
    loc = _LOCATED_IN_RE.search(html)
    epid = _EPID_RE.search(html)
    return {
        'end': _parse_end_date(html),
        'condition': _extract_condition(html),
        'reserve_not_met': bool(_RESERVE_NOT_MET_RE.search(html)),
        'category_path': path,
        'location': loc.group(1).strip() if loc else None,
        'epid': epid.group(1) if epid else None,
        # which purchase routes the listing offers — a listing can be several
        # at once (verified live: an auction with a Buy-It-Now shows both the
        # bid and BIN action panels).
        'has_bid': bool(_HAS_BID_RE.search(html)),
        'has_bin': bool(_HAS_BIN_RE.search(html)),
        'has_best_offer': bool(_HAS_OFFER_RE.search(html)),
        'watchers': _extract_watchers(html),
    }


def EnrichListing(ebay_id: int) -> dict | None:
    """Fetch the item page once and extract the enrichment dict. None on
    fetch failure — enrichment must never block surfacing."""
    try:
        html = _fetch_direct(f'https://www.ebay.co.uk/itm/{ebay_id}')
        return _extract_enrichment(html) if html else None
    except Exception as e:
        log.warning("Enrichment fetch failed for %s: %s", ebay_id, e)
        return None


def EnsureEnrichmentColumns() -> None:
    """EBAY.ReserveNotMet (deal queries gate on it) — reserve-not-met
    auctions show a price nobody can actually win at."""
    DUP_COLUMN_ERRNO = 1060
    conn = _get_connection()
    try:
        cur = conn.cursor()
        try:
            cur.execute("ALTER TABLE Scraper.EBAY ADD COLUMN ReserveNotMet TINYINT(1) NOT NULL DEFAULT 0")
            conn.commit()
            log.info("EBAY: added ReserveNotMet column")
        except mariadb.Error as e:
            if getattr(e, "errno", None) != DUP_COLUMN_ERRNO:
                log.error("EBAY: unexpected error adding ReserveNotMet: %s", e)
    finally:
        conn.close()


def EnsureOfferColumns() -> None:
    """EBAY.HasBid / HasBin / HasBestOffer — item-page purchase-route flags
    feeding the deal-page price advisor. A listing can offer several at once
    (an auction with a Buy-It-Now). NULL = not yet enriched (advisor hedges)."""
    DUP_COLUMN_ERRNO = 1060
    conn = _get_connection()
    try:
        cur = conn.cursor()
        for col in ('HasBid', 'HasBin', 'HasBestOffer'):
            try:
                cur.execute(f"ALTER TABLE Scraper.EBAY ADD COLUMN {col} TINYINT(1) NULL")
                conn.commit()
                log.info("EBAY: added %s column", col)
            except mariadb.Error as e:
                if getattr(e, "errno", None) != DUP_COLUMN_ERRNO:
                    log.error("EBAY: unexpected error adding %s: %s", col, e)
    finally:
        conn.close()


def EnsureEndTimeExact() -> None:
    """EBAY.EndTimeExact flag — set once an item-page fetch pinned the end."""
    DUP_COLUMN_ERRNO = 1060
    conn = _get_connection()
    try:
        cur = conn.cursor()
        try:
            cur.execute("ALTER TABLE Scraper.EBAY ADD COLUMN EndTimeExact TINYINT(1) NOT NULL DEFAULT 0")
            conn.commit()
            log.info("EBAY: added EndTimeExact column")
        except mariadb.Error as e:
            if getattr(e, "errno", None) != DUP_COLUMN_ERRNO:
                log.error("EBAY: unexpected error adding EndTimeExact: %s", e)
    finally:
        conn.close()


def EnsureLastSeenColumn() -> None:
    """Add EBAY.LastSeenAt and stamp existing rows with NOW() so everything
    starts fresh — live listings are re-stamped within one scrape cycle;
    already-cancelled phantoms age out of the deal feed 90 minutes later.
    """
    DUP_COLUMN_ERRNO = 1060
    conn = _get_connection()
    try:
        cur = conn.cursor()
        try:
            cur.execute("ALTER TABLE Scraper.EBAY ADD COLUMN LastSeenAt DATETIME NULL")
            conn.commit()
            log.info("EBAY: added LastSeenAt column")
            cur.execute("UPDATE Scraper.EBAY SET LastSeenAt = NOW() WHERE LastSeenAt IS NULL")
            conn.commit()
            log.info("EBAY: stamped LastSeenAt on %d existing row(s)", cur.rowcount)
        except mariadb.Error as e:
            if getattr(e, "errno", None) != DUP_COLUMN_ERRNO:
                log.error("EBAY: unexpected error adding LastSeenAt column: %s", e)
    finally:
        conn.close()


def EnsureFirstSeenColumn() -> None:
    """Add EBAY.FirstSeenAt (when a listing was first inserted). Existing rows
    are backfilled from LastSeenAt — the best estimate we have for pre-column
    rows — so the BIN feed's 'added within' window has a value to filter on."""
    DUP_COLUMN_ERRNO = 1060
    conn = _get_connection()
    try:
        cur = conn.cursor()
        try:
            cur.execute("ALTER TABLE Scraper.EBAY ADD COLUMN FirstSeenAt DATETIME NULL")
            conn.commit()
            log.info("EBAY: added FirstSeenAt column")
            cur.execute("UPDATE Scraper.EBAY SET FirstSeenAt = LastSeenAt WHERE FirstSeenAt IS NULL")
            conn.commit()
            log.info("EBAY: backfilled FirstSeenAt on %d existing row(s)", cur.rowcount)
        except mariadb.Error as e:
            if getattr(e, "errno", None) != DUP_COLUMN_ERRNO:
                log.error("EBAY: unexpected error adding FirstSeenAt column: %s", e)
    finally:
        conn.close()


def EnsureGpuVramSplit() -> None:
    """Backfill: rewrite bare dual-VRAM GPU model names to their qualified form
    ('RTX 3060' + VRAM 12 → 'RTX 3060 12GB') so historical sold data lands in
    the per-variant groups. Idempotent — suffixed rows no longer match the bare
    name; rows with NULL/unknown VRAM keep the bare name (thin group, excluded
    from stats by the 5-sold floor).
    """
    conn = _get_connection()
    try:
        cur = conn.cursor()
        changed = 0
        for model, variants in _DUAL_VRAM_MODELS.items():
            placeholders = ', '.join(['%s'] * len(variants))
            cur.execute(f"""
                UPDATE Scraper.GPU
                SET Model = CONCAT(Model, ' ', VRAM, 'GB')
                WHERE Model = %s AND VRAM IN ({placeholders})
            """, (model, *variants))
            changed += cur.rowcount
        conn.commit()
        if changed:
            log.info("GPU: split %d row(s) into VRAM-variant model groups", changed)
    except mariadb.Error as e:
        log.error("EnsureGpuVramSplit failed: %s", e)
        conn.rollback()
    finally:
        conn.close()


def EnsureSellerFeedbackColumns() -> None:
    """Add EBAY.SellerFeedbackPct/-Count on installations that predate them.
    No backfill possible — feedback only exists on the scraped card, so rows
    fill in as listings are re-scraped (NULL = not seen since the migration).
    """
    DUP_COLUMN_ERRNO = 1060
    conn = _get_connection()
    try:
        cur = conn.cursor()
        for col_sql in ("SellerFeedbackPct FLOAT NULL",
                        "SellerFeedbackCount INT NULL"):
            try:
                cur.execute(f"ALTER TABLE Scraper.EBAY ADD COLUMN {col_sql}")
                conn.commit()
                log.info("EBAY: added %s column", col_sql.split()[0])
            except mariadb.Error as e:
                if getattr(e, "errno", None) != DUP_COLUMN_ERRNO:
                    log.error("EBAY: unexpected error adding %s: %s", col_sql.split()[0], e)
    finally:
        conn.close()


def EnsureMoboTable() -> None:
    """Create the MOBO satellite table (new motherboard category)."""
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS Scraper.MOBO (
                ID         BIGINT      NOT NULL PRIMARY KEY,
                Brand      VARCHAR(40),
                Chipset    VARCHAR(12),
                Socket     VARCHAR(12),
                FormFactor VARCHAR(8)
            )
        """)
        conn.commit()
    except mariadb.Error as e:
        log.error("EnsureMoboTable failed: %s", e)
    finally:
        conn.close()


def EnsureBundleColumns() -> None:
    """CPU.IsBundle / MOBO.IsBundle — a CPU+motherboard bundle lives in BOTH
    tables (dual category membership) flagged IsBundle=1, so it appears under
    both filters yet is excluded from the bare-item medians (its price covers
    two components)."""
    DUP = 1060
    conn = _get_connection()
    try:
        cur = conn.cursor()
        for tbl in ('CPU', 'MOBO'):
            try:
                cur.execute(f"ALTER TABLE Scraper.{tbl} ADD COLUMN IsBundle TINYINT(1) NOT NULL DEFAULT 0")
                conn.commit()
                log.info("%s: added IsBundle column", tbl)
            except mariadb.Error as e:
                if getattr(e, "errno", None) != DUP:
                    log.error("%s IsBundle column: %s", tbl, e)
    finally:
        conn.close()


def EnsureCpuSocketBackfill() -> None:
    """Fill CPU.Socket for rows whose title never stated it, deriving the socket
    from the model (queries.socket_for). Idempotent — only touches NULLs, so
    re-running is cheap and title-stated sockets are never overwritten."""
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""SELECT DISTINCT Model FROM Scraper.CPU
                       WHERE Socket IS NULL AND Model IS NOT NULL""")
        models = [r[0] for r in cur.fetchall()]
        filled = 0
        for model in models:
            sock = queries.socket_for(model)
            if not sock:
                continue
            cur.execute("UPDATE Scraper.CPU SET Socket=%s WHERE Model=%s AND Socket IS NULL",
                        (sock, model))
            filled += cur.rowcount
        conn.commit()
        if filled:
            log.info("CPU socket backfill: derived socket for %d row(s) across %d model(s)",
                     filled, len(models))
    except mariadb.Error as e:
        log.error("EnsureCpuSocketBackfill failed: %s", e)
    finally:
        conn.close()


def EnsureCategoryAttributes() -> None:
    """Add HDD.DriveType (Internal/External) and RAM.FormFactor (DIMM/SODIMM) on
    installations that predate them, then backfill any NULL rows by re-parsing the
    listing title. Idempotent: after the first pass no NULLs remain (new inserts
    always set the value), so the backfill query returns nothing and does nothing.
    """
    DUP_COLUMN_ERRNO = 1060
    conn = _get_connection()
    try:
        cur = conn.cursor()
        for table, col in (("HDD", "DriveType"), ("RAM", "FormFactor")):
            try:
                cur.execute(f"ALTER TABLE Scraper.{table} ADD COLUMN {col} VARCHAR(16) NULL")
                conn.commit()
                log.info("%s: added %s column", table, col)
            except mariadb.Error as e:
                if getattr(e, "errno", None) != DUP_COLUMN_ERRNO:
                    log.error("%s: unexpected error adding %s column: %s", table, col, e)

        # Backfill from titles using the shared classifiers.
        for table, col, classifier in (
            ("HDD", "DriveType", classify_drive_type),
            ("RAM", "FormFactor", classify_ram_form_factor),
        ):
            cur.execute(f"""
                SELECT s.ID, e.Title FROM Scraper.{table} s
                JOIN Scraper.EBAY e ON e.ID = s.ID
                WHERE s.{col} IS NULL
            """)
            rows = cur.fetchall()
            for ebay_id, title in rows:
                cur.execute(
                    f"UPDATE Scraper.{table} SET {col} = %s WHERE ID = %s",
                    (classifier(title or ''), ebay_id),
                )
            if rows:
                conn.commit()
                log.info("%s: backfilled %s for %d row(s)", table, col, len(rows))
    except mariadb.Error as e:
        log.error("EnsureCategoryAttributes failed: %s", e)
        conn.rollback()
    finally:
        conn.close()


def _enrich_and_gate(cur, ebay_id: int, product_type: str):
    """One item-page fetch for a newly surfaced deal: store eBay's structured
    facts and return a suppression reason (or None). Suppressed listings are
    also DELISTED from their category when eBay's own data says they were
    never the component (wrong category / for-parts condition) — that pulls
    them from the feed and the market stats, not just the notification.
    Enrichment failure returns None: never block surfacing on a bad fetch."""
    enrich = EnrichListing(ebay_id)
    if not enrich:
        return None
    suppress = None
    if enrich['end'] is not None:
        cur.execute("""
            UPDATE Scraper.EBAY SET EndTime = %s, EndTimeExact = 1 WHERE ID = %s
        """, (enrich['end'], ebay_id))
    if enrich['reserve_not_met']:
        cur.execute("UPDATE Scraper.EBAY SET ReserveNotMet = 1 WHERE ID = %s", (ebay_id,))
        suppress = 'reserve not met'
    # Purchase-route flags for the deal-page price advisor: a listing can take
    # bids, a Buy-It-Now and Best Offers all at once.
    cur.execute("UPDATE Scraper.EBAY SET HasBid = %s, HasBin = %s, HasBestOffer = %s WHERE ID = %s",
                (1 if enrich.get('has_bid') else 0,
                 1 if enrich.get('has_bin') else 0,
                 1 if enrich.get('has_best_offer') else 0, ebay_id))
    cond = enrich['condition'] or ''
    delist = False
    if re.search(r'parts|not\s+working', cond, re.IGNORECASE):
        suppress = f'condition: {cond[:40]}'
        delist = True
    if not category_matches(product_type, enrich['category_path'] or ''):
        leaf = (enrich['category_path'] or '').split(' > ')[-1]
        suppress = f'category: {leaf[:45]}'
        delist = True
    if delist:
        table = queries.CATEGORIES[product_type]['table']
        cur.execute(f"DELETE FROM Scraper.{table} WHERE ID = %s", (ebay_id,))
    cur.execute("""
        UPDATE Scraper.DealOutcomes
        SET ItemLocation = %s, Epid = %s, CategoryPath = %s,
            ItemCondition = %s, EnrichNote = %s, Watchers = %s
        WHERE EbayID = %s
    """, (enrich['location'], enrich['epid'], enrich['category_path'],
          enrich['condition'], suppress, enrich.get('watchers'), ebay_id))
    return suppress


# Prediction-surfacing experiment: among the sub-threshold deals we record but
# don't surface, flag the ones the premium model PREDICTS will still close at
# least this far under median (a real model prediction, not the current price).
# Their resolved win rate vs the live feed tests whether we could surface deals
# on predicted margin instead of current discount (the TODO'd end-state).
# Surface anything the model is confident finishes BELOW median (margin 0).
# The old 10% was wiggle-room for prediction error; the probabilistic flag now
# handles that error directly (Wilson-bounded confidence over the real ratio
# distribution), so no arbitrary cushion is needed.
PRED_SURFACE_MARGIN = float(os.environ.get('PRED_SURFACE_MARGIN', '0'))
# The flag is probabilistic, not a point estimate: surface only when the
# cohort's realized ratio distribution says the deal closes >= the margin under
# median with at least this (Wilson-lower-bounded) confidence, and only when the
# cohort has enough resolved samples to estimate that tail.
PRED_SURFACE_CONFIDENCE = float(os.environ.get('PRED_SURFACE_CONFIDENCE', '0.75'))
PRED_PROB_MIN_SAMPLES = int(os.environ.get('PRED_PROB_MIN_SAMPLES', '8'))


def SurfaceDeals(window_hours: int = 2, min_discount: float = 20,
                 record_discount: float | None = None) -> list[dict]:
    """Detect current deals server-side and record first sightings.

    Runs the shared deal query for every category, INSERT IGNOREs each hit
    into DealOutcomes, and returns ONLY the real deals recorded for the
    first time (rowcount==1) so the caller can notify exactly once per deal.

    Recording floor: when record_discount is set below min_discount, the query
    runs at the lower threshold and rows in the [record, min) band are recorded
    flagged NearMiss=1 — never notified, and excluded from the outcomes
    scoreboard and from premium training (they are below the notify line). This
    wide recording feeds the prediction-surfacing experiment; the flag's only
    job now is to keep sub-threshold deals out of the headline stats.

    Prediction-surfacing cohort: any recorded row (regardless of current
    discount) the model is confident closes >= PRED_SURFACE_MARGIN under median
    is flagged PredMargin=1 — the parallel experiment for predicted-margin-first
    surfacing.

    This replaces the old browser-driven surfacing in /api/deals — deals are
    now captured even when nobody has the dashboard open.
    """
    new_deals = []
    near_misses = 0
    query_discount = (min_discount if record_discount is None
                      else min(record_discount, min_discount))
    premiums = GetSnipePremiums()
    dists = GetSnipeDistributions()   # ratio distributions for the probabilistic flag
    conn = _get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        ins = conn.cursor()
        for product_type in queries.CATEGORIES:
            try:
                cur.execute(queries.build_deals_query(product_type, window_hours, query_discount))
                rows = cur.fetchall()
            except mariadb.Error as e:
                log.error("SurfaceDeals: %s query failed: %s", product_type, e)
                continue
            queries.annotate_predictions(rows, product_type, premiums)
            for row in rows:
                # Trajectory observation for EVERY live in-window row, every
                # pass — not just first sightings (see EnsureDealSnapshots).
                _record_snapshot(ins, row['ID'], round(row['CurrentPrice'] * 100),
                                 row.get('Bids'), row.get('EndTime'))
                label = queries.model_label_for_row(product_type, row)
                near_miss = 1 if float(row['DiscountPct']) < min_discount else 0
                # SurfacedPrice is the whole-lot price, so the stored market
                # value must be whole-lot too (median × quantity) — outcome
                # win/loss math compares FinalPrice against AvgMarketPrice.
                qty = int(row.get('Quantity') or 1)
                # Prediction-surfacing cohort: flag deals the model expects to
                # close >= PRED_SURFACE_MARGIN under median — regardless of
                # current discount. But PROBABILISTICALLY, not on a point
                # estimate: a listing at lot price c with per-unit median m
                # closes >= X% under median iff its final/surfaced ratio r
                # <= (1-X)·m·qty / c. We take the empirical share of the
                # cohort's realized ratios at/under that threshold, Wilson-
                # lower-bounded for thin samples, and flag only when that
                # confidence clears the bar. This folds in the model's bias
                # (real outcomes) and its spread (noisy cohorts rarely clear it).
                pred_margin = 0
                if row.get('PremiumSamples'):
                    cur_lot = float(row.get('CurrentPrice') or 0)
                    med_unit = float(row.get('AvgMarketPrice') or 0)
                    end = row.get('EndTime')
                    _now = datetime.now(timezone.utc).replace(tzinfo=None)
                    hours = (max((end - _now).total_seconds() / 3600.0, 0.0)
                             if end is not None and hasattr(end, 'timestamp') else None)
                    if cur_lot > 0 and med_unit > 0:
                        threshold = (1.0 - PRED_SURFACE_MARGIN / 100.0) * med_unit * qty / cur_lot
                        prob, n = queries.prob_below(
                            dists, product_type, int(row.get('Bids') or 0), hours, threshold)
                        if (prob is not None and n >= PRED_PROB_MIN_SAMPLES
                                and prob >= PRED_SURFACE_CONFIDENCE):
                            pred_margin = 1
                # PredictedFinal is stored at surfacing so resolved outcomes
                # can grade the premium model itself (predicted vs actual).
                predicted = (int(round(row['PredictedFinalPrice'] * 100))
                             if row.get('PremiumSamples') else None)
                ins.execute("""
                    INSERT IGNORE INTO Scraper.DealOutcomes
                        (EbayID, Category, Model, SurfacedPrice, AvgMarketPrice, DiscountPct, BidCount, EndTime, PredictedFinal, NearMiss, PredMargin)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    row['ID'],
                    product_type.upper(),
                    label,
                    int(round(row['CurrentPrice'] * 100)),
                    int(round(row['AvgMarketPrice'] * qty * 100)),
                    float(row['DiscountPct']),
                    int(row.get('Bids') or 0),
                    row['EndTime'],
                    predicted,
                    near_miss,
                    pred_margin,
                ))
                if ins.rowcount == 1:
                    if near_miss:
                        near_misses += 1
                    else:
                        suppress = _enrich_and_gate(ins, row['ID'], product_type)
                        if suppress:
                            log.info("Deal %s suppressed by enrichment: %s",
                                     row['ID'], suppress)
                        else:
                            row['_label'] = label
                            row['_category'] = product_type.upper()
                            new_deals.append(row)
        conn.commit()
        if new_deals or near_misses:
            log.info("SurfaceDeals: %d new deal(s), %d sub-threshold recorded",
                     len(new_deals), near_misses)
        return new_deals
    except Exception as e:
        log.error("SurfaceDeals error: %s", e)
        conn.rollback()
        return []
    finally:
        conn.close()


def EnsureNotifyRecipients() -> None:
    """Create the NotifyRecipients table and bootstrap it from env vars.

    On first run, if the table is empty and legacy HA_URL/HA_TOKEN/
    HA_NOTIFY_SERVICE env vars are set, they are migrated into a default
    all-categories recipient so existing deployments keep notifying without
    manual setup.
    """
    conn = _get_connection()
    try:
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
        cur.execute("SELECT COUNT(*) FROM Scraper.NotifyRecipients")
        if cur.fetchone()[0] == 0:
            url = os.environ.get('HA_URL', '').rstrip('/')
            token = os.environ.get('HA_TOKEN', '')
            service = os.environ.get('HA_NOTIFY_SERVICE', '')
            if url and token and service:
                cur.execute("""
                    INSERT INTO Scraper.NotifyRecipients (Name, HaUrl, HaToken, NotifyService)
                    VALUES (%s, %s, %s, %s)
                """, ('Cam', url, token, service))
                conn.commit()
                log.info("NotifyRecipients: bootstrapped default recipient from env")
    except Exception as e:
        log.error("EnsureNotifyRecipients failed: %s", e)
    finally:
        conn.close()


# Every notification in the app is a SUBSCRIPTION: a user-owned rule (category
# + scope + listing type + trigger) delivered to that user's own Home Assistant
# endpoint. This one query is the source of truth for every push — the auction
# feed, BIN finds and model price alerts are all just subscriptions with
# different trigger kinds. The endpoint is joined from the owning Users row, so
# a user with notifications off or no endpoint configured simply gets no rows.
_SUB_ENDPOINT_JOIN = """
        FROM Scraper.PriceAlerts a
        JOIN Scraper.Users u ON u.ID = a.UserID
        WHERE a.Enabled = 1 AND COALESCE(u.NotifyEnabled, 1) = 1
          AND u.HaUrl IS NOT NULL AND u.HaUrl <> ''
          AND u.HaToken IS NOT NULL AND u.HaToken <> ''
          AND u.NotifyService IS NOT NULL AND u.NotifyService <> ''
"""


def GetSubscriptions(trigger_kinds: tuple | None = None) -> list[dict]:
    """Active subscriptions with their owner's HA endpoint joined in.

    Each row carries UserID + endpoint (HaUrl/HaToken/NotifyService) so a push
    goes straight to the owner — no separate recipient concept. Optionally
    filter to given trigger kinds ('discount_pct' | 'listing_price' |
    'median_price'). [] on any error (PriceAlerts/Users columns may not exist
    on the scraper's first boot — the web container creates them)."""
    try:
        conn = _get_connection()
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(f"""
                SELECT a.ID, a.UserID, a.Category, a.ScopeKind, a.GroupParams,
                       a.ListingType, a.Kind, a.TargetPrice, a.MinDiscount,
                       a.Label, a.LastFiredAt,
                       u.HaUrl, u.HaToken, u.NotifyService
                {_SUB_ENDPOINT_JOIN}
            """)
            rows = cur.fetchall()
        except mariadb.Error:
            return []
        finally:
            conn.close()
        if trigger_kinds is not None:
            rows = [r for r in rows if r['Kind'] in trigger_kinds]
        return rows
    except Exception as e:
        log.error("GetSubscriptions failed: %s", e)
        return []


def GetAdminEndpoints() -> list[dict]:
    """Endpoints of admin users with notifications on — the audience for system
    notices (the data-quality audit), which aren't tied to a subscription."""
    try:
        conn = _get_connection()
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute("""
                SELECT Username AS Name, HaUrl, HaToken, NotifyService
                FROM Scraper.Users
                WHERE IsAdmin = 1 AND COALESCE(NotifyEnabled, 1) = 1
                  AND HaUrl <> '' AND HaToken <> '' AND NotifyService <> ''
            """)
            return cur.fetchall()
        except mariadb.Error:
            return []
        finally:
            conn.close()
    except Exception as e:
        log.error("GetAdminEndpoints failed: %s", e)
        return []


def EnsureListingType() -> None:
    """EBAY.ListingType ('auction' | 'bin'). The BIN watcher needs fixed-price
    rows scoreable separately: no bids, usually no end time, and the listed
    price IS the final price. Every pre-existing row is an auction (the full
    scrape runs with LH_Auction=1), so the default backfills correctly."""
    DUP_COLUMN_ERRNO = 1060
    conn = _get_connection()
    try:
        cur = conn.cursor()
        try:
            cur.execute("""
                ALTER TABLE Scraper.EBAY
                ADD COLUMN ListingType VARCHAR(8) NOT NULL DEFAULT 'auction'
            """)
            conn.commit()
            log.info("EBAY: added ListingType column")
        except mariadb.Error as e:
            if getattr(e, "errno", None) != DUP_COLUMN_ERRNO:
                raise
    except mariadb.Error as e:
        log.error("EnsureListingType failed: %s", e)
    finally:
        conn.close()


def EnsureBinNotified() -> None:
    """Dedupe table for the BIN watcher: one row per fixed-price find ever
    pushed. BIN deals have no outcome to track (no bidding — the discount is
    real at first sight), so this replaces DealOutcomes as the once-only gate."""
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS Scraper.BinNotified (
                EbayID     BIGINT   NOT NULL PRIMARY KEY,
                NotifiedAt DATETIME NOT NULL
            )
        """)
        conn.commit()
    except mariadb.Error as e:
        log.error("EnsureBinNotified failed: %s", e)
    finally:
        conn.close()


def SurfaceBinDeals(min_discount: float = 25) -> list[dict]:
    """Detect fresh Buy-It-Now bargains; return only the never-notified ones.

    No prediction gate and no outcome tracking — a fixed price can't be bid
    past its value, so the discount on screen is the discount you get.
    Dedupe is INSERT IGNORE into BinNotified. New finds pass the same
    item-page enrichment gate as auction deals (wrong-category and for-parts
    listings are delisted, reserve is irrelevant for BIN) so a £30 'RTX 4090'
    backplate never pings a phone.
    """
    finds = []
    conn = _get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        ins = conn.cursor()
        for product_type in queries.CATEGORIES:
            try:
                cur.execute(queries.build_bin_deals_query(product_type, min_discount))
                rows = cur.fetchall()
            except mariadb.Error as e:
                log.error("SurfaceBinDeals: %s query failed: %s", product_type, e)
                continue
            for row in rows:
                ins.execute("""
                    INSERT IGNORE INTO Scraper.BinNotified (EbayID, NotifiedAt)
                    VALUES (%s, NOW())
                """, (row['ID'],))
                if ins.rowcount != 1:
                    continue
                suppress = _enrich_and_gate(ins, row['ID'], product_type)
                if suppress:
                    log.info("BIN find %s suppressed by enrichment: %s", row['ID'], suppress)
                    continue
                row['_label'] = queries.model_label_for_row(product_type, row)
                row['_category'] = product_type.upper()
                finds.append(row)
        conn.commit()
        if finds:
            log.info("SurfaceBinDeals: %d new BIN find(s)", len(finds))
        return finds
    except Exception as e:
        log.error("SurfaceBinDeals error: %s", e)
        conn.rollback()
        return []
    finally:
        conn.close()


_bin_cfg_cache = {'at': 0.0, 'val': None}


def GetBinConfig() -> dict:
    """BIN watcher settings: AppConfig (Settings page) over env defaults.

    {'enabled': bool, 'scan_minutes': int, 'min_discount': float}. Cached 60s
    — the scheduler consults this every 10s tick, and a settings change
    applying within a minute is as live as anyone needs. Tolerates AppConfig
    not existing yet (the web container creates it)."""
    now = time.time()
    if _bin_cfg_cache['val'] is not None and now - _bin_cfg_cache['at'] < 60:
        return _bin_cfg_cache['val']
    cfg = {
        'enabled': os.environ.get('BIN_ENABLED', '1').lower() not in ('0', 'false', ''),
        'scan_minutes': int(os.environ.get('BIN_SCAN_MINUTES', '30')),
        'min_discount': float(os.environ.get('BIN_MIN_DISCOUNT', '25')),
        'filters': {},
    }
    try:
        import json
        conn = _get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT K, V FROM Scraper.AppConfig WHERE K IN "
                        "('bin_enabled', 'bin_scan_minutes', 'bin_min_discount', 'bin_filters')")
            stored = dict(cur.fetchall())
        finally:
            conn.close()
        if 'bin_enabled' in stored:
            cfg['enabled'] = stored['bin_enabled'] == '1'
        if 'bin_scan_minutes' in stored:
            cfg['scan_minutes'] = max(5, int(stored['bin_scan_minutes']))
        if 'bin_min_discount' in stored:
            cfg['min_discount'] = float(stored['bin_min_discount'])
        if 'bin_filters' in stored:
            cfg['filters'] = json.loads(stored['bin_filters'] or '{}')
    except Exception:
        pass
    _bin_cfg_cache.update(at=now, val=cfg)
    return cfg


def bin_find_passes_filters(label: str, category: str, filters: dict) -> bool:
    """Per-category model filter from the BIN watcher settings.

    filters maps category key → comma-separated terms ("6TB, 8TB, 10TB");
    a find notifies only when its model label contains one of the terms,
    case-insensitively. No entry (or blank) = everything passes.
    """
    raw = (filters or {}).get((category or '').lower()) or ''
    terms = [t.strip().lower() for t in raw.split(',') if t.strip()]
    if not terms:
        return True
    lab = (label or '').lower()
    return any(t in lab for t in terms)


# Public base URL of the web UI (e.g. http://192.168.1.104:5010). When set,
# notification deep links open OUR deal page (market context, max-bid advisor,
# one tap further to eBay) instead of eBay directly.
APP_BASE_URL = os.environ.get('APP_BASE_URL', '').rstrip('/')


def deal_page_url(ebay_id, fallback: str | None = None) -> str | None:
    """Deep link for notifications: the deal page when APP_BASE_URL is set,
    else the given fallback (usually the raw eBay URL)."""
    return f"{APP_BASE_URL}/deal/{ebay_id}" if APP_BASE_URL else fallback


# Alert cooldowns: a listing sitting under the target must not ping every
# scrape cycle, and medians move slowly — repeat pushes are noise, not news.
_ALERT_COOLDOWN_HOURS = {'listing_price': 6, 'median_price': 24}


def alert_listing_relevant(hit: dict, target_pounds: float, premiums: dict,
                           category: str) -> tuple[bool, float]:
    """Second gate for listing_below hits (after the SQL's BIN-or-ending-soon
    filter): is this a price you could actually pay?

    BIN — the listed price IS the final price: relevant as-is.
    Auction (already inside its final window) — confirm the outcome-calibrated
    PREDICTED per-unit final still clears the target: a £10-now auction
    predicted to close at £18 against a £15 target is noise, not news.
    Returns (relevant, predicted_per_unit).
    """
    per_unit = float(hit['PerUnitPrice'])
    if hit.get('ListingType') == 'bin':
        return True, per_unit
    bids = int(hit.get('Bids') or 0)
    end = hit.get('EndTime')
    hours = None
    if end is not None and hasattr(end, 'timestamp'):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        hours = max((end - now).total_seconds() / 3600.0, 0.0)
    ratio, _ = queries.premium_for(premiums or {}, category, bids, hours)
    predicted = round(per_unit * ratio, 2)
    return predicted < target_pounds, predicted


def push_notification(endpoint: dict, title: str, message: str,
                      url: str | None = None, tag: str | None = None) -> bool:
    """One Home Assistant notification to one endpoint (a dict carrying HaUrl /
    HaToken / NotifyService — a subscription row or an admin-endpoint row).
    The single push primitive every notification path funnels through."""
    try:
        data = {}
        if url:
            data['url'] = url
        if tag:
            data['tag'] = tag
        requests.post(
            f"{endpoint['HaUrl'].rstrip('/')}/api/services/notify/{endpoint['NotifyService']}",
            headers={"Authorization": f"Bearer {endpoint['HaToken']}"},
            json={"title": title, "message": message, "data": data},
            timeout=10,
        )
        return True
    except Exception as e:
        log.warning("HA push to %s failed: %s", endpoint.get('Name') or endpoint.get('Label'), e)
        return False


def EvaluateSubscriptions() -> int:
    """Evaluate the £-target subscriptions (model-page 'Alert me') against
    current data. The discount-% subscriptions (auction feed + BIN watches)
    fire event-driven at surfacing time (scheduler.notify_new_deals /
    notify_bin_finds); these two need a poll because they trip on state, not a
    new listing:

    listing_price — a listing in the subscription's market group GENUINELY
    available under the target (deal-feed trust gates apply): Buy-It-Now at that
    price, or an auction in its final window whose predicted final also clears
    the target — a low-start auction with days left is never a hit.
    median_price — the group's 120-day sold median itself dropped under target.

    Each subscription pushes to its owner's endpoint, then stamps LastFiredAt
    for the cooldown. Returns the number fired. These are scope='group'
    subscriptions; scope is irrelevant to the SQL (the group columns come
    straight from GroupParams)."""
    subs = GetSubscriptions(trigger_kinds=('listing_price', 'median_price'))
    if not subs:
        return 0
    conn = _get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        import json
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        fired = 0
        upd = conn.cursor()
        # Premiums confirm auction hits (predicted final vs target); one
        # fetch covers every subscription this run.
        premiums = (GetSnipePremiums()
                    if any(s['Kind'] != 'median_price' for s in subs) else {})
        for a in subs:
            cooldown = _ALERT_COOLDOWN_HOURS.get(a['Kind'], 6)
            if a['LastFiredAt'] and (now - a['LastFiredAt']) < timedelta(hours=cooldown):
                continue
            cat = (a['Category'] or '').lower()
            if cat not in queries.CATEGORIES or a['TargetPrice'] is None:
                continue
            try:
                group = json.loads(a['GroupParams'] or '{}')
            except ValueError:
                continue
            target_pounds = float(a['TargetPrice']) / 100
            label = a['Label'] or cat.upper()
            title = message = url = None
            try:
                if a['Kind'] == 'median_price':
                    sql, binds = queries.group_median_query(cat, group)
                    cur.execute(sql, binds)
                    row = cur.fetchone()
                    if row and row['MedPrice'] is not None and float(row['MedPrice']) < target_pounds:
                        title = f"Price alert: {label}"
                        message = (f"120-day median now £{float(row['MedPrice']):.2f} "
                                   f"(target £{target_pounds:.2f}, {row['N']} sales)")
                else:  # listing_price
                    sql, binds = queries.group_live_below_query(cat, group, target_pounds)
                    cur.execute(sql, binds)
                    hits = []
                    for h in cur.fetchall():
                        ok, predicted = alert_listing_relevant(h, target_pounds, premiums, cat)
                        if ok:
                            h['_predicted'] = predicted
                            hits.append(h)
                    if hits:
                        h = hits[0]
                        qty = int(h['Quantity'] or 1)
                        per_unit = f"£{float(h['PerUnitPrice']):.2f}"
                        if qty > 1:
                            per_unit += f"/unit (×{qty})"
                        if h['ListingType'] == 'bin':
                            kind_txt = 'Buy It Now'
                        else:
                            kind_txt = (f"auction ending soon, {h['Bids'] or 0} bid(s), "
                                        f"predicted ~£{h['_predicted']:.2f}")
                        title = f"Price alert: {label} under £{target_pounds:.2f}"
                        message = f"{h['Title'][:90]} — {per_unit}, {kind_txt}"
                        if len(hits) > 1:
                            message += f" (+{len(hits) - 1} more)"
                        url = deal_page_url(h['ID'], h['URL'])
            except mariadb.Error as e:
                log.error("EvaluateSubscriptions: sub %s query failed: %s", a['ID'], e)
                continue
            if not title:
                continue
            sent = push_notification(a, title, message, url=url,
                                     tag=f"dealfinder-alert-{a['ID']}")
            if sent:
                upd.execute("UPDATE Scraper.PriceAlerts SET LastFiredAt = NOW() WHERE ID = %s",
                            (a['ID'],))
                conn.commit()
                fired += 1
        if fired:
            log.info("EvaluateSubscriptions: %d price alert(s) fired", fired)
        return fired
    except Exception as e:
        log.error("EvaluateSubscriptions error: %s", e)
        return 0
    finally:
        conn.close()


def _audit_card_html(ebay_id, title, price_pence):
    """One synthetic search-result card for re-parsing a stored row's title."""
    safe = (title or '').replace('<', '').replace('>', '')
    return (f'<div class="su-card-container su-card-container--horizontal">'
            f'<a href="https://www.ebay.co.uk/itm/{ebay_id}">x</a>'
            f'<a class="su-link su-item-card__title"><span>{safe}</span></a>'
            f'<span class="su-item-card__price">£{(price_pence or 0) / 100:.2f}</span>'
            f'</div>')


def audit_data_quality(outlier_ratio: float = 2.5, min_group: int = 5,
                       min_median: float = 10.0, cap: int = 25) -> dict:
    """Read-only self-check: catch classification/lot pollution BEFORE it skews
    a median. Two nets:

    ① Re-parse: every satellite row's title is run back through the CURRENT
       parser for its category. If the parser now REJECTS it, the row is
       pollution that slipped in before a gate existed (a laptop in GPU, a
       server in SSD). If it accepts with a DIFFERENT quantity, the lot is
       mislabelled. This needs no bespoke rules — it inherits every gate.
    ② Median outlier: for each market group with >= min_group sold singles,
       any single priced above outlier_ratio x the group median is flagged —
       the general 'something's off' detector that catches pollution types
       no rule covers yet (that's how the RTX-3050 laptops would surface).

    Returns {'reparse_rejects', 'lot_mismatch', 'gpu_lots', 'price_outliers':
    [...]} — dict lists of findings (each capped to `cap`), plus 'counts'.
    """
    from bs4 import BeautifulSoup
    # name-mangling only happens inside class bodies; at module scope the fn is
    # stored verbatim as "__ParseItems" (same key the tests use via vars()).
    parse_items = globals()['__ParseItems']

    findings = {'reparse_rejects': [], 'lot_mismatch': [], 'gpu_lots': [],
                'price_outliers': [], 'counts': {}}
    conn = _get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        for cat, cfg in queries.CATEGORIES.items():
            tbl = cfg['table']
            cur.execute(f"""SELECT e.ID, e.Title, e.Price, COALESCE(e.Quantity,1) AS Qty
                            FROM Scraper.{tbl} t JOIN Scraper.EBAY e ON e.ID=t.ID
                            WHERE e.Title IS NOT NULL""")
            rows = cur.fetchall()
            findings['counts'][cat] = len(rows)
            for r in rows:
                if cat == 'gpu' and int(r['Qty']) > 1:
                    if len(findings['gpu_lots']) < cap:
                        findings['gpu_lots'].append(
                            {'id': r['ID'], 'title': r['Title'][:80], 'qty': int(r['Qty'])})
                soup = BeautifulSoup(_audit_card_html(r['ID'], r['Title'], r['Price']),
                                     'html.parser')
                try:
                    parsed = parse_items(soup, 'audit', cat.upper())
                except Exception:
                    parsed = []
                if not parsed:
                    if len(findings['reparse_rejects']) < cap:
                        findings['reparse_rejects'].append(
                            {'cat': cat, 'id': r['ID'], 'title': r['Title'][:80]})
                elif int(parsed[0].get('quantity') or 1) != int(r['Qty']):
                    if len(findings['lot_mismatch']) < cap:
                        findings['lot_mismatch'].append(
                            {'cat': cat, 'id': r['ID'], 'title': r['Title'][:80],
                             'stored': int(r['Qty']), 'parsed': int(parsed[0].get('quantity') or 1)})

        # ② median outliers per group (single-unit sold, recency window)
        for cat, cfg in queries.CATEGORIES.items():
            a = cfg['alias']
            cols = ', '.join(c for c, _ in cfg['group_cols'])
            cur.execute(f"""
                WITH {queries._median_ctes(cfg)}
                SELECT {cols}, MedPrice, SoldCount
                FROM RawStats WHERE SoldCount >= {min_group}
            """)
            groups = cur.fetchall()
            for g in groups:
                med = float(g['MedPrice'] or 0)
                # a broken-LOW median (pennies from cheap junk) makes every
                # real item look like a high outlier — skip those groups so
                # the net only fires on genuine high pollution in a sane group
                if med < min_median:
                    continue
                params = {c: g[c] for c, _ in cfg['group_cols']}
                cond, vals = queries.model_where(cat, {k: ('' if v is None else v)
                                                       for k, v in params.items()})
                cur.execute(f"""
                    SELECT e.ID, e.Title, ROUND((e.Price+COALESCE(e.Shipping,0))/100,2) AS Eff
                    FROM Scraper.{cfg['table']} {a} JOIN Scraper.EBAY e ON e.ID={a}.ID
                    WHERE e.SoldDate IS NOT NULL AND COALESCE(e.Quantity,1)=1
                      AND e.SoldDate > NOW() - INTERVAL {queries.MARKET_STATS_DAYS} DAY
                      AND {cond} AND (e.Price+COALESCE(e.Shipping,0))/100 > {med * outlier_ratio}
                    ORDER BY Eff DESC LIMIT 5
                """, vals)
                for o in cur.fetchall():
                    if len(findings['price_outliers']) < cap:
                        findings['price_outliers'].append(
                            {'cat': cat, 'id': o['ID'], 'title': o['Title'][:80],
                             'price': float(o['Eff']), 'median': round(med, 2),
                             'label': queries.model_label_for_row(cat, params)})
        return findings
    finally:
        conn.close()


def PruneStaleListings(days: int = 14) -> int:
    """Delete zombie ACTIVE listings: never sold, ended more than `days` ago,
    and not referenced by DealOutcomes.

    These are ordinary auctions that ended while unobserved (e.g. downtime) —
    they never resolve, inflate the active-listings count, and add join weight.
    Sold rows are never touched: they ARE the price history. Satellite rows
    are removed first to satisfy the FK constraints.

    Returns the number of EBAY rows removed.
    """
    conn = _get_connection()
    try:
        cur = conn.cursor()
        params = (days,)
        zombie_cond = """
            e.SoldDate IS NULL
            AND e.EndTime IS NOT NULL
            AND e.EndTime < NOW() - INTERVAL %s DAY
            AND e.ID NOT IN (SELECT EbayID FROM Scraper.DealOutcomes)
        """
        for sat in ('GPU', 'CPU', 'HDD', 'RAM', 'SSD'):
            cur.execute(f"""
                DELETE s FROM Scraper.{sat} s
                JOIN Scraper.EBAY e ON e.ID = s.ID
                WHERE {zombie_cond}
            """, params)
        cur.execute(f"""
            DELETE e FROM Scraper.EBAY e
            WHERE {zombie_cond}
        """, params)
        removed = cur.rowcount
        conn.commit()
        if removed:
            log.info("PruneStaleListings: removed %d zombie listing(s) ended >%dd ago", removed, days)
        return removed
    except Exception as e:
        log.error("PruneStaleListings failed: %s", e)
        conn.rollback()
        return 0
    finally:
        conn.close()


# ── snipe-premium model ───────────────────────────────────────────────────────
# Bucket + ratio math lives in queries.py (the web image needs it too);
# these aliases keep this module's historical API for callers and tests.

_bid_bucket = queries.bid_bucket
_median_ratios = queries.median_ratios


def GetSnipePremiums(min_samples: int = 5) -> dict:
    """Learn how far above their surfaced price our deals actually close.

    Median FinalPrice/SurfacedPrice ratio from resolved DealOutcomes (sold,
    not ended-unsold), per category and bid bucket. Feeds deal-row predictions
    (queries.annotate_predictions) and notification gating. Ratios are
    unit-agnostic (both prices in pence). {} on error or thin history.
    """
    try:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            cur.execute(queries.SNIPE_PREMIUM_QUERY)
            rows = cur.fetchall()
        finally:
            conn.close()
        return _median_ratios(rows, min_samples)
    except Exception as e:
        log.error("GetSnipePremiums failed: %s", e)
        return {}


def GetSnipeDistributions(min_samples: int = 5) -> dict:
    """Full realized final/surfaced ratio distribution per cohort (same source
    and cohorts as GetSnipePremiums). Feeds the probabilistic surfacing flag,
    which asks P(closes at/under target) rather than trusting a point median.
    {} on error or thin history."""
    try:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            cur.execute(queries.SNIPE_PREMIUM_QUERY)
            rows = cur.fetchall()
        finally:
            conn.close()
        return queries.ratio_distributions(rows, min_samples)
    except Exception as e:
        log.error("GetSnipeDistributions failed: %s", e)
        return {}