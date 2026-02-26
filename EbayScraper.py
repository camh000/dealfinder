import re
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

def __GetHTML(query, country, condition='', listing_type='all', alreadySold=True, cache=False):
    
    cache_file = f"{query}_{'sold' if alreadySold else 'active'}.txt"

    if cache and os.path.isfile(cache_file):
        with open(cache_file, "r", encoding="utf-8") as f:
            responseHTML = f.read()
    else:
        alreadySoldString = '&LH_Complete=1&LH_Sold=1' if alreadySold else '&_sop=1'
        
        parsedQuery = urllib.parse.quote(query).replace('%20', '+')
        url = f'https://www.ebay{countryDict[country]}/sch/i.html?_from=R40&_nkw=' + parsedQuery + alreadySoldString + conditionDict[condition] + typeDict[listing_type]
        print(url)

        payload = {
            'source': 'universal',
            'user_agent_type': 'desktop',
            'url': url,
            'render': 'html',
            'geo_location': 'United Kingdom'
        }
        response = requests.request(
            'POST',
            'https://realtime.oxylabs.io/v1/queries',
            auth=(os.environ["OXYLABS_USER"], os.environ["OXYLABS_PASSWORD"]),
            json=payload,
        )
        responseHTML = response.json()['results'][0]['content']

        if cache:
            with open(cache_file, "w", encoding='utf-8') as f:
                f.write(responseHTML)

    return BeautifulSoup(responseHTML, 'html.parser')

def __ParseItems(soup, query, productType):
    rawItems = soup.find_all('div', {'class': 'su-card-container su-card-container--horizontal'})
    data = []
    for item in rawItems[1:]:
        
        # Get item data
        title = item.find(class_="s-card__title").find_all('span')
        if title[0].get_text(strip=True) == "New listing":
            title = title[1].get_text(strip=True)
        else:
            title = title[0].get_text(strip=True)

        price = __ParseRawPrice(item.find('span', {'class': 's-card__price'}).get_text(strip=True))

        try:
            shipping = __ParseRawPrice(item.find('span', {'class': 'su-styled-text secondary large'}).find('span').get_text(strip=True))
        except: shipping = 0
        
        try: timeLeft = item.find(class_="s-card__time-left").get_text(strip=True)
        except: timeLeft = ""
        
        try: 
            timeEnd = item.find(class_="s-card__time-end").get_text(strip=True)
            timeEnd = parse_ebay_endtime(timeEnd)
        except: timeEnd = None
        
        try: 
            soldDate = item.find(class_="su-styled-text positive default").get_text(strip=True)
            soldDate = soldDate.lstrip('Sold ')
            soldDate = parse_soldDate(soldDate)
        except: soldDate = None

        try: 
            bidcount = item.find(class_="su-styled-text secondary large", string=re.compile("bid")).get_text(strip=True)
            bidCount = int("".join(filter(str.isdigit, bidcount)))
        except: bidCount = 0
        
        try: reviewCount = int("".join(filter(str.isdigit, item.find(class_="s-item__reviews-count").find('span').get_text(strip=True))))
        except: reviewCount = 0
        
        url = item.find('a')['href']

        id = url.lstrip('https://www.ebay.co.uk/itm/').split('?')[0]

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
        else:
            brand = '',
            model = '',
            vram = ''
        print(f"{brand} {model} {vram}")

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
            'vram': vram


        }
        
        data.append(itemData)
    
    # Remove item with prices too high or too low
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
    parsedPrice = re.search('(\d+(.\d+)?)', string.replace(',', '.'))
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
    brand: str
    model: str
    vram: Optional[int]

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
        VALUES (?, ?, ?, ?, ?, ?, ?)
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
            VALUES (?, ?, ?, ?)
            ON DUPLICATE KEY UPDATE
                Brand = VALUES(Brand),
                Model = VALUES(Model),
                VRAM = VALUES(VRAM);
            """, (p.id, p.brand, p.model, p.vram)
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
                    brand=d["brand"], model=d["model"], vram=d["vram"]
                )
                for d in items
            ]

            for p in products:
                try:
                    _upload(cur, p, product_type)
                except mariadb.Error as e:
                    print(f"Error uploading {p.id}: {e}")

        conn.commit()
        print(f"Last inserted ID: {cur.lastrowid}")

    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()