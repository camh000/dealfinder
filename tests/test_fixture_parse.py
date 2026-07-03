"""Offline regression test for __ParseItems against a saved real eBay page.

eBay's search markup (su-card / s-card class names) churns every few months
and is the single most brittle dependency in this project. This test pins the
parser against a real captured results page so markup drift is caught by
`pytest` instead of silent zero-item scrapes in production.

Refresh the fixture whenever eBay changes markup (or just periodically):

    python -c "import EbayScraper as E; \
        html = E._fetch_direct('https://www.ebay.co.uk/sch/i.html?_from=R40&_nkw=NVIDIA+RTX+30&LH_Complete=1&LH_Sold=1&LH_ItemCondition=3000&LH_Auction=1'); \
        open('tests/fixtures/ebay_gpu_sold_sample.html','w',encoding='utf-8').write(html)"
"""
import os
import sys

import pytest
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import EbayScraper

FIXTURE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'ebay_gpu_sold_sample.html'
)

pytestmark = pytest.mark.skipif(
    not os.path.isfile(FIXTURE),
    reason="fixture not captured yet — see module docstring",
)


@pytest.fixture(scope="module")
def parsed_items():
    with open(FIXTURE, encoding='utf-8') as f:
        soup = BeautifulSoup(f.read(), 'html.parser')
    # Module-private function, accessed the same way test_scraper.py does —
    # this test exists precisely to pin the internals production depends on.
    parse_items = vars(EbayScraper)["__ParseItems"]
    return parse_items(soup, "fixture", "GPU")


def test_fixture_yields_items(parsed_items):
    """A real results page must produce a healthy number of parsed items."""
    assert len(parsed_items) >= 20, (
        f"Only {len(parsed_items)} items parsed from the fixture — "
        "eBay markup has likely drifted from the parser's expectations."
    )


def test_fixture_items_have_critical_fields(parsed_items):
    for item in parsed_items:
        assert item['id'], "every item must have an eBay ID"
        assert item['price'] is not None and item['price'] > 0
        assert item['title']
        assert item['url'].startswith('http')


def test_fixture_model_extraction_rate(parsed_items):
    """At least 60% of GPU titles should yield a model on a real page."""
    with_model = sum(1 for i in parsed_items if i['model'])
    rate = with_model / len(parsed_items)
    assert rate >= 0.6, f"GPU model extraction rate {rate:.0%} below 60% floor"


def test_fixture_prices_are_plausible(parsed_items):
    """Guard against the thousands-separator class of parsing bug."""
    for item in parsed_items:
        assert 1 <= item['price'] <= 10000, (
            f"implausible price £{item['price']} for '{item['title'][:50]}' — "
            "check __ParseRawPrice separator handling"
        )
