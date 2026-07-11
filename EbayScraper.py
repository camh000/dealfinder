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
    #   False       → active listings          (&_sop=1)
    if alreadySold == 'completed':
        cache_suffix = 'completed'
        alreadySoldString = '&LH_Complete=1'
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
_DUAL_VRAM_MODELS = {
    'GTX 1060':    (3, 6),
    'RTX 2060':    (6, 12),
    'RTX 3050':    (6, 8),
    'RTX 3060':    (8, 12),
    'RTX 3080':    (10, 12),
    'RTX 4060 TI': (8, 16),
    'RTX 5060 TI': (8, 16),
    'RX 570':      (4, 8),
    'RX 580':      (4, 8),
    'RX 7600':     (8, 16),
    'RX 9060 XT':  (8, 16),
    'ARC A770':    (8, 16),
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
]

# Above this a "quantity" is more likely a misparse than a real lot; treat the
# listing as a single so it prices itself out of the deal feed (false negative
# beats a 50x phantom valuation).
LOT_MAX_QTY = 30

_LOT_RISK_RE = re.compile(
    r'\buntested\b|\bspares?\b|\brepairs?\b|\bfaulty\b|\bnot\s+working\b|'
    r'\bfor\s+parts\b|\bas[-\s]is\b|\bdead\b|\bdamaged\b|\bbroken\b|'
    r'\bnon[-\s]?functional\b', re.IGNORECASE)

# Accessory listings masquerading as the component: "RTX 4090 Founders
# Edition Heatsink, with fans and box (no GPU)" parses as a 4090 and shows
# as 98% off. Only explicit tells — a real card saying "with backplate"
# must not be skipped.
_ACCESSORY_RE = re.compile(
    r'no\s+gpu\b|not\s+included\b|\bno\s+(?:graphics\s+)?card\b|\bempty\s+box\b|'
    r'\b(?:box|heatsink|cooler|fans?|shroud|backplate|bracket|stand)\s*only\b|'
    r'\bheatsink\s*(?:&|and|\+|,)\s*(?:box|fans?|shroud)\b|'
    r'\bbox\s*(?:&|and|\+|,)\s*(?:manual|heatsink)\b', re.IGNORECASE)


def extract_lot_quantity(title: str) -> int:
    """Number of units in a multi-item listing; 1 when not confidently a lot."""
    for pat in _LOT_QTY_PATTERNS:
        m = pat.search(title or '')
        if m:
            qty = int(m.group(1))
            if 2 <= qty <= LOT_MAX_QTY:
                return qty
    return 1


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
            price = __ParseRawPrice(price_el.get_text(strip=True))
            if price is None:
                raise ValueError("Price pattern not found in text")
        except (AttributeError, TypeError, ValueError) as e:
            log.warning("[%s] Skipping item '%s...' - could not parse price: %s", query, title[:40], e)
            continue

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
        drive_type = ram_format = pcie_gen = kit_config = None
        quantity = 1

        if productType == 'GPU':

            BRANDS = [
                "ASUS", "MSI", "GIGABYTE", "ZOTAC", "PALIT",
                "EVGA", "PNY", "SAPPHIRE", "XFX", "INNO3D",
                "GAINWARD", "AORUS", "SPARKLE", "ASROCK", "ACER"
            ]

            # Flexible GPU model pattern
            model_pattern = re.compile(
                r'(?P<series>RTX|GTX|TITAN|RX)\s*'      # series
                r'(?P<number>\d{2,4})\s*'               # number
                r'(?P<variant>Ti|SUPER|Ti\s*SUPER|XT|XTX)?',  # optional variant
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
                                        'poweredge', 'proliant', 'supermicro', 'thinkserver',
                                        'rack server', 'tower server', 'server bundle'])
                or (bool(re.search(r'\d+\s*gb\s*(ddr\d?|ram)', _tl))
                    and bool(re.search(r'\d+\s*(tb|gb)\s*(ssd|nvme|hdd|m\.2)', _tl)))
                or (('motherboard' in _tl or ' mobo' in _tl)
                    and bool(re.search(r'\bddr\d|\bram\b', _tl)))
            )
            if _is_system:
                log.debug("[%s] Skipping system listing: %s", query, title[:60])
                continue

            # Matched pairs/quads ("2x Xeon E5-2690" for dual-socket boards)
            # are multi-unit listings — same per-unit treatment as HDD lots.
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
            socket = extract_socket(title)
            cores  = extract_cores(title)

        elif productType == 'HDD':

            # Flash media isn't a hard drive — the job-lot search in particular
            # returns USB-stick lots that would otherwise pollute HDD groups.
            _tl = title.lower()
            if any(k in _tl for k in _FLASH_MEDIA_KEYWORDS):
                log.debug("[%s] Skipping flash media: %s", query, title[:60])
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

            brand       = extract_hdd_brand(title)
            model       = None
            vram        = None
            socket      = None
            cores       = None
            capacity_gb = extract_capacity_gb(title)
            interface   = extract_interface(title)
            form_factor = extract_form_factor(title)
            rpm         = extract_rpm(title)
            drive_type  = classify_drive_type(title)
            quantity    = extract_lot_quantity(title)

        elif productType == 'SSD':

            _tl = title.lower()
            # Flash media isn't an SSD; SSHDs are hybrids priced like neither;
            # whole machines ("laptop, 1TB SSD") mention SSDs constantly.
            if any(k in _tl for k in _FLASH_MEDIA_KEYWORDS) or 'sshd' in _tl:
                log.debug("[%s] Skipping non-SSD storage: %s", query, title[:60])
                continue
            _is_system_ssd = (
                any(k in _tl for k in ['gaming pc', 'desktop pc', 'mini pc',
                                        'all-in-one', 'complete pc', 'pc bundle'])
                or (('laptop' in _tl or 'notebook' in _tl)
                    and bool(re.search(r'\d+\s*gb\s*(ddr\d?|ram)', _tl)))
            )
            if _is_system_ssd:
                log.debug("[%s] Skipping system listing: %s", query, title[:60])
                continue

            ssd_cap_pattern = re.compile(r'(\d+(?:\.\d+)?)\s*(TB|GB)', re.IGNORECASE)

            def extract_ssd_capacity_gb(title: str):
                m = ssd_cap_pattern.search(title)
                if m:
                    val, unit = float(m.group(1)), m.group(2).upper()
                    return int(val * 1000) if unit == 'TB' else int(val)
                return None

            capacity_gb = extract_ssd_capacity_gb(title)
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
            quantity = extract_lot_quantity(title)

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
                ])
                or (('laptop' in _tl or 'notebook' in _tl)
                    and bool(re.search(r'\d+\s*(tb|gb)\s*(ssd|nvme|hdd|emmc)', _tl)))
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

        else:
            brand = ''
            model = ''
            vram  = None

        log.debug("Parsed: brand=%s model=%s vram=%s", brand, model, vram)

        itemData = {
            'id': id,
            'title': title,
            'price': price,
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

def _upload(cur, p: Product, product_type: str) -> int:
    """Returns the EBAY rowcount: 1 = inserted, 2 = updated, 0 = no change."""
    # LastSeenAt = the last time a scrape actually saw this listing on eBay.
    # Seller-cancelled listings vanish from search but keep a future EndTime —
    # the deal queries use this stamp to drop them instead of showing phantom
    # deals until the original end time.
    cur.execute("""
        INSERT INTO EBAY (ID, Title, Price, Shipping, Quantity, Bids, EndTime, SoldDate, URL,
                          SellerFeedbackPct, SellerFeedbackCount, LastSeenAt)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
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
            LastSeenAt = NOW();
        """, (p.id, p.title, p.price * 100, int(round((p.shipping or 0) * 100)),
              p.quantity or 1, p.bid_count, p.time_end, p.sold_date, p.url,
              p.feedback_pct, p.feedback_count)
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
            INSERT INTO CPU (ID, Brand, Model, Socket, Cores)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                Brand = VALUES(Brand),
                Model = VALUES(Model),
                Socket = VALUES(Socket),
                Cores = VALUES(Cores);
            """, (p.id, p.brand, p.model, p.socket, p.cores)
        )
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
    return ebay_rc

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


def ScrapeAndUpload(query_list: list[str], product_type: str, country='us', condition='all', listing_type='all', cache=False):
    conn = _get_connection()
    cur = conn.cursor()

    try:
        inserted = updated = 0
        for query in query_list:
            items = Scrape(query, product_type, country, condition, listing_type, cache=cache)

            products = [
                Product(
                    id=d["id"], title=d["title"], price=d["price"],
                    shipping=d.get("shipping") or 0,
                    time_left=d["time-left"], time_end=d["time-end"],
                    sold_date=d["sold-date"], bid_count=d["bid-count"],
                    reviews_count=d["reviews-count"], url=d["url"],
                    brand=d["brand"], model=d["model"], vram=d["vram"],
                    socket=d["socket"], cores=d["cores"],
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
                for d in items
            ]

            for p in products:
                try:
                    rc = _upload(cur, p, product_type)
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
                        "ItemLocation VARCHAR(80) NULL",
                        "ItemCondition VARCHAR(40) NULL",
                        "Epid VARCHAR(20) NULL",
                        "CategoryPath VARCHAR(200) NULL",
                        "EnrichNote VARCHAR(60) NULL"):
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
            ItemCondition = %s, EnrichNote = %s
        WHERE EbayID = %s
    """, (enrich['location'], enrich['epid'], enrich['category_path'],
          enrich['condition'], suppress, ebay_id))
    return suppress


def SurfaceDeals(window_hours: int = 2, min_discount: float = 20,
                 nearmiss_discount: float | None = None) -> list[dict]:
    """Detect current deals server-side and record first sightings.

    Runs the shared deal query for every category, INSERT IGNOREs each hit
    into DealOutcomes, and returns ONLY the real deals recorded for the
    first time (rowcount==1) so the caller can notify exactly once per deal.

    Near-miss control cohort: when nearmiss_discount is set below
    min_discount, the query runs at the lower threshold and rows in the
    [nearmiss, min) band are recorded flagged NearMiss=1 — never notified,
    excluded from the outcomes scoreboard and from premium training. Their
    resolved outcomes are the control group that shows whether min_discount
    is set right.

    This replaces the old browser-driven surfacing in /api/deals — deals are
    now captured even when nobody has the dashboard open.
    """
    new_deals = []
    near_misses = 0
    query_discount = (min_discount if nearmiss_discount is None
                      else min(nearmiss_discount, min_discount))
    premiums = GetSnipePremiums()
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
                # PredictedFinal is stored at surfacing so resolved outcomes
                # can grade the premium model itself (predicted vs actual).
                predicted = (int(round(row['PredictedFinalPrice'] * 100))
                             if row.get('PremiumSamples') else None)
                ins.execute("""
                    INSERT IGNORE INTO Scraper.DealOutcomes
                        (EbayID, Category, Model, SurfacedPrice, AvgMarketPrice, DiscountPct, BidCount, EndTime, PredictedFinal, NearMiss)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
            log.info("SurfaceDeals: %d new deal(s), %d near-miss(es) recorded",
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


def GetNotifyRecipients() -> list[dict]:
    """Return enabled notification recipients. [] on any error."""
    try:
        conn = _get_connection()
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute("""
                SELECT ID, Name, HaUrl, HaToken, NotifyService, Categories
                FROM Scraper.NotifyRecipients
                WHERE Enabled = 1
            """)
            return cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        log.error("GetNotifyRecipients failed: %s", e)
        return []


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