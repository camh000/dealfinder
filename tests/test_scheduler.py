"""Scheduler run-loop behaviour that doesn't need eBay or the DB."""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import EbayScraper
import scheduler


class TestRotation:
    def test_rotates_the_start_and_keeps_every_category(self):
        seq = ['GPU', 'CPU', 'HDD', 'SSD', 'RAM', 'MOBO']
        assert scheduler._rotated(seq, 0) == seq
        assert scheduler._rotated(seq, 1) == ['CPU', 'HDD', 'SSD', 'RAM', 'MOBO', 'GPU']
        assert scheduler._rotated(seq, 6) == seq
        assert sorted(scheduler._rotated(seq, 4)) == sorted(seq)

    def test_every_category_gets_a_turn_at_the_front(self):
        n = len(scheduler.CATEGORY_QUERIES)
        firsts = {scheduler._rotated(scheduler.CATEGORY_QUERIES, i)[0][1] for i in range(n)}
        assert len(firsts) == n


class TestFullRun:
    """run_full_scrape with every side effect stubbed out."""

    def _run(self, scrape_side_effect):
        beats = []
        stubs = dict(
            reset_direct_session=None, reset_field_coverage=None,
            VerifyPendingOutcomes=0, ResolveEndedAuctions={}, SurfaceDeals=[],
            EvaluateSubscriptions=None, PruneStaleListings=0, get_field_coverage={},
            coverage_alerts=[], resolve_alerts=[], RecordScrapeCompleted=None,
        )
        patches = [patch.object(EbayScraper, k, return_value=v) for k, v in stubs.items()]
        patches += [
            patch.object(EbayScraper, 'ScrapeAndUpload', side_effect=scrape_side_effect),
            patch.object(scheduler, 'notify_new_deals'),
            patch.object(scheduler, 'kuma_heartbeat',
                         side_effect=lambda ok, msg: beats.append((ok, msg))),
        ]
        for p in patches:
            p.start()
        try:
            scheduler.run_full_scrape()
        finally:
            for p in patches:
                p.stop()
        return beats

    def test_one_of_six_categories_is_not_a_healthy_beat(self):
        outcomes = iter([(0, 100)] + [RuntimeError('all queries failed')] * 5)

        def scrape(*a, **k):
            r = next(outcomes)
            if isinstance(r, Exception):
                raise r
            return r
        (ok, msg), = self._run(scrape)
        assert ok is False and '1/6' in msg

    def test_most_categories_through_is_healthy(self):
        outcomes = iter([(0, 100)] * 4 + [RuntimeError('all queries failed')] * 2)

        def scrape(*a, **k):
            r = next(outcomes)
            if isinstance(r, Exception):
                raise r
            return r
        (ok, msg), = self._run(scrape)
        assert ok is True and '4/6' in msg

    def test_start_category_moves_on_each_run(self):
        order = []

        def scrape(query_list, product_type, **k):
            order.append(product_type)
            return (0, 1)
        self._run(scrape)
        self._run(scrape)
        n = len(scheduler.CATEGORY_QUERIES)
        assert order[0] != order[n]
        assert sorted(order[:n]) == sorted(order[n:])
