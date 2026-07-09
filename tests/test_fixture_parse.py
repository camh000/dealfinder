"""Offline regression tests for __ParseItems against saved real eBay pages.

eBay's search markup (su-card / s-card class names) churns every few months
and is the single most brittle dependency in this project. These tests pin the
parser against real captured results pages so markup drift is caught by
`pytest` instead of silent zero-item scrapes in production.

Fixtures:
  ebay_gpu_sold_sample.html   — 2026-07-04 capture, legacy s-card__* markup
  ebay_gpu_sold_2026-07.html  — 2026-07-08 capture, su-item-card__* markup
  ebay_gpu_active_2026-07.html — same date, ACTIVE results (countdown div,
                                 split delivery spans — sold pages lack both)

Refresh whenever eBay changes markup (or just periodically):

    python -c "import EbayScraper as E; \
        html = E._fetch_direct('https://www.ebay.co.uk/sch/i.html?_from=R40&_nkw=NVIDIA+RTX+30&LH_Complete=1&LH_Sold=1&LH_ItemCondition=3000&LH_Auction=1'); \
        open('tests/fixtures/ebay_gpu_sold_2026-07.html','w',encoding='utf-8').write(html)"

(for the active fixture use &_sop=1 instead of &LH_Complete=1&LH_Sold=1)
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import EbayScraper

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')

SOLD_FIXTURES = [
    'ebay_gpu_sold_sample.html',    # legacy markup
    'ebay_gpu_sold_2026-07.html',   # su-item-card markup
]
ACTIVE_FIXTURES = [
    'ebay_gpu_active_2026-07.html',
]
ALL_FIXTURES = SOLD_FIXTURES + ACTIVE_FIXTURES

_parse_cache = {}


def parse_fixture(name):
    if name not in _parse_cache:
        path = os.path.join(FIXTURE_DIR, name)
        if not os.path.isfile(path):
            pytest.skip(f"fixture {name} not captured yet — see module docstring")
        with open(path, encoding='utf-8') as f:
            soup = BeautifulSoup(f.read(), 'html.parser')
        # Module-private function, accessed the same way test_scraper.py does —
        # these tests exist precisely to pin the internals production depends on.
        parse_items = vars(EbayScraper)["__ParseItems"]
        _parse_cache[name] = parse_items(soup, "fixture", "GPU")
    return _parse_cache[name]


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_fixture_yields_items(fixture):
    """A real results page must produce a healthy number of parsed items."""
    items = parse_fixture(fixture)
    assert len(items) >= 20, (
        f"Only {len(items)} items parsed from {fixture} — "
        "eBay markup has likely drifted from the parser's expectations."
    )


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_fixture_items_have_critical_fields(fixture):
    for item in parse_fixture(fixture):
        assert item['id'], "every item must have an eBay ID"
        assert item['price'] is not None and item['price'] > 0
        assert item['title']
        # URLs are canonicalised at parse time — bare /itm/<id>, no ~800-char
        # tracking query string (which overflowed the VARCHAR(500) column).
        assert item['url'] == f"https://www.ebay.co.uk/itm/{item['id']}"


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_fixture_model_extraction_rate(fixture):
    """At least 60% of GPU titles should yield a model on a real page."""
    items = parse_fixture(fixture)
    with_model = sum(1 for i in items if i['model'])
    rate = with_model / len(items)
    assert rate >= 0.6, f"GPU model extraction rate {rate:.0%} below 60% floor ({fixture})"


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_fixture_prices_are_plausible(fixture):
    """Guard against the thousands-separator class of parsing bug."""
    for item in parse_fixture(fixture):
        assert 1 <= item['price'] <= 10000, (
            f"implausible price £{item['price']} for '{item['title'][:50]}' — "
            "check __ParseRawPrice separator handling"
        )


@pytest.mark.parametrize("fixture", SOLD_FIXTURES)
def test_sold_fixture_has_sold_dates(fixture):
    """Every result on a sold page is sold — the date must parse for most."""
    items = parse_fixture(fixture)
    with_date = sum(1 for i in items if i['sold-date'])
    assert with_date / len(items) >= 0.8, (
        f"only {with_date}/{len(items)} sold dates parsed from {fixture}"
    )


def test_active_fixture_derives_end_times():
    """2026-07 markup shows only a relative countdown — EndTime must be
    derived from it, land in the future, and stay within eBay's 10-day max."""
    items = parse_fixture(ACTIVE_FIXTURES[0])
    with_end = [i for i in items if i['time-end']]
    assert len(with_end) / len(items) >= 0.8, "most active items should get an EndTime"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for i in with_end:
        # generous slack: fixture parse time vs test run time
        assert now - timedelta(days=1) < i['time-end'] < now + timedelta(days=11)


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_fixture_parses_seller_feedback(fixture):
    """Both markups carry 'N% positive (count)' on every card."""
    items = parse_fixture(fixture)
    with_fb = [i for i in items if i['feedback-pct'] is not None]
    assert len(with_fb) / len(items) >= 0.8, "most cards should yield seller feedback"
    for i in with_fb:
        assert 0 <= i['feedback-pct'] <= 100
        assert i['feedback-count'] >= 0


def test_active_fixture_parses_bids_and_shipping():
    """Split-span delivery ("+£36.95 " + "delivery in 2-3 days") and the
    bid-countdown div are active-page features — some rows must yield both."""
    items = parse_fixture(ACTIVE_FIXTURES[0])
    assert any(i['bid-count'] > 0 for i in items), "no bid counts parsed"
    paid_ship = [i for i in items if i['shipping'] and i['shipping'] > 0]
    assert paid_ship, "no shipping amounts parsed — split-span handling broken?"
    for i in paid_ship:
        assert i['shipping'] < 100, f"implausible shipping £{i['shipping']}"
