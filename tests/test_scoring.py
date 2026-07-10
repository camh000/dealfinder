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

    def test_hdd_stats_split_by_drive_type(self):
        """External vs internal must be a grouping dimension in every HDD query."""
        for sql in (queries.build_deals_query("hdd"),
                    queries.build_count_query("hdd"),
                    queries.build_price_guide_query("hdd")):
            assert "DriveType" in sql

    def test_ram_stats_split_by_form_factor(self):
        """SODIMM vs DIMM must be a grouping dimension in every RAM query."""
        for sql in (queries.build_deals_query("ram"),
                    queries.build_count_query("ram"),
                    queries.build_price_guide_query("ram")):
            assert "FormFactor" in sql

    def test_labels_annotate_non_default_subtype(self):
        assert queries.model_label_for_row("hdd", {"CapacityGB": 4000, "Interface": "SATA", "DriveType": "External"}) == "4TB SATA External"
        assert queries.model_label_for_row("hdd", {"CapacityGB": 4000, "Interface": "SATA", "DriveType": "Internal"}) == "4TB SATA"
        assert queries.model_label_for_row("ram", {"CapacityGB": 16, "Type": "DDR4", "FormFactor": "SODIMM"}) == "16GB DDR4 SODIMM"
        assert queries.model_label_for_row("ram", {"CapacityGB": 16, "Type": "DDR4", "FormFactor": "DIMM"}) == "16GB DDR4"

    @pytest.mark.parametrize("ptype", ALL_TYPES)
    def test_sold_stats_exclude_lots(self, ptype):
        """Bulk discounts are structural — lots must never shape the
        single-unit market medians, in any of the three query kinds."""
        for sql in (queries.build_deals_query(ptype),
                    queries.build_count_query(ptype),
                    queries.build_price_guide_query(ptype)):
            assert "COALESCE(e.Quantity, 1) = 1" in sql

    @pytest.mark.parametrize("ptype", ALL_TYPES)
    def test_deal_condition_is_per_unit(self, ptype):
        """Deal detection (and the badge count) compares price PER UNIT
        against the median, so an N-drive lot isn't priced as one drive."""
        for sql in (queries.build_deals_query(ptype),
                    queries.build_count_query(ptype)):
            assert queries.QTY in sql

    @pytest.mark.parametrize("ptype", ALL_TYPES)
    def test_market_stats_use_recency_window(self, ptype):
        """Medians must only trust recent sales — component prices drift,
        so every stats consumer (deals, counts, guide) shares the window."""
        for sql in (queries.build_deals_query(ptype),
                    queries.build_count_query(ptype),
                    queries.build_price_guide_query(ptype)):
            assert f"e.SoldDate > NOW() - INTERVAL {queries.MARKET_STATS_DAYS} DAY" in sql

    @pytest.mark.parametrize("ptype", ALL_TYPES)
    def test_deal_feed_gates_on_seller_feedback(self, ptype):
        """Low-feedback sellers are hidden from deals + counts, but only once
        they have real history (count >= 3); stats are unaffected."""
        for sql in (queries.build_deals_query(ptype),
                    queries.build_count_query(ptype)):
            assert queries.FEEDBACK_OK in sql
        assert "SellerFeedback" not in queries.build_price_guide_query(ptype)
        assert "e.SellerFeedbackCount < 3" in queries.FEEDBACK_OK

    @pytest.mark.parametrize("ptype", ALL_TYPES)
    def test_deal_feed_drops_unseen_listings(self, ptype):
        """Seller-cancelled listings vanish from search but keep a future
        EndTime — the feed (and its badge count) must require a recent
        LastSeenAt so phantoms drop within one scrape cycle. Stats and the
        price guide are sold-history and never gate on freshness."""
        for sql in (queries.build_deals_query(ptype),
                    queries.build_count_query(ptype)):
            assert queries.FRESH_OK in sql
        assert "LastSeenAt" not in queries.build_price_guide_query(ptype)

    def test_deals_query_exposes_lot_columns(self):
        sql = queries.build_deals_query("hdd")
        assert "AS Quantity" in sql
        assert "AS PerUnitPrice" in sql

    def test_deals_query_exposes_price_breakdown(self):
        """UI shows the eBay listing price with delivery as its own line —
        the query must expose both alongside the effective CurrentPrice."""
        sql = queries.build_deals_query("gpu")
        assert "AS ItemPrice" in sql
        assert "AS Shipping" in sql
        assert "AS CurrentPrice" in sql
        # whole-lot gain: market value of the lot minus the lot price
        assert f"ms.AvgPrice * {queries.QTY}" in sql

    def test_labels_annotate_lots(self):
        assert queries.model_label_for_row("hdd", {"CapacityGB": 4000, "Interface": "SAS", "DriveType": "Internal", "Quantity": 5}) == "4TB SAS ×5"
        assert queries.model_label_for_row("hdd", {"CapacityGB": 4000, "Interface": "SAS", "DriveType": "Internal", "Quantity": 1}) == "4TB SAS"
        # quantity untouched / missing → no suffix (non-lot categories)
        assert queries.model_label_for_row("gpu", {"Model": "RTX 3060"}) == "RTX 3060"


class TestSubtypeClassifiers:
    @pytest.mark.parametrize("title,expected", [
        ("WD Elements 4TB Portable External Hard Drive USB 3.0", "External"),
        ("Seagate Expansion 8TB Desktop External HDD", "External"),
        ("Toshiba Canvio 2TB", "External"),
        ("LaCie Rugged 5TB", "External"),
        ("Seagate Barracuda 4TB 3.5\" SATA Internal Hard Drive", "Internal"),
        ("WD Red 8TB NAS 3.5 inch SATA", "Internal"),
        ("HGST Ultrastar 12TB SAS 7200rpm", "Internal"),
    ])
    def test_drive_type(self, title, expected):
        assert EbayScraper.classify_drive_type(title) == expected

    @pytest.mark.parametrize("title,expected", [
        ("Crucial 16GB DDR4 3200MHz SODIMM Laptop Memory", "SODIMM"),
        ("Samsung 8GB DDR4 SO-DIMM", "SODIMM"),
        ("Kingston 16GB DDR4 SO DIMM Notebook RAM", "SODIMM"),
        ("Corsair Vengeance 32GB DDR4 3600MHz Desktop DIMM", "DIMM"),
        ("G.Skill Trident Z 16GB DDR4 3200", "DIMM"),
    ])
    def test_ram_form_factor(self, title, expected):
        assert EbayScraper.classify_ram_form_factor(title) == expected

    def test_defaults_are_the_common_variant(self):
        assert EbayScraper.classify_drive_type("4TB Hard Drive") == "Internal"
        assert EbayScraper.classify_ram_form_factor("16GB DDR4 2666") == "DIMM"
        assert EbayScraper.classify_drive_type("") == "Internal"
        assert EbayScraper.classify_ram_form_factor("") == "DIMM"


class TestLotQuantity:
    @pytest.mark.parametrize("title,expected", [
        ("5 x 4TB Seagate Constellation SAS Hard Drive", 5),
        ("10x500GB WD Blue SATA", 10),
        ("Job lot of 8 assorted hard drives 1TB SATA", 8),
        ("Joblot of 3 Seagate 2TB drives", 3),
        ("Bundle of 6 HGST 4TB SAS", 6),
        ("Seagate 4TB SATA hard drive x4", 4),
        ("Job lot x6 WD 2TB hard drives", 6),
        # not lots
        ("WD Red 4TB NAS Hard Drive", 1),
        ("Seagate Barracuda 8TB 3.5\" SATA", 1),
        # form factor must not read as a quantity
        ("Job lot of 3.5\" SATA hard drives", 1),
        # implausible quantity → treated as a single (prices itself out)
        ("99 x 4TB drives", 1),
    ])
    def test_quantity_extraction(self, title, expected):
        assert EbayScraper.extract_lot_quantity(title) == expected

    def test_empty_and_none_are_singles(self):
        assert EbayScraper.extract_lot_quantity("") == 1
        assert EbayScraper.extract_lot_quantity(None) == 1

    def test_flash_media_lots_skipped_from_hdd(self):
        """USB-stick job lots must not enter the HDD category at all."""
        from bs4 import BeautifulSoup
        # Minimal new-markup card wrapping a flash-drive lot title
        html = """
        <div class="su-card-container su-card-container--horizontal">
          <a href="https://www.ebay.co.uk/itm/111222333444">x</a>
          <a class="su-link su-item-card__title"><span>Job Lot 64GB x 2 Sandisk USB 3.0 Flash Drive Memory Stick</span></a>
          <span class="su-item-card__price">£6.50</span>
        </div>"""
        parse_items = vars(EbayScraper)["__ParseItems"]
        assert parse_items(BeautifulSoup(html, 'html.parser'), "t", "HDD") == []

    @pytest.mark.parametrize("title,risky", [
        ("Job lot of 5 hard drives UNTESTED", True),
        ("10 x 2TB drives spares or repairs", True),
        ("4 x 4TB SAS faulty for parts", True),
        ("5 x 4TB Seagate SAS wiped and tested", False),
        ("Job lot of 8 WD 2TB fully working", False),
    ])
    def test_risk_filter(self, title, risky):
        assert EbayScraper.lot_is_risky(title) is risky


class TestGpuVramSplit:
    def test_dual_vram_models_get_suffixed(self):
        assert EbayScraper.qualify_gpu_model("RTX 3060", 12) == "RTX 3060 12GB"
        assert EbayScraper.qualify_gpu_model("RTX 3060", 8) == "RTX 3060 8GB"
        assert EbayScraper.qualify_gpu_model("GTX 1060", 6) == "GTX 1060 6GB"
        assert EbayScraper.qualify_gpu_model("RX 580", 4) == "RX 580 4GB"

    def test_unknown_or_implausible_vram_keeps_bare_name(self):
        # No VRAM parsed → thin bare-name group, excluded by the stats floor
        assert EbayScraper.qualify_gpu_model("RTX 3060", None) == "RTX 3060"
        # 24GB isn't a real 3060 variant — a bundle's system RAM misread
        assert EbayScraper.qualify_gpu_model("RTX 3060", 24) == "RTX 3060"

    def test_single_variant_models_unchanged(self):
        assert EbayScraper.qualify_gpu_model("RTX 3060 TI", 8) == "RTX 3060 TI"
        assert EbayScraper.qualify_gpu_model("RTX 4090", 24) == "RTX 4090"
        assert EbayScraper.qualify_gpu_model(None, 8) is None

    def test_parse_integration(self):
        from bs4 import BeautifulSoup
        html = """
        <div class="su-card-container su-card-container--horizontal">
          <a href="https://www.ebay.co.uk/itm/111222333444">x</a>
          <a class="su-link su-item-card__title"><span>MSI GeForce RTX 3060 12GB Gaming X Graphics Card</span></a>
          <span class="su-item-card__price">£220.00</span>
        </div>"""
        parse_items = vars(EbayScraper)["__ParseItems"]
        items = parse_items(BeautifulSoup(html, 'html.parser'), "t", "GPU")
        assert items[0]['model'] == "RTX 3060 12GB"
        assert items[0]['vram'] == 12


def _card(title, price="£100.00", item_id="111222333444"):
    """Minimal new-markup result card for parse tests."""
    from bs4 import BeautifulSoup
    html = f"""
    <div class="su-card-container su-card-container--horizontal">
      <a href="https://www.ebay.co.uk/itm/{item_id}">x</a>
      <a class="su-link su-item-card__title"><span>{title}</span></a>
      <span class="su-item-card__price">{price}</span>
    </div>"""
    return BeautifulSoup(html, 'html.parser')


def _parse_one(title, product_type, **kw):
    items = vars(EbayScraper)["__ParseItems"](_card(title, **kw), "t", product_type)
    return items[0] if items else None


class TestArcGpuParsing:
    @pytest.mark.parametrize("title,model,brand,vram", [
        ("Sparkle Intel Arc A750 8GB GDDR6 Graphics Card", "ARC A750", "Sparkle", 8),
        ("Intel Arc B580 12GB Limited Edition GPU", "ARC B580", "Intel", 12),
        ("ASRock Intel Arc A380 Challenger 6GB", "ARC A380", "Asrock", 6),
    ])
    def test_arc_models(self, title, model, brand, vram):
        item = _parse_one(title, "GPU")
        assert item['model'] == model
        assert item['brand'] == brand
        assert item['vram'] == vram

    def test_a770_vram_variants_split(self):
        assert _parse_one("Intel Arc A770 16GB Graphics Card", "GPU")['model'] == "ARC A770 16GB"
        assert _parse_one("Intel Arc A770 8GB Graphics Card", "GPU")['model'] == "ARC A770 8GB"

    def test_arc_does_not_hijack_other_gpus(self):
        assert _parse_one("MSI RTX 4070 Gaming X 12GB", "GPU")['model'] == "RTX 4070"
        assert _parse_one("Sapphire RX 6700 XT 12GB", "GPU")['brand'] == "Sapphire"


class TestXeonParsing:
    @pytest.mark.parametrize("title,model", [
        ("Intel Xeon E5-2680 V4 14 Core 2.4GHz LGA2011-3 CPU", "Xeon E5-2680 V4"),
        ("Intel Xeon E5-2690v3 12-Core Processor", "Xeon E5-2690 V3"),
        ("Intel Xeon E3-1230 V2 Quad Core CPU", "Xeon E3-1230 V2"),
        ("Intel Xeon Gold 6248R 24 Core CPU", "Xeon Gold 6248R"),
        ("Intel Xeon Silver 4114 2.2GHz 10 Core", "Xeon Silver 4114"),
        ("Intel Xeon Platinum 8168 CPU", "Xeon Platinum 8168"),
        ("Intel Xeon W-2145 8 Core Workstation CPU", "Xeon W-2145"),
        ("Intel Xeon E-2224G 4-Core CPU", "Xeon E-2224G"),
        ("Intel Xeon X5670 Six Core 2.93GHz", "Xeon X5670"),
    ])
    def test_xeon_models(self, title, model):
        item = _parse_one(title, "CPU")
        assert item is not None, f"listing dropped: {title}"
        assert item['model'] == model
        assert item['brand'] == "Intel"

    def test_xeon_socket_and_cores_extracted(self):
        item = _parse_one("Intel Xeon E5-2680 V4 14 Core 2.4GHz LGA2011-3 CPU", "CPU")
        assert item['socket'] == "LGA2011"
        assert item['cores'] == 14

    def test_matched_pair_is_a_lot(self):
        item = _parse_one("2x Intel Xeon E5-2690 V4 Matched Pair 14 Core", "CPU")
        assert item['quantity'] == 2
        assert item['model'] == "Xeon E5-2690 V4"

    def test_untested_pair_skipped(self):
        assert _parse_one("2x Intel Xeon Gold 6132 untested spares", "CPU") is None

    def test_whole_servers_skipped(self):
        assert _parse_one("Dell PowerEdge R730 2x Xeon E5-2680 V4 64GB Server", "CPU") is None
        assert _parse_one("HP ProLiant DL380 Gen9 Xeon E5-2650", "CPU") is None

    def test_cpu_motherboard_ram_combo_skipped(self):
        assert _parse_one("Intel Xeon E5-2680 V4 + X99 Motherboard + 32GB DDR4 RAM Combo", "CPU") is None

    def test_core_i_models_unaffected(self):
        assert _parse_one("Intel Core i7-9700K 8 Core CPU", "CPU")['model'] == "i7-9700K"


class TestFieldCoverage:
    def _healthy(self):
        return {'items': 1000, 'feedback': 950, 'shipping': 400,
                'sold_items': 500, 'sold_date': 480,
                'active_items': 500, 'end_time': 470, 'bids': 200}

    def test_healthy_run_raises_nothing(self):
        assert EbayScraper.coverage_alerts(self._healthy()) == []

    def test_collapsed_field_alerts(self):
        cov = self._healthy()
        cov['shipping'] = 0          # the silent-£0 shipping bug, redetected
        alerts = EbayScraper.coverage_alerts(cov)
        assert len(alerts) == 1 and 'shipping' in alerts[0]

    def test_multiple_collapses_all_reported(self):
        cov = self._healthy()
        cov['sold_date'] = 0
        cov['end_time'] = 0
        assert len(EbayScraper.coverage_alerts(cov)) == 2

    def test_small_runs_never_alert(self):
        """A failed/partial run mustn't flap the alarm — the zero-rows guard
        owns that case."""
        cov = {k: 0 for k in self._healthy()}
        cov['items'] = 40
        assert EbayScraper.coverage_alerts(cov) == []

    def test_thin_denominator_skipped(self):
        cov = self._healthy()
        cov['sold_items'] = 10      # only 10 sold items seen this run
        cov['sold_date'] = 0        # ...none dated: too few to judge
        assert EbayScraper.coverage_alerts(cov) == []

    def test_none_coverage_is_quiet(self):
        assert EbayScraper.coverage_alerts(None) == []


class TestAnnotatePredictions:
    def _row(self, **kw):
        from datetime import datetime
        base = {'ID': 1, 'CurrentPrice': 100.0, 'AvgMarketPrice': 150.0,
                'Quantity': 1, 'Bids': 0, 'DiscountPct': 33.3, 'DealScore': 5.0,
                'EndTime': datetime(2026, 7, 9, 14, 0)}
        base.update(kw)
        return base

    NOW = None

    def setup_method(self):
        from datetime import datetime
        type(self).NOW = datetime(2026, 7, 9, 12, 0)  # rows end 2h later

    def test_premium_ratio_applied_by_bucket(self):
        rows = [self._row(Bids=5)]
        premiums = {('GPU', '4+'): (1.2, 10)}
        queries.annotate_predictions(rows, 'gpu', premiums, now=self.NOW)
        assert rows[0]['PredictedFinalPrice'] == 120.0
        assert rows[0]['PremiumSamples'] == 10
        assert rows[0]['PredictedDiscountPct'] == 20.0   # 1 - 120/150

    def test_category_all_fallback(self):
        rows = [self._row(Bids=1)]
        premiums = {('GPU', 'all'): (1.1, 8)}            # no '1-3' bucket
        queries.annotate_predictions(rows, 'gpu', premiums, now=self.NOW)
        assert rows[0]['PredictedFinalPrice'] == 110.0

    def test_no_history_is_identity(self):
        rows = [self._row()]
        queries.annotate_predictions(rows, 'gpu', {}, now=self.NOW)
        assert rows[0]['PredictedFinalPrice'] == 100.0
        assert rows[0]['PremiumSamples'] == 0
        # predicted == current → predicted discount == current discount
        assert rows[0]['PredictedDiscountPct'] == 33.3

    def test_lot_market_scaling(self):
        # ×5 lot at £100 total, £30/unit market → lot market £150
        rows = [self._row(Quantity=5, AvgMarketPrice=30.0)]
        queries.annotate_predictions(rows, 'hdd', {}, now=self.NOW)
        assert rows[0]['PredictedDiscountPct'] == 33.3

    def test_filter_drops_predicted_over_market(self):
        """The feed only shows deals predicted to close BELOW market."""
        rows = [self._row(ID=1, Bids=6), self._row(ID=2, Bids=0)]
        premiums = {('HDD', '4+'): (1.56, 12)}   # erases the contested row's edge
        queries.annotate_predictions(rows, 'hdd', premiums, now=self.NOW)
        kept = queries.filter_predicted_deals(rows)
        assert [r['ID'] for r in kept] == [2]

    def test_filter_keeps_rows_without_history(self):
        """No premium data → prediction equals current price → passes through."""
        rows = [self._row()]
        queries.annotate_predictions(rows, 'gpu', {}, now=self.NOW)
        assert queries.filter_predicted_deals(rows) == rows

    def test_filter_keeps_none_predicted_discount(self):
        """Rows the annotator couldn't price (no market value) aren't dropped."""
        assert queries.filter_predicted_deals(
            [{'PredictedDiscountPct': None}]) == [{'PredictedDiscountPct': None}]

    def test_dealscore_recomputed_and_resorted(self):
        # Same current discount; the contested row's premium erases its edge.
        contested = self._row(ID=1, Bids=6)
        quiet = self._row(ID=2, Bids=0)
        rows = [contested, quiet]
        premiums = {('HDD', '4+'): (1.56, 12)}           # HDD snipe premium
        queries.annotate_predictions(rows, 'hdd', premiums, now=self.NOW)
        assert rows[0]['ID'] == 2, "quiet auction should now outrank the contested one"
        assert contested['PredictedDiscountPct'] < 0     # predicted OVER market
        assert contested['DealScore'] == 0.0             # negative discount floors to 0


class TestNearMissCohort:
    def test_premium_training_excludes_cohort(self):
        """Premiums must stay trained on the population they predict for —
        the 12–20% control band would contaminate the ratios."""
        assert "d.NearMiss = 0" in queries.SNIPE_PREMIUM_QUERY

    def test_surface_deals_classifies_and_gates(self):
        """Rows below min_discount are recorded flagged NearMiss=1 and are
        NOT returned for notification; real deals are."""
        from datetime import datetime
        from unittest.mock import MagicMock, patch
        deal = {'ID': 1, 'CurrentPrice': 80.0, 'AvgMarketPrice': 100.0,
                'DiscountPct': 25.0, 'Bids': 0, 'Quantity': 1, 'Model': 'RTX 3070',
                'EndTime': datetime(2026, 7, 10, 12, 0)}
        near = {**deal, 'ID': 2, 'CurrentPrice': 85.0, 'DiscountPct': 15.0}
        cur = MagicMock()
        cur.fetchall.return_value = [deal, near]
        cur.rowcount = 1
        conn = MagicMock()
        conn.cursor.return_value = cur
        gpu_only = {'gpu': queries.CATEGORIES['gpu']}
        with patch.object(EbayScraper, '_get_connection', return_value=conn), \
             patch.object(EbayScraper, 'GetSnipePremiums', return_value={}), \
             patch.object(queries, 'CATEGORIES', gpu_only):
            new_deals = EbayScraper.SurfaceDeals(2, 20, nearmiss_discount=12)

        # only the real deal comes back for notification
        assert [r['ID'] for r in new_deals] == [1]
        # both rows were recorded, with the right NearMiss flags
        inserts = [c[0][1] for c in cur.execute.call_args_list
                   if 'INSERT IGNORE INTO Scraper.DealOutcomes' in str(c[0][0])]
        assert len(inserts) == 2
        by_id = {p[0]: p for p in inserts}
        assert by_id[1][-1] == 0   # NearMiss flag is the last param
        assert by_id[2][-1] == 1

    def test_cohort_disabled_when_thresholds_equal(self):
        """nearmiss == min_discount → query runs at min_discount, no band."""
        from unittest.mock import MagicMock, patch
        cur = MagicMock()
        cur.fetchall.return_value = []
        conn = MagicMock()
        conn.cursor.return_value = cur
        with patch.object(EbayScraper, '_get_connection', return_value=conn), \
             patch.object(EbayScraper, 'GetSnipePremiums', return_value={}), \
             patch.object(queries, 'CATEGORIES', {'gpu': queries.CATEGORIES['gpu']}):
            EbayScraper.SurfaceDeals(2, 20, nearmiss_discount=20)
        sql = cur.execute.call_args_list[0][0][0]
        # threshold factor for 20% is 0.8 — the query ran at min_discount
        assert "* 0.8" in sql


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
