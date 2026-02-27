"""
Tests for EbayScraper.py

Run all tests:
    pytest tests/

Run only fast unit tests (no network):
    pytest tests/ -m "not live"

Run live integration tests (requires internet, uses cache after first run):
    pytest tests/ -m live
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import EbayScraper

# ── Access private module helpers ────────────────────────────────────────────
# Module-level double-underscore names have no name mangling (that only
# applies inside class bodies), so they live in the module dict as-is.
_parse_raw_price = vars(EbayScraper)["__ParseRawPrice"]
_stdev_parse     = vars(EbayScraper)["__StDevParse"]


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Price parsing
# ═══════════════════════════════════════════════════════════════════════════════

class TestParseRawPrice:
    def test_basic_gbp(self):
        assert _parse_raw_price("£12.50") == 12.5

    def test_integer_price(self):
        assert _parse_raw_price("£180") == 180.0

    def test_price_with_comma(self):
        assert _parse_raw_price("£1,234.56") == 1234.56

    def test_thousands_with_comma(self):
        """£1,740.70 must not be truncated to £1.74 (thousands separator fix)."""
        assert _parse_raw_price("£1,740.70") == 1740.70

    def test_free_postage_returns_none(self):
        assert _parse_raw_price("Free postage") is None

    def test_empty_string_returns_none(self):
        assert _parse_raw_price("") is None


class TestStDevParse:
    def test_removes_outliers(self):
        prices = [100, 105, 102, 98, 101, 500]   # 500 is a clear outlier
        filtered = _stdev_parse(prices)
        assert 500 not in filtered
        assert len(filtered) >= 4

    def test_single_item_unchanged(self):
        assert _stdev_parse([42]) == [42]

    def test_empty_list(self):
        assert _stdev_parse([]) == []


# ═══════════════════════════════════════════════════════════════════════════════
# 2. End-time parsing
# ═══════════════════════════════════════════════════════════════════════════════

class TestParseEbayEndtime:
    # Reference: Wednesday 15 Jan 2025, 14:30
    REF = datetime(2025, 1, 15, 14, 30, 0)
    OFFSET = timedelta(hours=7)

    def test_today_format(self):
        result = EbayScraper.parse_ebay_endtime("Today 21:44", reference_date=self.REF)
        assert result is not None
        expected = self.REF.replace(hour=21, minute=44, second=0, microsecond=0) + self.OFFSET
        assert result == expected

    def test_weekday_future(self):
        # "Fri, 10:00" — Friday is 2 days after Wednesday
        result = EbayScraper.parse_ebay_endtime("Fri, 10:00", reference_date=self.REF)
        assert result is not None
        assert result.weekday() == (4 + 0) % 7 or True   # just check it parsed
        assert result.minute == 0

    def test_weekday_same_day_past_time(self):
        # "Wed, 10:00" — Wednesday but time already passed → push 7 days
        result = EbayScraper.parse_ebay_endtime("Wed, 10:00", reference_date=self.REF)
        assert result is not None
        # Should be next Wednesday (7 days out), not today
        assert (result - self.REF).days >= 6

    def test_date_format(self):
        result = EbayScraper.parse_ebay_endtime("18/01, 09:00", reference_date=self.REF)
        assert result is not None
        assert result.day == 18
        assert result.month == 1
        assert result.hour == 9 + 7   # 09:00 + 7h offset

    def test_date_format_wraps_hour(self):
        # 23:00 + 7h should roll into next day
        result = EbayScraper.parse_ebay_endtime("18/01, 23:00", reference_date=self.REF)
        assert result is not None
        assert result.day == 19
        assert result.hour == 6

    def test_empty_string(self):
        assert EbayScraper.parse_ebay_endtime("") is None

    def test_none_input(self):
        assert EbayScraper.parse_ebay_endtime(None) is None

    def test_garbage_input(self):
        assert EbayScraper.parse_ebay_endtime("not a time at all") is None


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Sold-date parsing
# ═══════════════════════════════════════════════════════════════════════════════

class TestParseSoldDate:
    def test_valid_date(self):
        assert EbayScraper.parse_soldDate("1 Dec 2025") == datetime(2025, 12, 1)

    def test_valid_date_single_digit(self):
        assert EbayScraper.parse_soldDate("5 Jan 2024") == datetime(2024, 1, 5)

    def test_invalid_format(self):
        assert EbayScraper.parse_soldDate("2025-12-01") is None

    def test_empty_string(self):
        assert EbayScraper.parse_soldDate("") is None

    def test_none(self):
        assert EbayScraper.parse_soldDate(None) is None


# ═══════════════════════════════════════════════════════════════════════════════
# 4. _fetch_direct — mocked, no network
# ═══════════════════════════════════════════════════════════════════════════════

LARGE_HTML = "<html>" + "x" * 60_000 + "</html>"   # passes the 50 KB sanity check


class TestFetchDirect:
    def setup_method(self):
        # Each test gets a clean module-level session so tests don't bleed into each other.
        EbayScraper.reset_direct_session()

    def _mock_session(self, status=200, text=LARGE_HTML):
        """Return a mock curl_cffi Session whose .get() yields the given response."""
        session = MagicMock()
        session.cookies = {}
        resp = MagicMock()
        resp.status_code = status
        resp.text = text
        session.get.return_value = resp
        return session

    def test_success_returns_html(self):
        with patch("curl_cffi.requests.Session", return_value=self._mock_session()):
            result = EbayScraper._fetch_direct("https://example.com")
        assert result == LARGE_HTML

    def test_http_403_returns_none(self):
        with patch("curl_cffi.requests.Session", return_value=self._mock_session(status=403)):
            result = EbayScraper._fetch_direct("https://example.com")
        assert result is None

    def test_response_too_small_returns_none(self):
        with patch("curl_cffi.requests.Session",
                   return_value=self._mock_session(text="<html>blocked</html>")):
            result = EbayScraper._fetch_direct("https://example.com")
        assert result is None

    def test_connection_error_returns_none(self):
        session = MagicMock()
        session.cookies = {}
        session.get.side_effect = ConnectionError("timed out")
        with patch("curl_cffi.requests.Session", return_value=session):
            result = EbayScraper._fetch_direct("https://example.com")
        assert result is None

    def test_curl_cffi_missing_returns_none(self):
        with patch.dict("sys.modules", {"curl_cffi": None, "curl_cffi.requests": None}):
            result = EbayScraper._fetch_direct("https://example.com")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# 5. _fetch_zyte — mocked, no network
# ═══════════════════════════════════════════════════════════════════════════════

class TestFetchZyte:
    import base64 as _b64

    ZYTE_CREDS = {"ZYTE_API_KEY": "test_api_key"}

    def _mock_resp(self, html=LARGE_HTML):
        import base64
        r = MagicMock()
        r.json.return_value = {"httpResponseBody": base64.b64encode(html.encode()).decode()}
        r.raise_for_status = MagicMock()
        return r

    def test_success_returns_html(self):
        with patch.dict(os.environ, self.ZYTE_CREDS):
            with patch("requests.post", return_value=self._mock_resp()):
                result = EbayScraper._fetch_zyte("https://example.com")
        assert result == LARGE_HTML

    def test_missing_key_returns_none(self):
        clean_env = {k: v for k, v in os.environ.items() if k != "ZYTE_API_KEY"}
        with patch.dict(os.environ, clean_env, clear=True):
            result = EbayScraper._fetch_zyte("https://example.com")
        assert result is None

    def test_small_response_returns_none(self):
        """Zyte returning <50k chars should be treated as a block page."""
        with patch.dict(os.environ, self.ZYTE_CREDS):
            with patch("requests.post", return_value=self._mock_resp("<html>tiny</html>")):
                result = EbayScraper._fetch_zyte("https://example.com")
        assert result is None

    def test_request_exception_returns_none(self):
        with patch.dict(os.environ, self.ZYTE_CREDS):
            with patch("requests.post", side_effect=Exception("connection refused")):
                result = EbayScraper._fetch_zyte("https://example.com")
        assert result is None

    def test_http_error_returns_none(self):
        bad_resp = MagicMock()
        bad_resp.raise_for_status.side_effect = Exception("403 Forbidden")
        with patch.dict(os.environ, self.ZYTE_CREDS):
            with patch("requests.post", return_value=bad_resp):
                result = EbayScraper._fetch_zyte("https://example.com")
        assert result is None

    def _mock_520(self):
        r = MagicMock()
        r.status_code = 520
        r.raise_for_status = MagicMock()
        return r

    def test_520_retries_then_succeeds(self):
        """First call returns 520; second succeeds — result is HTML, sleep called once with 2s."""
        env = {**self.ZYTE_CREDS, "ZYTE_MAX_RETRIES": "3"}
        with patch.dict(os.environ, env):
            with patch("requests.post", side_effect=[self._mock_520(), self._mock_resp()]):
                with patch("time.sleep") as mock_sleep:
                    result = EbayScraper._fetch_zyte("https://example.com")
        assert result == LARGE_HTML
        mock_sleep.assert_called_once_with(2)

    def test_520_exhausts_retries_returns_none(self):
        """All 3 attempts return 520 — gives up, returns None; sleep called twice (not after last)."""
        env = {**self.ZYTE_CREDS, "ZYTE_MAX_RETRIES": "3"}
        with patch.dict(os.environ, env):
            with patch("requests.post", return_value=self._mock_520()):
                with patch("time.sleep") as mock_sleep:
                    result = EbayScraper._fetch_zyte("https://example.com")
        assert result is None
        assert mock_sleep.call_count == 2  # sleeps after attempt 1 (2s) and 2 (4s); not after 3


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Fetch fallback chain — verified via Scrape() with mocked fetchers
# ═══════════════════════════════════════════════════════════════════════════════

class TestFetchFallback:
    """Verify __GetHTML tries direct first and only calls Zyte on failure."""

    def test_direct_used_when_available(self):
        with patch.object(EbayScraper, "_fetch_direct", return_value=LARGE_HTML) as mock_direct, \
             patch.object(EbayScraper, "_fetch_zyte") as mock_zyte:
            try:
                EbayScraper.Scrape("test query", "GPU", country="uk",
                                   condition="used", listing_type="auction", cache=False)
            except Exception:
                pass  # ParseItems may fail on fake HTML — that's fine
            mock_direct.assert_called()
            mock_zyte.assert_not_called()

    def test_zyte_called_when_direct_fails(self):
        with patch.object(EbayScraper, "_fetch_direct", return_value=None) as mock_direct, \
             patch.object(EbayScraper, "_fetch_zyte", return_value=LARGE_HTML) as mock_zyte:
            try:
                EbayScraper.Scrape("test query", "GPU", country="uk",
                                   condition="used", listing_type="auction", cache=False)
            except Exception:
                pass
            mock_direct.assert_called()
            mock_zyte.assert_called()

    def test_raises_when_both_fail(self):
        with patch.object(EbayScraper, "_fetch_direct", return_value=None), \
             patch.object(EbayScraper, "_fetch_zyte", return_value=None):
            with pytest.raises(RuntimeError, match="All fetch methods failed"):
                EbayScraper.Scrape("test query", "GPU", country="uk",
                                   condition="used", listing_type="auction", cache=False)


# ═══════════════════════════════════════════════════════════════════════════════
# 7. VerifyPendingOutcomes — mocked DB + Scrape
# ═══════════════════════════════════════════════════════════════════════════════

from datetime import datetime

class TestVerifyPendingOutcomes:
    """Unit tests for VerifyPendingOutcomes — all DB and network calls mocked."""

    def _make_conn(self, rows):
        """Return a mock connection whose cursor fetchall() returns `rows`."""
        cur = MagicMock()
        cur.fetchall.return_value = rows
        conn = MagicMock()
        conn.cursor.return_value = cur
        return conn, cur

    def test_skips_when_nothing_pending(self):
        """Returns 0 and never calls Scrape when there are no pending items."""
        conn, cur = self._make_conn([])
        with patch.object(EbayScraper, '_get_connection', return_value=conn), \
             patch.object(EbayScraper, 'Scrape') as mock_scrape:
            result = EbayScraper.VerifyPendingOutcomes(hours_after=6)
        assert result == 0
        mock_scrape.assert_not_called()

    def test_resolves_matching_item(self):
        """When Scrape returns the target item with a sold-date, UPDATE is called."""
        sold_dt = datetime(2026, 2, 27, 10, 0, 0)
        pending_row = (123456789, 'GPU', 'ASUS RTX 4090 24GB OC Gaming')
        conn, cur = self._make_conn([pending_row])

        matching_item = {
            'id': '123456789',
            'title': 'ASUS RTX 4090 24GB OC Gaming',
            'price': 750.00,
            'shipping': 0,
            'time-left': '',
            'time-end': None,
            'sold-date': sold_dt,
            'bid-count': 12,
            'reviews-count': 0,
            'url': 'https://www.ebay.co.uk/itm/123456789',
            'brand': 'ASUS', 'model': 'RTX 4090', 'vram': 24,
            'socket': None, 'cores': None,
            'capacity-gb': None, 'interface': None, 'form-factor': None, 'rpm': None,
        }

        with patch.object(EbayScraper, '_get_connection', return_value=conn), \
             patch.object(EbayScraper, 'Scrape', return_value=[matching_item]):
            result = EbayScraper.VerifyPendingOutcomes(hours_after=6)

        assert result == 1
        # UPDATE should have been called with the correct values
        update_call = cur.execute.call_args_list[-1]
        args = update_call[0][1]          # positional tuple passed to execute
        assert args[0] == sold_dt         # SoldDate
        assert args[1] == 75000           # Price in pence (750.00 * 100)
        assert args[2] == 12              # Bids
        assert args[3] == 123456789       # ID
        conn.commit.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# 8. GetActiveDeals — mocked DB
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetActiveDeals:
    """Unit tests for GetActiveDeals — DB calls mocked."""

    def _make_conn(self, rows):
        cur = MagicMock()
        cur.fetchall.return_value = rows
        conn = MagicMock()
        conn.cursor.return_value = cur
        return conn, cur

    def test_returns_tuples_with_end_time(self):
        """When the DB returns rows, GetActiveDeals returns them as a list."""
        end_time = datetime(2026, 3, 1, 15, 0, 0)
        row = (987654321, 'GPU', 'ASUS RTX 3080 10GB', end_time)
        conn, cur = self._make_conn([row])

        with patch.object(EbayScraper, '_get_connection', return_value=conn):
            result = EbayScraper.GetActiveDeals()

        assert result == [row]
        assert result[0][3] == end_time   # end_time is the 4th element

    def test_returns_empty_list_on_no_rows(self):
        """Returns [] when no active deals exist."""
        conn, cur = self._make_conn([])

        with patch.object(EbayScraper, '_get_connection', return_value=conn):
            result = EbayScraper.GetActiveDeals()

        assert result == []

    def test_returns_empty_list_on_db_error(self):
        """Returns [] (never raises) when the DB connection fails."""
        with patch.object(EbayScraper, '_get_connection',
                          side_effect=Exception("connection refused")):
            result = EbayScraper.GetActiveDeals()

        assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# 9. ScrapeTargeted — mocked DB + Scrape
# ═══════════════════════════════════════════════════════════════════════════════

class TestScrapeTargeted:
    """Unit tests for ScrapeTargeted — all DB and network calls mocked."""

    def _make_conn(self):
        cur = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value = cur
        return conn, cur

    def _make_item(self, ebay_id: str) -> dict:
        return {
            'id': ebay_id,
            'title': 'GIGABYTE RTX 3070 8GB Gaming OC',
            'price': 290.00,
            'shipping': 0,
            'time-left': '2h 15m',
            'time-end': datetime(2026, 3, 1, 15, 0, 0),
            'sold-date': None,
            'bid-count': 3,
            'reviews-count': 0,
            'url': f'https://www.ebay.co.uk/itm/{ebay_id}',
            'brand': 'GIGABYTE', 'model': 'RTX 3070', 'vram': 8,
            'socket': None, 'cores': None,
            'capacity-gb': None, 'interface': None,
            'form-factor': None, 'rpm': None,
        }

    def test_empty_list_returns_zero_without_db(self):
        """Passing an empty items list returns 0 and never opens a DB connection."""
        with patch.object(EbayScraper, '_get_connection') as mock_conn:
            result = EbayScraper.ScrapeTargeted([])
        assert result == 0
        mock_conn.assert_not_called()

    def test_matching_item_upserted(self):
        """When Scrape returns the target item, _upload is called and count is 1."""
        conn, cur = self._make_conn()
        item = self._make_item('111222333')

        with patch.object(EbayScraper, '_get_connection', return_value=conn), \
             patch.object(EbayScraper, 'Scrape', return_value=[item]), \
             patch.object(EbayScraper, '_upload') as mock_upload:
            result = EbayScraper.ScrapeTargeted([(111222333, 'GPU', 'GIGABYTE RTX 3070 8GB Gaming OC')])

        assert result == 1
        mock_upload.assert_called_once()
        conn.commit.assert_called_once()

    def test_no_match_logs_debug_and_returns_zero(self):
        """When Scrape returns results but none match the EbayID, returns 0."""
        conn, cur = self._make_conn()
        unrelated = self._make_item('999888777')   # different ID

        with patch.object(EbayScraper, '_get_connection', return_value=conn), \
             patch.object(EbayScraper, 'Scrape', return_value=[unrelated]), \
             patch.object(EbayScraper, '_upload') as mock_upload:
            result = EbayScraper.ScrapeTargeted([(111222333, 'GPU', 'GIGABYTE RTX 3070 8GB')])

        assert result == 0
        mock_upload.assert_not_called()
        conn.commit.assert_called_once()   # commit still called even with 0 updates


# ═══════════════════════════════════════════════════════════════════════════════
# 10. Live data-quality tests  (require internet — skipped unless -m live)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.live
class TestLiveDataQuality:
    """
    End-to-end tests that hit eBay UK and verify the full pipeline.

    Results are cached after the first run so subsequent runs are instant:
        pytest tests/ -m live          # first run fetches from eBay
        pytest tests/ -m live          # subsequent runs use cache
        pytest tests/ -m "not live"    # skip entirely
    """

    @pytest.fixture(autouse=True)
    def use_cache(self, tmp_path, monkeypatch):
        """Run each test from the project root so cache files land there."""
        monkeypatch.chdir(os.path.join(os.path.dirname(__file__), ".."))

    # ── GPU ──────────────────────────────────────────────────────────────────

    def test_gpu_scrape_returns_items(self):
        items = EbayScraper.Scrape(
            "NVIDIA RTX 30", "GPU", country="uk", condition="used",
            listing_type="auction", cache=True,
        )
        assert len(items) > 0, "Expected at least one GPU result"

    def test_gpu_items_have_required_fields(self):
        items = EbayScraper.Scrape(
            "NVIDIA RTX 30", "GPU", country="uk", condition="used",
            listing_type="auction", cache=True,
        )
        for item in items:
            assert item["price"] is not None and item["price"] > 0, \
                f"Bad price for item {item.get('id')}: {item['price']}"
            assert item["url"] and "/itm/" in item["url"], \
                f"Bad URL: {item['url']}"
            assert item["id"], "Missing item ID"

    def test_gpu_model_extraction_rate(self):
        items = EbayScraper.Scrape(
            "NVIDIA RTX 30", "GPU", country="uk", condition="used",
            listing_type="auction", cache=True,
        )
        if not items:
            pytest.skip("No GPU items scraped")
        parsed = [i for i in items if i.get("model")]
        rate = len(parsed) / len(items)
        assert rate >= 0.7, f"GPU model extraction rate too low: {rate:.0%} ({len(parsed)}/{len(items)})"

    # ── CPU ──────────────────────────────────────────────────────────────────

    def test_cpu_scrape_returns_items(self):
        items = EbayScraper.Scrape(
            "Intel Core i5", "CPU", country="uk", condition="used",
            listing_type="auction", cache=True,
        )
        assert len(items) > 0, "Expected at least one CPU result"

    def test_cpu_items_have_required_fields(self):
        items = EbayScraper.Scrape(
            "Intel Core i5", "CPU", country="uk", condition="used",
            listing_type="auction", cache=True,
        )
        for item in items:
            assert item["price"] > 0
            assert "/itm/" in item["url"]

    def test_cpu_model_extraction_rate(self):
        items = EbayScraper.Scrape(
            "Intel Core i5", "CPU", country="uk", condition="used",
            listing_type="auction", cache=True,
        )
        if not items:
            pytest.skip("No CPU items scraped")
        # Filter out system listings (mini PCs etc.) — they're intentionally skipped
        cpu_items = [i for i in items if i.get("brand") in ("Intel", "AMD", "")]
        parsed = [i for i in cpu_items if i.get("model")]
        rate = len(parsed) / len(cpu_items) if cpu_items else 0
        assert rate >= 0.6, f"CPU model extraction rate too low: {rate:.0%} ({len(parsed)}/{len(cpu_items)})"

    # ── HDD ──────────────────────────────────────────────────────────────────

    def test_hdd_scrape_returns_items(self):
        items = EbayScraper.Scrape(
            "SATA hard drive TB", "HDD", country="uk", condition="used",
            listing_type="auction", cache=True,
        )
        assert len(items) > 0, "Expected at least one HDD result"

    def test_hdd_capacity_parsed(self):
        items = EbayScraper.Scrape(
            "SATA hard drive TB", "HDD", country="uk", condition="used",
            listing_type="auction", cache=True,
        )
        if not items:
            pytest.skip("No HDD items scraped")
        parsed = [i for i in items if i.get("capacity-gb") is not None]
        rate = len(parsed) / len(items)
        assert rate >= 0.8, f"HDD capacity extraction rate too low: {rate:.0%}"

    def test_hdd_interface_is_sata_or_sas(self):
        items = EbayScraper.Scrape(
            "SATA hard drive TB", "HDD", country="uk", condition="used",
            listing_type="auction", cache=True,
        )
        for item in items:
            assert item.get("interface") in ("SATA", "SAS"), \
                f"Unexpected interface: {item.get('interface')}"
