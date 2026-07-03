"""
Tests for the scoring layer added in the deep-dive audit (batch 3):
  - queries.py median-based market stats + DealScore
  - EbayScraper snipe-premium helpers (_bid_bucket / _median_ratios)

All pure-Python / SQL-string tests — no DB or network required.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

import queries
import EbayScraper


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Query builders
# ═══════════════════════════════════════════════════════════════════════════════

ALL_TYPES = list(queries.CATEGORIES)


class TestQueryBuilders:
    @pytest.mark.parametrize("ptype", ALL_TYPES)
    def test_deals_query_renders(self, ptype):
        sql = queries.build_deals_query(ptype, window_hours=2, min_discount=20)
        assert "{" not in sql and "}" not in sql, "unrendered placeholder"
        assert "MEDIAN(Eff) OVER" in sql
        assert "AS DealScore" in sql
        assert "ORDER BY DealScore DESC" in sql
        # market stats gated on enough sold history
        assert "rs.SoldCount >= 5" in sql

    @pytest.mark.parametrize("ptype", ALL_TYPES)
    def test_count_query_renders_and_matches_median_basis(self, ptype):
        sql = queries.build_count_query(ptype, window_hours=2, min_discount=20)
        assert "{" not in sql and "}" not in sql
        assert "MEDIAN(Eff) OVER" in sql
        assert "rs.MedPrice" in sql
        assert "rs.SoldCount >= 5" in sql

    @pytest.mark.parametrize("ptype", ALL_TYPES)
    def test_price_guide_renders(self, ptype):
        sql = queries.build_price_guide_query(ptype)
        assert "{" not in sql and "}" not in sql
        assert "MEDIAN(Eff) OVER" in sql
        # guide keeps its public column contract
        for col in ("AvgPrice", "MinPrice", "MaxPrice", "SoldCount"):
            assert col in sql
        # guide includes thinner models than the deal feed
        assert "rs.SoldCount >= 3" in sql

    def test_outlier_band_present(self):
        sql = queries.build_deals_query("gpu")
        assert f"rs.MedPrice * {queries.BAND_LO}" in sql
        assert f"rs.MedPrice * {queries.BAND_HI}" in sql

    def test_window_clamped(self):
        sql = queries.build_deals_query("gpu", window_hours=99)
        assert "INTERVAL 24 HOUR" in sql
        sql = queries.build_deals_query("gpu", window_hours=0)
        assert "INTERVAL 1 HOUR" in sql

    def test_dealscore_guards(self):
        """Score must be safe near auction end (floor) and for NULL bids."""
        sql = queries.build_deals_query("gpu")
        assert "GREATEST(TIMESTAMPDIFF(MINUTE, NOW(), e.EndTime) / 60.0, 0.25)" in sql
        assert "1 + COALESCE(e.Bids, 0)" in sql

    def test_hdd_uses_null_safe_join(self):
        """HDD groups on Interface which may be NULL — joins must be <=>."""
        sql = queries.build_deals_query("hdd")
        assert "<=>" in sql


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Snipe-premium helpers
# ═══════════════════════════════════════════════════════════════════════════════

class TestBidBucket:
    def test_edges(self):
        assert EbayScraper._bid_bucket(0) == "0"
        assert EbayScraper._bid_bucket(None) == "0"
        assert EbayScraper._bid_bucket(1) == "1-3"
        assert EbayScraper._bid_bucket(3) == "1-3"
        assert EbayScraper._bid_bucket(4) == "4+"
        assert EbayScraper._bid_bucket(27) == "4+"


class TestMedianRatios:
    def _rows(self, ratios, cat="GPU", bids=0):
        # surfaced 10000p; final = surfaced * ratio
        return [(cat, bids, 10000, int(10000 * r)) for r in ratios]

    def test_median_of_bucket(self):
        rows = self._rows([1.0, 1.1, 1.2, 1.3, 1.4])
        out = EbayScraper._median_ratios(rows, min_samples=5)
        ratio, n = out[("GPU", "0")]
        assert ratio == 1.2
        assert n == 5
        # category-level fallback aggregates the same rows
        assert out[("GPU", "all")] == (1.2, 5)

    def test_min_samples_filters_thin_buckets(self):
        rows = self._rows([1.0, 1.5])  # only 2 samples
        out = EbayScraper._median_ratios(rows, min_samples=5)
        assert out == {}

    def test_all_fallback_survives_when_buckets_thin(self):
        # 3 zero-bid + 3 contested sales: neither bucket reaches 5,
        # but the category-level 'all' pool does at min_samples=5.
        rows = self._rows([1.0, 1.0, 1.0], bids=0) + self._rows([2.0, 2.0, 2.0], bids=5)
        out = EbayScraper._median_ratios(rows, min_samples=5)
        assert ("GPU", "0") not in out
        assert ("GPU", "4+") not in out
        assert out[("GPU", "all")] == (1.5, 6)

    def test_zero_surfaced_price_skipped(self):
        rows = [("GPU", 0, 0, 12000)] + self._rows([1.0] * 5)
        out = EbayScraper._median_ratios(rows, min_samples=5)
        assert out[("GPU", "0")][1] == 5  # divide-by-zero row dropped

    def test_none_final_price_skipped(self):
        rows = [("GPU", 0, 10000, None)] + self._rows([1.0] * 5)
        out = EbayScraper._median_ratios(rows, min_samples=5)
        assert out[("GPU", "0")][1] == 5

    def test_categories_kept_separate(self):
        rows = self._rows([1.0] * 5, cat="GPU") + self._rows([2.0] * 5, cat="HDD")
        out = EbayScraper._median_ratios(rows, min_samples=5)
        assert out[("GPU", "0")][0] == 1.0
        assert out[("HDD", "0")][0] == 2.0

    def test_decimal_like_inputs(self):
        """mariadb returns Decimal for price columns — helper must coerce."""
        from decimal import Decimal
        rows = [("GPU", 0, Decimal("10000"), Decimal("11000"))] * 5
        out = EbayScraper._median_ratios(rows, min_samples=5)
        assert out[("GPU", "0")][0] == 1.1
