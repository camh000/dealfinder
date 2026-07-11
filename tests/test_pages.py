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
    ("/deals/gpu", "GPU deals"),
    ("/deals/ssd", "SSD deals"),
    ("/outcomes", "Outcomes"),
    ("/prices", "Price guide"),
    ("/model/gpu?Model=RTX+3060+12GB", "price guide"),
    ("/settings", "Settings"),
    ("/deal/123456789012", "Deal detail"),
    ("/health", "System health"),
])
def test_page_renders(client, path, marker):
    resp = client.get(path)
    assert resp.status_code == 200
    assert marker.encode() in resp.data


def test_root_redirects_to_deals(client):
    resp = client.get("/")
    assert resp.status_code == 302
    assert "/deals/gpu" in resp.headers["Location"]


def test_unknown_category_404s(client):
    assert client.get("/deals/psu").status_code == 404
    assert client.get("/model/psu").status_code == 404


def test_pages_share_base_chrome(client):
    resp = client.get("/outcomes")
    for marker in (b"app.css", b"common.js", b"PC", b"nav"):
        assert marker in resp.data


def test_sw_served_with_version(client):
    resp = client.get("/sw.js")
    assert resp.status_code == 200
    assert b"pcd-" in resp.data
    assert b"'pcd-v1'" not in resp.data  # placeholder must be rewritten
