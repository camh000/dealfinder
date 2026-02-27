import re
import logging
import requests
import urllib.parse
from bs4 import BeautifulSoup
import os.path
from datetime import datetime, timedelta
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


def reset_direct_session() -> None:
    """Discard the current curl-cffi session.

    Call at the start of each scrape run so a fresh Akamai identity
    (new cookies, new TLS session) is established via the homepage warmup.
    """
    global _direct_session
    _direct_session = None


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
            _direct_session = None  # session may be flagged; reset for next call
            return None
        log.info("Direct fetch OK (curl-cffi/chrome131, %d chars)", len(html))
        return html
    except Exception as e:
        log.warning("Direct fetch failed: %s", e)
        _direct_session = None
        return None


def _fetch_zyte(url: str) -> str | None:
    """Fetch URL via Zyte API — pay-per-use fallback when direct fetch is blocked.

    Uses httpResponseBody mode (raw HTTP response, no JS rendering).
    eBay search pages are server-rendered HTML so JS execution is not required.
    Approx cost: $1.8 per 1,000 successful requests (no monthly fee).

    If Akamai still blocks via Zyte (response too small), switch the payload to:
        {"url": url, "browserHtml": True, "geolocation": "GB"}
    and decode with resp.json()["browserHtml"] (no base64). Cost ~$9/1k.
    """
    import base64
    api_key = os.environ.get("ZYTE_API_KEY")
    if not api_key:
        log.warning("Zyte API key not configured — skipping Zyte fetch")
        return None
    try:
        log.info("Fetching via Zyte API: %s", url)
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


def __GetHTML(query, country, condition='', listing_type='all', alreadySold=True, cache=False):

    cache_file = f"{query}_{'sold' if alreadySold else 'active'}.txt"

    if cache and os.path.isfile(cache_file):
        with open(cache_file, "r", encoding="utf-8") as f:
            responseHTML = f.read()
    else:
        alreadySoldString = '&LH_Complete=1&LH_Sold=1' if alreadySold else '&_sop=1'
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
    for item in rawItems[1:]:
        
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
            shipping = __ParseRawPrice(item.find('span', {'class': 'su-styled-text secondary large'}).find('span').get_text(strip=True))
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
            soldDate = soldDate.lstrip('Sold ')
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

        socket = cores = capacity_gb = interface = form_factor = rpm = None

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
                                        'desktop pc', 'all-in-one', 'laptop', 'notebook'])
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
    parsedPrice = re.search(r'(\d+(\.\d+)?)', string.replace(',', '.'))
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
    
    avg = __Average(numberList)
    stdev = __StDev(numberList)
    
    # Remove prices too high or too low; Accept Between -1 StDev to +1 StDev
    numberList = [nmbr for nmbr in numberList if (avg + stdev >= nmbr >= avg - stdev)]

    return numberList

def parse_ebay_endtime(endtime_str: str, reference_date: datetime = None):

    if not endtime_str:
        return None

    if not reference_date:
        reference_date = datetime.now()

    # Clean input
    endtime_str = endtime_str.strip().strip("() ")

    weekdays = {"Mon":0, "Tue":1, "Wed":2, "Thu":3, "Fri":4, "Sat":5, "Sun":6}

    offset = timedelta(hours=7)

    # Case 1: Today 21:44
    if endtime_str.lower().startswith("today"):
        time_part = endtime_str.split()[1]
        hour, minute = map(int, time_part.split(":"))
        return reference_date.replace(
            hour=hour, minute=minute, second=0, microsecond=0
        ) + offset

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

        dt = reference_date + timedelta(days=days_ahead)
        return dt.replace(hour=hour, minute=minute, second=0, microsecond=0) + offset

    # Case 3: 05/03, 07:05
    match = re.match(r"(\d{2})/(\d{2}),\s*(\d{1,2}):(\d{2})", endtime_str)
    if match:
        day, month, hour, minute = map(int, match.groups())
        year = reference_date.year

        dt = datetime(year, month, day, hour, minute)

        if dt < reference_date:
            dt = dt.replace(year=year + 1)

        return dt + offset

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
    # CPU fields
    socket: Optional[str] = None
    cores: Optional[int] = None
    # HDD fields
    capacity_gb: Optional[int] = None
    interface: Optional[str] = None
    form_factor: Optional[str] = None
    rpm: Optional[int] = None

def _get_connection():
    return mariadb.connect(
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", 3305)),
        database=os.environ["DB_NAME"]
    )

def _upload(cur, p: Product, product_type: str):
    cur.execute("""
        INSERT INTO EBAY (ID, Title, Price, Bids, EndTime, SoldDate, URL)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            Title = VALUES(Title),
            Price = VALUES(Price),
            Bids = VALUES(Bids),
            EndTime = VALUES(EndTime),
            SoldDate = VALUES(SoldDate),
            URL = VALUES(URL);
        """, (p.id, p.title, p.price * 100, p.bid_count, p.time_end, p.sold_date, p.url)
    )
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

def VerifyPendingOutcomes(hours_after: int = 6) -> int:
    """Search eBay sold listings for DealOutcomes past their end time that
    still have SoldDate IS NULL in the EBAY table.

    Runs against ALL past-threshold pending items on every call, so any
    backlog (including items that pre-date this feature) is self-healed.
    Returns the number of outcomes successfully resolved this run.
    """
    conn = _get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT o.EbayID, o.Category, e.Title
            FROM   Scraper.DealOutcomes o
            JOIN   Scraper.EBAY e ON e.ID = o.EbayID
            WHERE  o.EndTime < NOW() - INTERVAL %s HOUR
              AND  e.SoldDate IS NULL
        """, (hours_after,))
        pending = cur.fetchall()

        if not pending:
            log.info("Outcome verification: no unresolved outcomes past %dh threshold", hours_after)
            return 0

        log.info("Outcome verification: checking %d item(s) past %dh threshold", len(pending), hours_after)
        resolved = 0

        for ebay_id, category, title in pending:
            try:
                # Use first 80 chars of title — specific enough to surface the item,
                # short enough to match eBay's search index reliably.
                items = Scrape(
                    title[:80],
                    category,
                    country='uk',
                    condition='used',
                    listing_type='auction',
                    cache=False,
                )
                for item in items:
                    if str(item['id']) == str(ebay_id) and item.get('sold-date'):
                        cur.execute("""
                            UPDATE Scraper.EBAY
                            SET    SoldDate = %s,
                                   Price    = %s,
                                   Bids     = %s
                            WHERE  ID       = %s
                              AND  SoldDate IS NULL
                        """, (item['sold-date'], int(item['price'] * 100), item['bid-count'], ebay_id))
                        log.info(
                            "Outcome verified: ID=%s sold for £%.2f on %s",
                            ebay_id, item['price'], item['sold-date'],
                        )
                        resolved += 1
                        break
                else:
                    log.debug("Outcome not yet in sold results: ID=%s '%s'", ebay_id, title[:60])
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
        for query in query_list:
            items = Scrape(query, product_type, country, condition, listing_type, cache=cache)

            products = [
                Product(
                    id=d["id"], title=d["title"], price=d["price"],
                    time_left=d["time-left"], time_end=d["time-end"],
                    sold_date=d["sold-date"], bid_count=d["bid-count"],
                    reviews_count=d["reviews-count"], url=d["url"],
                    brand=d["brand"], model=d["model"], vram=d["vram"],
                    socket=d["socket"], cores=d["cores"],
                    capacity_gb=d["capacity-gb"], interface=d["interface"],
                    form_factor=d["form-factor"], rpm=d["rpm"],
                )
                for d in items
            ]

            for p in products:
                try:
                    _upload(cur, p, product_type)
                except mariadb.Error as e:
                    log.error("DB error uploading item %s: %s", p.id, e)

        conn.commit()
        log.info("Upload complete. Last inserted ID: %s", cur.lastrowid)

    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()