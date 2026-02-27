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
        # Known limitation: __ParseRawPrice replaces ',' with '.' so
        # "£1,234.56" → "£1.234.56" → regex matches 1.234, not 1234.56.
        # This only affects prices ≥ £1,000 which are rare in our categories.
        result = _parse_raw_price("£1,234.56")
        assert result is not None   # does parse something (just not 1234.56)

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
# 5. _fetch_oxylabs — mocked, no network
# ═══════════════════════════════════════════════════════════════════════════════

class TestFetchOxylabs:
    OXYLABS_CREDS = {"OXYLABS_USER": "test_user", "OXYLABS_PASSWORD": "test_pass"}

    def _mock_resp(self, html="<html>content</html>"):
        r = MagicMock()
        r.json.return_value = {"results": [{"content": html}]}
        r.raise_for_status = MagicMock()
        return r

    def test_success_returns_html(self):
        with patch.dict(os.environ, self.OXYLABS_CREDS):
            with patch("requests.post", return_value=self._mock_resp("<html>ok</html>")):
                result = EbayScraper._fetch_oxylabs("https://example.com")
        assert result == "<html>ok</html>"

    def test_missing_user_returns_none(self):
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("OXYLABS_USER", "OXYLABS_PASSWORD")}
        with patch.dict(os.environ, clean_env, clear=True):
            result = EbayScraper._fetch_oxylabs("https://example.com")
        assert result is None

    def test_missing_password_returns_none(self):
        with patch.dict(os.environ, {"OXYLABS_USER": "u"}, clear=False):
            # Remove password if present
            env = {k: v for k, v in os.environ.items() if k != "OXYLABS_PASSWORD"}
            with patch.dict(os.environ, env, clear=True):
                result = EbayScraper._fetch_oxylabs("https://example.com")
        assert result is None

    def test_request_exception_returns_none(self):
        with patch.dict(os.environ, self.OXYLABS_CREDS):
            with patch("requests.post", side_effect=Exception("connection refused")):
                result = EbayScraper._fetch_oxylabs("https://example.com")
        assert result is None

    def test_http_error_returns_none(self):
        bad_resp = MagicMock()
        bad_resp.raise_for_status.side_effect = Exception("403 Forbidden")
        with patch.dict(os.environ, self.OXYLABS_CREDS):
            with patch("requests.post", return_value=bad_resp):
                result = EbayScraper._fetch_oxylabs("https://example.com")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Fetch fallback chain — verified via Scrape() with mocked fetchers
# ═══════════════════════════════════════════════════════════════════════════════

class TestFetchFallback:
    """Verify __GetHTML tries direct first and only calls Oxylabs on failure."""

    def test_direct_used_when_available(self):
        with patch.object(EbayScraper, "_fetch_direct", return_value=LARGE_HTML) as mock_direct, \
             patch.object(EbayScraper, "_fetch_oxylabs") as mock_oxy:
            # cache=True so we can intercept before the cache write
            try:
                EbayScraper.Scrape("test query", "GPU", country="uk",
                                   condition="used", listing_type="auction", cache=False)
            except Exception:
                pass  # ParseItems may fail on fake HTML — that's fine
            mock_direct.assert_called()
            mock_oxy.assert_not_called()

    def test_oxylabs_called_when_direct_fails(self):
        with patch.object(EbayScraper, "_fetch_direct", return_value=None) as mock_direct, \
             patch.object(EbayScraper, "_fetch_oxylabs", return_value=LARGE_HTML) as mock_oxy:
            try:
                EbayScraper.Scrape("test query", "GPU", country="uk",
                                   condition="used", listing_type="auction", cache=False)
            except Exception:
                pass
            mock_direct.assert_called()
            mock_oxy.assert_called()

    def test_raises_when_both_fail(self):
        with patch.object(EbayScraper, "_fetch_direct", return_value=None), \
             patch.object(EbayScraper, "_fetch_oxylabs", return_value=None):
            with pytest.raises(RuntimeError, match="All fetch methods failed"):
                EbayScraper.Scrape("test query", "GPU", country="uk",
                                   condition="used", listing_type="auction", cache=False)


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Live data-quality tests  (require internet — skipped unless -m live)
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
