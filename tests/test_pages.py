"""Multi-page routing smoke tests — every page renders without a DB.

Page templates carry no server-side data (everything loads via fetch), so
a plain GET must return 200 even when MariaDB is unreachable.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="module")
def client(request):
    os.environ.pop("HTTP_USER", None)
    os.environ.pop("HTTP_PASS", None)
    sys.modules.pop("App", None)
    import App
    return App.app.test_client()


@pytest.mark.parametrize("path,marker", [
    ("/deals", "Deals"),
    ("/deals/ssd", "Deals"),          # deep link → unified page, chip preselected
    ("/outcomes", "Outcomes"),
    ("/prices", "Price guide"),
    ("/model/gpu?Model=RTX+3060+12GB", "price guide"),
    ("/settings", "Settings"),
    ("/deal/123456789012", "Deal detail"),
    ("/health", "System health"),
    ("/insights/predictions", "Prediction model"),
    ("/insights/nearmiss", "Near-miss experiment"),
    ("/bin", "Buy It Now"),
])
def test_page_renders(client, path, marker):
    resp = client.get(path)
    assert resp.status_code == 200
    assert marker.encode() in resp.data


def test_root_redirects_to_deals(client):
    resp = client.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"].rstrip("/").endswith("/deals")


def test_unknown_category_404s(client):
    assert client.get("/deals/psu").status_code == 404
    assert client.get("/model/psu").status_code == 404


def test_bin_api_rejects_unknown_category(client):
    assert client.get("/api/bin-deals?type=psu").status_code == 400


def test_pages_share_base_chrome(client):
    resp = client.get("/outcomes")
    for marker in (b"app.css", b"common.js", b"PC", b"nav"):
        assert marker in resp.data


def test_sw_served_with_version(client):
    resp = client.get("/sw.js")
    assert resp.status_code == 200
    assert b"pcd-" in resp.data
    assert b"'pcd-v1'" not in resp.data  # placeholder must be rewritten


def test_wilson_ci_behaves(client):
    import App
    assert App._wilson_ci(0, 0) == [0.0, 0.0]
    lo, hi = App._wilson_ci(5, 5)
    assert lo < 100.0 and hi == 100.0        # 5-for-5 is not "100% proven"
    lo, hi = App._wilson_ci(50, 100)
    assert lo < 50.0 < hi
    assert hi - lo < 25                       # n=100 narrows the interval
    # more data → tighter interval at the same rate
    lo2, hi2 = App._wilson_ci(500, 1000)
    assert (hi2 - lo2) < (hi - lo)


def test_err_stats_shape(client):
    import App
    assert App._err_stats([]) == {"n": 0}
    s = App._err_stats([(10.0, 20.0), (-10.0, 30.0), (0.0, 25.0)])
    assert s["n"] == 3
    assert s["median_abs_err_pct"] == 10.0
    assert s["bias_pct"] == 0.0
    assert s["baseline_median_abs_err_pct"] == 25.0
    assert s["within_10_pct"] == 100.0
