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
        
        # Get item data — skip item entirely if critical fields can't be parsed
        try:
            spans = item.find(class_="s-card__title").find_all('span')
            if spans[0].get_text(strip=True) == "New listing":
                title = spans[1].get_text(strip=True)
            else:
                title = spans[0].get_text(strip=True)
        except (AttributeError, IndexError) as e:
            log.warning("[%s] Skipping item - could not parse title: %s", query, e)
            continue

        try:
            price = __ParseRawPrice(item.find('span', {'class': 's-card__price'}).get_text(strip=True))
            if price is None:
                raise ValueError("Price pattern not found in text")
        except (AttributeError, TypeError, ValueError) as e:
            log.warning("[%s] Skipping item '%s...' - could not parse price: %s", query, title[:40], e)
            continue

        try:
            # eBay 2026 markup: postage lives in a flat span of this class with
            # text like "+£3.42 delivery" / "Free delivery" (previously a nested
            # span saying "postage" — the old selector silently returned 0).
            # The same class also carries the bid-count span, hence the regex.
            ship_el = item.find('span', {'class': 'su-styled-text secondary large'},
                                string=re.compile(r'delivery|postage', re.I))
            shipping = __ParseRawPrice(ship_el.get_text(strip=True)) if ship_el else 0
            if shipping is None:
                shipping = 0   # "Free delivery"
        except (AttributeError, TypeError):
            shipping = 0

        try:
            timeLeft = item.find(class_="s-card__time-left").get_text(strip=True)
        except AttributeError:
            timeLeft = ""

        try:
            timeEnd = item.find(class_="s-card__time-end").get_text(strip=True)
            timeEnd = parse_ebay_endtime(timeEnd)
        except (AttributeError, TypeError):
            timeEnd = None

        try:
            soldDate = item.find(class_="su-styled-text positive default").get_text(strip=True)
            soldDate = soldDate.removeprefix('Sold ')
            soldDate = parse_soldDate(soldDate)
        except AttributeError:
            soldDate = None

        try:
            bidcount = item.find(class_="su-styled-text secondary large", string=re.compile("bid")).get_text(strip=True)
            bidCount = int("".join(filter(str.isdigit, bidcount)))
        except (AttributeError, TypeError, ValueError):
            bidCount = 0

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
        except (TypeError, KeyError, ValueError) as e:
            log.warning("[%s] Skipping item '%s...' - could not parse URL/ID: %s", query, title[:40], e)
            continue

        socket = cores = capacity_gb = interface = form_factor = rpm = ram_type = speed = None

        if productType == 'GPU':

            BRANDS = [
                "ASUS", "MSI", "GIGABYTE", "ZOTAC", "PALIT",
                "EVGA", "PNY", "SAPPHIRE", "XFX", "INNO3D",
                "GAINWARD", "AORUS"
            ]

            # Flexible GPU model pattern
            model_pattern = re.compile(
                r'(?P<series>RTX|GTX|TITAN|RX)\s*'      # series
                r'(?P<number>\d{2,4})\s*'               # number
                r'(?P<variant>Ti|SUPER|Ti\s*SUPER|XT|XTX)?',  # optional variant
                re.IGNORECASE
            )

            # VRAM pattern
            vram_pattern = re.compile(r'(\d{1,2})\s*GB', re.IGNORECASE)

            def extract_model(title: str):
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
                # AMD detection
                if "RX" in title_upper or "RADEON" in title_upper or "XT" in title_upper or "XTX" in title_upper:
                    return "AMD"
                return "NVIDIA"

            model = extract_model(title)
            vram  = extract_vram(title)
            brand = extract_brand(title)
        elif productType == 'CPU':

            # Drop complete-system listings (mini PCs etc.) that mention a CPU
            _tl = title.lower()
            _is_system = (
                any(k in _tl for k in ['mini pc', 'mini-pc', ' nuc', 'barebones',
                                        'desktop pc', 'all-in-one', 'laptop', 'notebook',
                                        'gaming pc', 'gaming computer', 'custom pc',
                                        'full pc', 'complete pc', 'pc bundle', 'pc build'])
                or (bool(re.search(r'\d+\s*gb\s*(ddr\d?|ram)', _tl))
                    and bool(re.search(r'\d+\s*(tb|gb)\s*(ssd|nvme|hdd|m\.2)', _tl)))
            )
            if _is_system:
                log.debug("[%s] Skipping system listing: %s", query, title[:60])
                continue

            def extract_cpu_brand(title: str):
                t = title.upper()
                if 'AMD' in t:
                    return 'AMD'
                if 'INTEL' in t:
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

            def extract_cpu_model(title: str):
                # AMD — normalise to "Ryzen 9 7940HS"
                m = amd_model_pattern.search(title)
                if m:
                    return f"Ryzen {m.group(1)} {m.group(2).upper()}"
                # Intel — normalise to "i5-6600K"
                m = intel_model_pattern.search(title)
                if m:
                    return f"i{m.group(1)}-{m.group(2).upper()}"
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

        elif productType == 'RAM':

            _tl = title.lower()
            _is_system_ram = any(k in _tl for k in [
                'mini pc', 'mini-pc', ' nuc', 'barebones',
                'desktop pc', 'all-in-one', 'laptop', 'notebook',
                'gaming pc', 'gaming computer', 'custom pc',
                'full pc', 'complete pc', 'pc bundle', 'pc build',
            ])
            if _is_system_ram:
                log.debug("[%s] Skipping system listing: %s", query, title[:60])
                continue

            # Type — DDR3 / DDR4 / DDR5 (mandatory; skip if absent)
            type_m = re.search(r'\b(DDR[345])\b', title, re.IGNORECASE)
            if not type_m:
                continue
            ram_type = type_m.group(1).upper()

            # Capacity — total kit GB
            title_up = title.upper()
            kit_m = re.search(r'(\d+)\s*[xX×]\s*(\d+)\s*GB', title_up)
            if kit_m:
                capacity_gb = int(kit_m.group(1)) * int(kit_m.group(2))
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
            'ram-type': ram_type,
            'speed': speed,
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
    # RAM fields
    ram_type: Optional[str] = None
    speed: Optional[int] = None

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
    cur.execute("""
        INSERT INTO EBAY (ID, Title, Price, Shipping, Bids, EndTime, SoldDate, URL)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            Title = VALUES(Title),
            Price = VALUES(Price),
            Shipping = VALUES(Shipping),
            Bids = VALUES(Bids),
            EndTime = VALUES(EndTime),
            SoldDate = VALUES(SoldDate),
            URL = VALUES(URL);
        """, (p.id, p.title, p.price * 100, int(round((p.shipping or 0) * 100)),
              p.bid_count, p.time_end, p.sold_date, p.url)
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
            INSERT INTO HDD (ID, Brand, CapacityGB, Interface, FormFactor, RPM)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                Brand = VALUES(Brand),
                CapacityGB = VALUES(CapacityGB),
                Interface = VALUES(Interface),
                FormFactor = VALUES(FormFactor),
                RPM = VALUES(RPM);
            """, (p.id, p.brand, p.capacity_gb, p.interface, p.form_factor, p.rpm)
        )
    elif product_type == 'RAM':
        cur.execute("""
            INSERT INTO RAM (ID, Brand, CapacityGB, Type, Speed)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                Brand      = VALUES(Brand),
                CapacityGB = VALUES(CapacityGB),
                Type       = VALUES(Type),
                Speed      = VALUES(Speed);
            """, (p.id, p.brand, p.capacity_gb, p.ram_type, p.speed)
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

    return sold_items + active_items

def VerifyPendingOutcomes(hours_after: int = 6, give_up_days: int = 7) -> int:
    """Search eBay sold listings for DealOutcomes past their end time that
    still have SoldDate IS NULL in the EBAY table.

    Two-phase logic:
      Phase 1 — mark give-up: any item past `give_up_days` that is still
                unresolved is flagged GaveUp=1 and will never be retried.
      Phase 2 — verify in-window items: items between `hours_after` hours
                and `give_up_days` days old are looked up by ID in eBay
                sold results.  If not found an ERROR is logged.

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
            SELECT o.EbayID, o.Category, e.Title, o.EndTime
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

        for ebay_id, category, title, end_time in pending:
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
                    log.error(
                        "Outcome NOT found in sold or completed results — "
                        "ID=%s category=%s end_time=%s title='%s'; "
                        "check search params or eBay availability",
                        ebay_id, category, end_time, title[:80],
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
                    ram_type=d["ram-type"], speed=d["speed"],
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


def RecordScrapeCompleted():
    """Persist the current UTC timestamp as the last full-scrape completion time."""
    conn = _get_connection()
    try:
        cur = conn.cursor()
        _ensure_scrape_meta_table(cur)
        cur.execute("""
            INSERT INTO Scraper.ScrapeMeta (id, LastScrapeAt) VALUES (1, NOW())
            ON DUPLICATE KEY UPDATE LastScrapeAt = NOW()
        """)
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
            cur.execute("""
                SELECT o.EbayID, o.Category, e.Title, o.EndTime
                FROM   Scraper.DealOutcomes o
                JOIN   Scraper.EBAY e ON e.ID = o.EbayID
                WHERE  o.EndTime > NOW()
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
                        ram_type=item['ram-type'],
                        speed=item['speed'],
                    )
                    _upload(cur, product, category)
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


def SurfaceDeals(window_hours: int = 2, min_discount: float = 20) -> list[dict]:
    """Detect current deals server-side and record first sightings.

    Runs the shared deal query for every category, INSERT IGNOREs each hit
    into DealOutcomes, and returns ONLY the rows recorded for the first time
    (rowcount==1) so the caller can notify exactly once per deal.

    This replaces the old browser-driven surfacing in /api/deals — deals are
    now captured even when nobody has the dashboard open.
    """
    import queries

    new_deals = []
    conn = _get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        ins = conn.cursor()
        for product_type in queries.CATEGORIES:
            try:
                cur.execute(queries.build_deals_query(product_type, window_hours, min_discount))
                rows = cur.fetchall()
            except mariadb.Error as e:
                log.error("SurfaceDeals: %s query failed: %s", product_type, e)
                continue
            for row in rows:
                label = queries.model_label_for_row(product_type, row)
                ins.execute("""
                    INSERT IGNORE INTO Scraper.DealOutcomes
                        (EbayID, Category, Model, SurfacedPrice, AvgMarketPrice, DiscountPct, BidCount, EndTime)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    row['ID'],
                    product_type.upper(),
                    label,
                    int(round(row['CurrentPrice'] * 100)),
                    int(round(row['AvgMarketPrice'] * 100)),
                    float(row['DiscountPct']),
                    int(row.get('Bids') or 0),
                    row['EndTime'],
                ))
                if ins.rowcount == 1:
                    row['_label'] = label
                    row['_category'] = product_type.upper()
                    new_deals.append(row)
        conn.commit()
        if new_deals:
            log.info("SurfaceDeals: %d new deal(s) recorded", len(new_deals))
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
        for sat in ('GPU', 'CPU', 'HDD', 'RAM'):
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