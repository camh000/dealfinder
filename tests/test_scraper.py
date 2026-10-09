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
    """Times are interpreted as US-Pacific wall clock (eBay's display TZ for
    anonymous scrapes) and returned as naive UTC. In January that's PST
    (UTC-8); in July PDT (UTC-7). The tz database supplies the difference —
    the old fixed +7 offset was an hour wrong every winter.
    """
    # Reference "now" in display-TZ wall clock: Wednesday 15 Jan 2025, 14:30 PST
    REF = datetime(2025, 1, 15, 14, 30, 0)
    PST = timedelta(hours=8)   # winter: PST -> UTC

    def test_today_format(self):
        result = EbayScraper.parse_ebay_endtime("Today 21:44", reference_date=self.REF)
        assert result is not None
        expected = self.REF.replace(hour=21, minute=44, second=0, microsecond=0) + self.PST
        assert result == expected

    def test_today_format_summer_uses_pdt(self):
        # Tuesday 15 Jul 2025 — Pacific daylight time, so UTC is only +7.
        ref_summer = datetime(2025, 7, 15, 14, 30, 0)
        result = EbayScraper.parse_ebay_endtime("Today 21:44", reference_date=ref_summer)
        assert result == ref_summer.replace(hour=21, minute=44) + timedelta(hours=7)

    def test_weekday_future(self):
        # "Fri, 10:00" — Friday (weekday 4) is 2 days after Wednesday.
        # 10:00 PST -> 18:00 UTC, still Friday.
        result = EbayScraper.parse_ebay_endtime("Fri, 10:00", reference_date=self.REF)
        assert result is not None
        assert result.weekday() == 4
        assert result.hour == 18
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
        assert result.hour == 9 + 8   # 09:00 PST -> 17:00 UTC

    def test_date_format_wraps_hour(self):
        # 23:00 PST + 8h rolls into the next day (07:00 UTC)
        result = EbayScraper.parse_ebay_endtime("18/01, 23:00", reference_date=self.REF)
        assert result is not None
        assert result.day == 19
        assert result.hour == 7

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
        self._saved = {k: getattr(EbayScraper, k) for k in (
            '_MIN_DELAY_S', '_JITTER_S', '_BLOCK_BACKOFF_S', '_profile_idx')}
        EbayScraper._MIN_DELAY_S = EbayScraper._JITTER_S = EbayScraper._BLOCK_BACKOFF_S = 0
        EbayScraper._consecutive_blocks = 0
        EbayScraper._breaker_open_until = 0.0

    def teardown_method(self):
        for k, v in self._saved.items():
            setattr(EbayScraper, k, v)
        EbayScraper._consecutive_blocks = 0
        EbayScraper._breaker_open_until = 0.0
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

    def test_block_retries_once_on_the_next_fingerprint(self):
        blocked, ok = self._mock_session(status=403), self._mock_session()
        with patch("curl_cffi.requests.Session", side_effect=[blocked, ok]) as mk:
            result = EbayScraper._fetch_direct("https://example.com")
        assert result == LARGE_HTML
        profiles = [c.kwargs['impersonate'] for c in mk.call_args_list]
        assert profiles[0] != profiles[1]

    def test_signin_wall_returns_at_once_without_rotating(self):
        sess = self._mock_session()
        sess.get.return_value.url = 'https://signin.ebay.co.uk/ws/eBayISAPI.dll?SignIn'
        with patch("curl_cffi.requests.Session", return_value=sess) as mk:
            assert EbayScraper._fetch_direct("https://example.com") is None
        assert EbayScraper.last_block_reason == EbayScraper.SIGNIN_WALL
        assert mk.call_count == 1

    def test_consecutive_blocks_open_the_circuit(self):
        with patch("curl_cffi.requests.Session", return_value=self._mock_session(status=403)):
            for _ in range(EbayScraper._BREAKER_THRESHOLD):
                EbayScraper._fetch_direct("https://example.com")
        with patch("curl_cffi.requests.Session") as mk:
            assert EbayScraper._fetch_direct("https://example.com") is None
        mk.assert_not_called()
        assert EbayScraper.last_block_reason == EbayScraper.CIRCUIT_OPEN

    def test_404_is_an_answer_not_a_block(self):
        sess = self._mock_session(status=404)
        with patch("curl_cffi.requests.Session", return_value=sess) as mk:
            for _ in range(EbayScraper._BREAKER_THRESHOLD + 1):
                assert EbayScraper._fetch_direct("https://example.com") is None
        assert EbayScraper.last_block_reason == 'HTTP 404'
        assert mk.call_count == 1                       # no rotation, same session
        assert EbayScraper.breaker_remaining() == 0.0   # and the breaker stays shut

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

    def test_suspended_account_disables_zyte(self):
        suspended = MagicMock(status_code=403, text='{"type":"/auth/account-suspended"}')
        try:
            with patch.dict(os.environ, self.ZYTE_CREDS):
                with patch("requests.post", return_value=suspended) as mock_post:
                    assert EbayScraper._fetch_zyte("https://example.com") is None
                    assert EbayScraper._fetch_zyte("https://example.com") is None
            assert mock_post.call_count == 1
        finally:
            EbayScraper._zyte_disabled = False

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
# 7. _scrape_item_by_id — mocked __GetHTML / __ParseItems
# ═══════════════════════════════════════════════════════════════════════════════

class TestScrapeItemById:
    """Unit tests for _scrape_item_by_id — __GetHTML and __ParseItems mocked via patch.dict."""

    def _make_item(self, ebay_id: str, sold_date=None) -> dict:
        return {
            'id': ebay_id,
            'title': 'Test GPU',
            'price': 500.0,
            'shipping': 0,
            'time-left': '',
            'time-end': None,
            'sold-date': sold_date,
            'bid-count': 5,
            'reviews-count': 0,
            'url': f'https://www.ebay.co.uk/itm/{ebay_id}',
            'brand': 'Test', 'model': 'RTX 9000', 'vram': 16,
            'socket': None, 'cores': None,
            'capacity-gb': None, 'interface': None, 'form-factor': None, 'rpm': None,
            'ram-type': None, 'speed': None,
        }

    def _patch_internals(self, parse_return):
        """Return a patch.dict context that replaces __GetHTML and __ParseItems."""
        fake_soup = MagicMock()
        return patch.dict(vars(EbayScraper), {
            '__GetHTML': MagicMock(return_value=fake_soup),
            '__ParseItems': MagicMock(return_value=parse_return),
        })

    def test_sold_returns_matching_item(self):
        """sold=True: returns item dict when the ID matches a sold result."""
        item = self._make_item('123456789', sold_date=datetime(2026, 2, 27))
        with self._patch_internals([item]):
            result = EbayScraper._scrape_item_by_id(123456789, 'GPU', sold=True)
        assert result is not None
        assert result['id'] == '123456789'

    def test_active_returns_matching_item(self):
        """sold=False: returns item dict when the ID matches an active result."""
        item = self._make_item('987654321')
        with self._patch_internals([item]):
            result = EbayScraper._scrape_item_by_id(987654321, 'GPU', sold=False)
        assert result is not None
        assert result['id'] == '987654321'

    def test_no_match_returns_none(self):
        """Returns None when ParseItems returns results but none match the target ID."""
        unrelated = self._make_item('999000111')
        with self._patch_internals([unrelated]):
            result = EbayScraper._scrape_item_by_id(123456789, 'GPU', sold=True)
        assert result is None

    def test_sold_flag_passed_to_get_html(self):
        """Verifies alreadySold kwarg is forwarded correctly to __GetHTML."""
        fake_soup = MagicMock()
        mock_get_html = MagicMock(return_value=fake_soup)
        with patch.dict(vars(EbayScraper), {
            '__GetHTML': mock_get_html,
            '__ParseItems': MagicMock(return_value=[]),
        }):
            EbayScraper._scrape_item_by_id(111, 'GPU', sold=True)
            _, kwargs = mock_get_html.call_args
            assert kwargs.get('alreadySold') is True

            EbayScraper._scrape_item_by_id(111, 'GPU', sold=False)
            _, kwargs = mock_get_html.call_args
            assert kwargs.get('alreadySold') is False


# ═══════════════════════════════════════════════════════════════════════════════
# 8. VerifyPendingOutcomes — mocked DB + _scrape_item_by_id
# ═══════════════════════════════════════════════════════════════════════════════

from datetime import datetime

class TestScrapeTargetedSnapshots:
    """Each successful targeted refresh must append a DealSnapshots row —
    the trajectory dataset for time-aware premiums."""

    def test_snapshot_recorded_on_refresh(self):
        from datetime import datetime, timedelta, timezone
        end = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=30)
        item = {
            'id': '123456789', 'title': 'MSI RTX 3070 8GB', 'price': 150.0,
            'shipping': 4.5, 'time-left': '30m', 'time-end': end,
            'sold-date': None, 'bid-count': 3, 'reviews-count': 0,
            'url': 'https://www.ebay.co.uk/itm/123456789',
            'brand': 'Msi', 'model': 'RTX 3070', 'vram': 8,
            'socket': None, 'cores': None, 'capacity-gb': None,
            'interface': None, 'form-factor': None, 'rpm': None,
            'drive-type': None, 'ram-type': None, 'speed': None,
            'ram-format': None, 'quantity': 1,
            'feedback-pct': 100.0, 'feedback-count': 50,
        }
        cur = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value = cur
        with patch.object(EbayScraper, '_get_connection', return_value=conn), \
             patch.object(EbayScraper, '_scrape_item_by_id', return_value=item):
            updated = EbayScraper.ScrapeTargeted([(123456789, 'GPU', 'MSI RTX 3070 8GB')])

        assert updated == 1
        snap_calls = [c for c in cur.execute.call_args_list
                      if 'DealSnapshots' in str(c[0][0])]
        assert len(snap_calls) == 1
        ebay_id, eff_pence, bids, minutes_left = snap_calls[0][0][1]
        assert ebay_id == 123456789
        assert eff_pence == 15450          # (150.00 + 4.50) * 100
        assert bids == 3
        assert 28 <= minutes_left <= 30


class TestVerifyPendingOutcomes:
    """VerifyPendingOutcomes resolves tracked deals from item pages (eBay put
    sold SEARCHES behind sign-in in Jul 2026) — DB + fetch mocked."""

    def _make_conn(self, rows):
        cur = MagicMock()
        cur.fetchall.return_value = rows
        cur.rowcount = 0
        conn = MagicMock()
        conn.cursor.return_value = cur
        return conn, cur

    def test_skips_when_nothing_pending(self):
        conn, cur = self._make_conn([])
        with patch.object(EbayScraper, '_get_connection', return_value=conn), \
             patch.object(EbayScraper, '_fetch_direct') as mock_fetch:
            assert EbayScraper.VerifyPendingOutcomes(hours_after=6, give_up_days=7) == 0
        mock_fetch.assert_not_called()

    def test_sold_item_page_resolves_outcome(self):
        conn, cur = self._make_conn([(128083783938,)])
        with patch.object(EbayScraper, '_get_connection', return_value=conn), \
             patch.object(EbayScraper, '_fetch_direct', return_value=ITEM_SOLD_HTML), \
             patch.object(EbayScraper, 'last_fetch_url', 'https://www.ebay.co.uk/itm/128083783938'):
            assert EbayScraper.VerifyPendingOutcomes(hours_after=2, give_up_days=7) == 1
        ebay = [c for c in cur.execute.call_args_list
                if 'UPDATE Scraper.EBAY' in c[0][0] and 'Price = %s' in c[0][0]]
        end, pence, bids, ebay_id, repair = ebay[0][0][1]
        assert (pence, bids, ebay_id, repair) == (4049, 9, 128083783938, False)
        assert end == datetime(2026, 9, 24, 6, 15, 6)
        assert any('EndedUnsold = 0' in c[0][0] for c in cur.execute.call_args_list)

    def test_unsold_item_page_marks_ended_unsold(self):
        conn, cur = self._make_conn([(227518392483,)])
        with patch.object(EbayScraper, '_get_connection', return_value=conn), \
             patch.object(EbayScraper, '_fetch_direct', return_value=ITEM_UNSOLD_HTML):
            assert EbayScraper.VerifyPendingOutcomes(hours_after=2, give_up_days=7) == 1
        sqls = [c[0][0] for c in cur.execute.call_args_list]
        assert any('Price = NULL' in s for s in sqls)
        assert any('EndedUnsold = 1' in s for s in sqls)

    def test_missing_page_counts_an_attempt(self):
        conn, cur = self._make_conn([(555666777,)])
        with patch.object(EbayScraper, '_get_connection', return_value=conn), \
             patch.object(EbayScraper, '_fetch_direct', return_value=None), \
             patch.object(EbayScraper, 'last_block_reason', 'HTTP 404'):
            assert EbayScraper.VerifyPendingOutcomes(hours_after=2, give_up_days=7) == 0
        assert any('ResolveAttempts = ResolveAttempts + 1' in c[0][0]
                   for c in cur.execute.call_args_list)

    def test_blocked_fetch_does_not_count_an_attempt(self):
        conn, cur = self._make_conn([(555666777,)])
        with patch.object(EbayScraper, '_get_connection', return_value=conn), \
             patch.object(EbayScraper, '_fetch_direct', return_value=None), \
             patch.object(EbayScraper, 'last_block_reason', 'HTTP 403'):
            assert EbayScraper.VerifyPendingOutcomes(hours_after=2, give_up_days=7) == 0
        assert not any('ResolveAttempts = ResolveAttempts + 1' in c[0][0]
                       for c in cur.execute.call_args_list)

    def test_open_circuit_stops_the_pass(self):
        conn, cur = self._make_conn([(1,), (2,), (3,)])
        with patch.object(EbayScraper, '_get_connection', return_value=conn), \
             patch.object(EbayScraper, '_fetch_direct', return_value=None) as mock_fetch, \
             patch.object(EbayScraper, 'last_block_reason', EbayScraper.CIRCUIT_OPEN):
            EbayScraper.VerifyPendingOutcomes(hours_after=2, give_up_days=7)
        assert mock_fetch.call_count == 1      # the rest wait for the next run


# ═══════════════════════════════════════════════════════════════════════════════
# 8b. Item-page outcome parsing — real captured ended-auction pages
# ═══════════════════════════════════════════════════════════════════════════════

_FIX = os.path.join(os.path.dirname(__file__), 'fixtures')
ITEM_SOLD_HTML = open(os.path.join(_FIX, 'ebay_item_ended_sold_2026-09.html'), encoding='utf-8').read()
ITEM_UNSOLD_HTML = open(os.path.join(_FIX, 'ebay_item_ended_unsold_2026-09.html'), encoding='utf-8').read()
ITEM_LIVE_HTML = open(os.path.join(_FIX, 'ebay_item_page_2026-07.html'), encoding='utf-8').read()


class TestParseItemOutcome:
    def test_sold_page(self):
        # 9-bid RAM kit; the page showed "EUR 47.10" but currentBidPrice is GBP
        r = EbayScraper.parse_item_outcome(ITEM_SOLD_HTML)
        assert r == {'state': 'sold', 'price': 40.49, 'bids': 9,
                     'end': datetime(2026, 9, 24, 6, 15, 6)}

    def test_no_bid_page_is_unsold(self):
        r = EbayScraper.parse_item_outcome(ITEM_UNSOLD_HTML)
        assert r['state'] == 'unsold' and r['bids'] == 0 and r['price'] is None

    def test_live_page_is_active(self):
        assert EbayScraper.parse_item_outcome(ITEM_LIVE_HTML)['state'] == 'active'

    def test_catalogue_redirect_is_gone(self):
        r = EbayScraper.parse_item_outcome('<html>x</html>', 'https://www.ebay.co.uk/p/11046131534')
        assert r['state'] == 'gone'

    def test_unrecognised_page_is_unknown(self):
        assert EbayScraper.parse_item_outcome('<html>nothing</html>')['state'] == 'unknown'

    def test_sold_banner_without_readable_price_never_guesses(self):
        html = ITEM_SOLD_HTML.replace('"currency":"GBP","convertedFromValue"',
                                      '"currency":"EUR","convertedFromValue"')
        assert EbayScraper.parse_item_outcome(html)['state'] == 'unknown'


class TestResolveAlerts:
    def test_blind_sold_detection_alerts(self):
        assert EbayScraper.resolve_alerts({'ended_with_bids': 12, 'sold': 0})

    def test_healthy_or_thin_runs_do_not(self):
        assert not EbayScraper.resolve_alerts({'ended_with_bids': 12, 'sold': 7})
        assert not EbayScraper.resolve_alerts({'ended_with_bids': 4, 'sold': 0})
        assert not EbayScraper.resolve_alerts({})


class TestResolveEndedAuctions:
    def test_repair_mode_may_overwrite_wrongly_closed_rows(self):
        cur = MagicMock()
        cur.fetchall.return_value = [(128083783938,)]
        conn = MagicMock()
        conn.cursor.return_value = cur
        with patch.object(EbayScraper, '_get_connection', return_value=conn), \
             patch.object(EbayScraper, '_fetch_direct', return_value=ITEM_SOLD_HTML):
            stats = EbayScraper.ResolveEndedAuctions(limit=5, repair=True)
        assert stats['sold'] == 1
        select_sql = cur.execute.call_args_list[0][0][0]
        assert 'VerifyMisses >= 2' in select_sql
        ebay = [c for c in cur.execute.call_args_list if 'Price = %s' in c[0][0]]
        assert ebay[0][0][1][-1] is True        # repair flag reaches the WHERE

    def test_repair_confirmed_unsold_leaves_the_queue(self):
        # the repair query keys on VerifyMisses >= 2; a confirmed unsold
        # verdict must clear it or the row is re-fetched forever
        cur = MagicMock()
        EbayScraper._apply_item_outcome(cur, 1, {'state': 'unsold', 'bids': 0, 'end': None},
                                        repair=True)
        assert any('VerifyMisses = 0' in c[0][0] for c in cur.execute.call_args_list)


# ═══════════════════════════════════════════════════════════════════════════════
# 8c. Block handling — classification, sign-in wall, per-query resilience
# ═══════════════════════════════════════════════════════════════════════════════

class TestClassifyResponse:
    BIG = 'x' * 60_000

    def test_real_page(self):
        assert EbayScraper.classify_response(200, 'https://www.ebay.co.uk/sch/i.html', self.BIG) is None

    def test_http_error(self):
        assert EbayScraper.classify_response(403, '', self.BIG) == 'HTTP 403'

    def test_signin_redirect_is_the_wall_even_when_large(self):
        # the real sign-in page is ~116 KB — it used to sail past the size check
        url = 'https://signin.ebay.co.uk/ws/eBayISAPI.dll?SignIn&siteid=3'
        assert EbayScraper.classify_response(200, url, self.BIG) == EbayScraper.SIGNIN_WALL

    def test_bot_challenge(self):
        html = '<title>Pardon our interruption...</title>' + self.BIG
        assert EbayScraper.classify_response(200, '', html) == 'bot challenge'

    def test_tiny_page(self):
        assert EbayScraper.classify_response(200, '', '<html></html>') == 'too small'


class TestSignInWall:
    def setup_method(self):
        EbayScraper._sold_wall_until = 0.0

    def teardown_method(self):
        EbayScraper._sold_wall_until = 0.0

    def test_wall_skips_zyte_and_sold_searches_but_keeps_active(self):
        calls = []

        def fake_direct(url):
            calls.append(url)
            if 'LH_Sold=1' in url:
                EbayScraper.last_block_reason = EbayScraper.SIGNIN_WALL
                return None
            EbayScraper.last_block_reason = None
            return LARGE_HTML

        with patch.object(EbayScraper, '_fetch_direct', side_effect=fake_direct), \
             patch.object(EbayScraper, '_fetch_zyte') as mock_zyte:
            EbayScraper.Scrape('rtx 3060', 'GPU', country='uk', condition='used',
                               listing_type='auction')
            EbayScraper.Scrape('rtx 3070', 'GPU', country='uk', condition='used',
                               listing_type='auction')
        mock_zyte.assert_not_called()
        sold = [u for u in calls if 'LH_Sold=1' in u]
        assert len(sold) == 1                   # second query didn't retry the wall
        assert len(calls) == 3                  # both active searches still ran


class TestScrapeAndUploadResilience:
    def test_one_failed_query_does_not_abort_the_category(self):
        conn = MagicMock()
        with patch.object(EbayScraper, '_get_connection', return_value=conn), \
             patch.object(EbayScraper, 'Scrape',
                          side_effect=[RuntimeError('blocked'), [], []]) as mock_scrape:
            assert EbayScraper.ScrapeAndUpload(['a', 'b', 'c'], 'GPU', country='uk') == (0, 0)
        assert mock_scrape.call_count == 3

    def test_all_queries_failing_fails_the_category(self):
        conn = MagicMock()
        with patch.object(EbayScraper, '_get_connection', return_value=conn), \
             patch.object(EbayScraper, 'Scrape', side_effect=RuntimeError('blocked')):
            with pytest.raises(RuntimeError, match='all 2 GPU queries failed'):
                EbayScraper.ScrapeAndUpload(['a', 'b'], 'GPU', country='uk')

    def test_open_breaker_is_waited_out_and_the_query_retried(self):
        conn, waited = MagicMock(), MagicMock(return_value=True)
        with patch.object(EbayScraper, '_get_connection', return_value=conn), \
             patch.object(EbayScraper, 'breaker_remaining', return_value=600.0), \
             patch.object(EbayScraper, 'Scrape',
                          side_effect=[RuntimeError('circuit open'), [], []]) as mock_scrape:
            assert EbayScraper.ScrapeAndUpload(['a', 'b'], 'GPU', country='uk',
                                               on_breaker=waited) == (0, 0)
        waited.assert_called_once()
        assert mock_scrape.call_count == 3      # 'a' twice, then 'b'

    def test_no_retry_when_the_caller_declines_to_wait(self):
        conn = MagicMock()
        with patch.object(EbayScraper, '_get_connection', return_value=conn), \
             patch.object(EbayScraper, 'breaker_remaining', return_value=600.0), \
             patch.object(EbayScraper, 'Scrape',
                          side_effect=[RuntimeError('circuit open'), []]) as mock_scrape:
            EbayScraper.ScrapeAndUpload(['a', 'b'], 'GPU', country='uk',
                                        on_breaker=lambda: False)
        assert mock_scrape.call_count == 2

    def test_ordinary_block_does_not_call_on_breaker(self):
        conn, waited = MagicMock(), MagicMock(return_value=True)
        with patch.object(EbayScraper, '_get_connection', return_value=conn), \
             patch.object(EbayScraper, 'breaker_remaining', return_value=0.0), \
             patch.object(EbayScraper, 'Scrape', side_effect=[RuntimeError('blocked'), []]):
            EbayScraper.ScrapeAndUpload(['a', 'b'], 'GPU', country='uk', on_breaker=waited)
        waited.assert_not_called()


class TestBreakerWait:
    def teardown_method(self):
        EbayScraper._breaker_open_until = 0.0

    def test_closed_breaker_returns_at_once(self):
        EbayScraper._breaker_open_until = 0.0
        assert EbayScraper.breaker_remaining() == 0.0
        assert EbayScraper.wait_for_breaker() == 0.0

    def test_waits_until_the_breaker_closes_and_feeds_the_tick(self):
        import time as _t
        EbayScraper._breaker_open_until = _t.monotonic() + 0.25
        ticks = MagicMock()
        waited = EbayScraper.wait_for_breaker(on_tick=ticks, chunk_s=0.1)
        assert waited > 0 and EbayScraper.breaker_remaining() == 0.0
        assert ticks.call_count >= 2


# ═══════════════════════════════════════════════════════════════════════════════
# 9. GetActiveDeals — mocked DB
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
# 10. ScrapeTargeted — mocked DB + _scrape_item_by_id
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
            'ram-type': None, 'speed': None,
        }

    def test_empty_list_returns_zero_without_db(self):
        """Passing an empty items list returns 0 and never opens a DB connection."""
        with patch.object(EbayScraper, '_get_connection') as mock_conn:
            result = EbayScraper.ScrapeTargeted([])
        assert result == 0
        mock_conn.assert_not_called()

    def test_matching_item_upserted(self):
        """When _scrape_item_by_id returns the item, _upload is called and count is 1."""
        conn, cur = self._make_conn()
        item = self._make_item('111222333')

        with patch.object(EbayScraper, '_get_connection', return_value=conn), \
             patch.object(EbayScraper, '_scrape_item_by_id', return_value=item), \
             patch.object(EbayScraper, '_upload') as mock_upload:
            result = EbayScraper.ScrapeTargeted([(111222333, 'GPU', 'GIGABYTE RTX 3070 8GB Gaming OC')])

        assert result == 1
        mock_upload.assert_called_once()
        conn.commit.assert_called_once()

    def test_no_match_logs_debug_and_returns_zero(self):
        """When _scrape_item_by_id returns None (item not in active results), returns 0."""
        conn, cur = self._make_conn()

        with patch.object(EbayScraper, '_get_connection', return_value=conn), \
             patch.object(EbayScraper, '_scrape_item_by_id', return_value=None), \
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
