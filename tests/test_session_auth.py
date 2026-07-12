"""Session-login gate (App.py _session_gate + the auth APIs).

Without a reachable DB, _users_exist() is False → bootstrap mode: everything
is open and Settings offers "create the admin account". Once users exist the
gate closes: pages redirect to /login, APIs 401. These tests monkeypatch
_users_exist and get_connection so both worlds are testable offline.
"""
import os
import sys

import pytest
from werkzeug.security import generate_password_hash

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def app_module(monkeypatch):
    monkeypatch.delenv("HTTP_USER", raising=False)
    monkeypatch.delenv("HTTP_PASS", raising=False)
    sys.modules.pop("App", None)
    import App
    return App


class _FakeCursor:
    def __init__(self, row):
        self.row = row
        self.lastrowid = 1

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return self.row

    def close(self):
        pass


class _FakeConn:
    def __init__(self, row=None):
        self.row = row

    def cursor(self, dictionary=False):
        return _FakeCursor(self.row)

    def commit(self):
        pass

    def close(self):
        pass


def _fake_user(pw="hunter2long", admin=True):
    return {"ID": 1, "Username": "cam",
            "PasswordHash": generate_password_hash(pw),
            "IsAdmin": 1 if admin else 0}


def test_bootstrap_mode_is_open(app_module):
    """No users (or no DB) → the app behaves exactly as before accounts."""
    client = app_module.app.test_client()
    assert client.get("/outcomes").status_code == 200
    me = client.get("/api/me").get_json()
    assert me["bootstrap"] is True
    assert me["user"] is None


def test_guest_mode_reads_open_writes_gated(app_module, monkeypatch):
    """Once users exist, signed-out visitors browse read-only: pages and
    market-data APIs work, settings APIs and every mutation demand login."""
    monkeypatch.setattr(app_module, "_users_exist", lambda: True)
    client = app_module.app.test_client()
    # guest can view
    assert client.get("/outcomes").status_code == 200
    assert client.get("/settings").status_code == 200
    me = client.get("/api/me").get_json()
    assert me["user"] is None and me["bootstrap"] is False
    # exempt paths reachable
    assert client.get("/login").status_code == 200
    assert client.get("/sw.js").status_code == 200
    # settings APIs blocked even as reads
    for path in ("/api/my-endpoint", "/api/bin-settings",
                 "/api/users", "/api/subscriptions"):
        assert client.get(path).status_code == 401, path
    # mutations blocked (page POSTs redirect to login, API POSTs 401)
    assert client.post("/api/my-endpoint", json={}).status_code == 401
    assert client.post("/api/bin-settings", json={}).status_code == 401
    assert client.post("/api/subscriptions", json={}).status_code == 401
    assert client.delete("/api/users/1").status_code == 401


def test_login_flow(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "_users_exist", lambda: True)
    monkeypatch.setattr(app_module, "get_connection",
                        lambda: _FakeConn(_fake_user()))
    client = app_module.app.test_client()
    bad = client.post("/api/login", json={"username": "cam", "password": "wrong"})
    assert bad.status_code == 401
    ok = client.post("/api/login", json={"username": "cam", "password": "hunter2long"})
    assert ok.get_json()["status"] == "ok"
    me = client.get("/api/me").get_json()
    assert me["user"]["name"] == "cam"
    assert me["user"]["admin"] is True
    client.post("/api/logout")
    # back to guest: reads open, private APIs gated again
    assert client.get("/api/subscriptions").status_code == 401


def test_admin_apis_reject_plain_users(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "_users_exist", lambda: True)
    client = app_module.app.test_client()
    with client.session_transaction() as s:
        s["uid"], s["uname"], s["admin"] = 2, "dad", False
    assert client.get("/api/users").status_code == 403
    assert client.post("/api/users", json={"username": "x", "password": "y" * 8}).status_code == 403
    assert client.delete("/api/users/1").status_code == 403


def test_bootstrap_user_creation_validates_password(app_module):
    client = app_module.app.test_client()
    resp = client.post("/api/users", json={"username": "cam", "password": "short"})
    assert resp.status_code == 400


def test_bin_settings_defaults_without_db(app_module):
    """GET falls back to env defaults when AppConfig is unreachable."""
    cfg = app_module.app.test_client().get("/api/bin-settings").get_json()
    assert cfg["status"] == "ok"
    assert cfg["scan_minutes"] == 30
    assert cfg["min_discount"] == 25
    assert cfg["enabled"] is True


def test_bin_settings_post_requires_admin(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "_users_exist", lambda: True)
    client = app_module.app.test_client()
    with client.session_transaction() as s:
        s["uid"], s["uname"], s["admin"] = 2, "dad", False
    resp = client.post("/api/bin-settings",
                       json={"scan_minutes": 15, "min_discount": 30, "enabled": True})
    assert resp.status_code == 403


def test_subscription_fields_discount(app_module):
    """A discount-% subscription carries a scope + listing type + min-%, no £."""
    import json as _j
    f = app_module._subscription_fields(
        {'kind': 'discount_pct', 'scope_kind': 'filter', 'listing_type': 'bin',
         'filters': {'series': 'RTX'}, 'min_discount': 25})
    assert not isinstance(f, tuple)
    assert f['kind'] == 'discount_pct' and f['scope'] == 'filter' and f['ltype'] == 'bin'
    assert f['target'] is None and f['min_disc'] == 25.0
    assert _j.loads(f['group']) == {'series': 'RTX'}
    # empty filters default to whole-category scope
    f2 = app_module._subscription_fields(
        {'kind': 'discount_pct', 'listing_type': 'auction', 'min_discount': 20})
    assert f2['scope'] == 'all' and f2['ltype'] == 'auction'
    # out-of-range discount rejected
    err = app_module._subscription_fields(
        {'kind': 'discount_pct', 'filters': {}, 'min_discount': 200})
    assert isinstance(err, tuple) and err[1] == 400


def test_subscription_fields_group_discount(app_module):
    """Model-page 'a new auction deal for this model' — a group-scoped discount
    subscription of a specific listing type (the gap Cam flagged)."""
    import json as _j
    f = app_module._subscription_fields(
        {'kind': 'discount_pct', 'scope_kind': 'group', 'listing_type': 'auction',
         'group': {'Model': 'RTX 3060 12GB'}, 'min_discount': 20})
    assert not isinstance(f, tuple)
    assert f['scope'] == 'group' and f['ltype'] == 'auction' and f['kind'] == 'discount_pct'
    assert f['min_disc'] == 20.0 and f['target'] is None
    assert _j.loads(f['group']) == {'Model': 'RTX 3060 12GB'}


def test_subscription_fields_price(app_module):
    f = app_module._subscription_fields(
        {'kind': 'listing_price', 'scope_kind': 'group',
         'group': {'Model': 'RTX 3060 12GB'}, 'target_price': 180})
    assert not isinstance(f, tuple)
    assert f['kind'] == 'listing_price' and f['scope'] == 'group'
    assert f['target'] == 18000 and f['min_disc'] is None
    err = app_module._subscription_fields(
        {'kind': 'listing_price', 'group': {}, 'target_price': -5})
    assert isinstance(err, tuple) and err[1] == 400


def test_bin_settings_post_validates_ranges(app_module):
    client = app_module.app.test_client()   # bootstrap: admin check open
    for body in ({"scan_minutes": 2, "min_discount": 30, "enabled": True},
                 {"scan_minutes": 500, "min_discount": 30, "enabled": True},
                 {"scan_minutes": 30, "min_discount": 2, "enabled": True},
                 {"scan_minutes": 30, "min_discount": 95, "enabled": True},
                 {"scan_minutes": "nope", "min_discount": 30, "enabled": True}):
        assert client.post("/api/bin-settings", json=body).status_code == 400, body


def test_bin_settings_post_validates_filters(app_module):
    client = app_module.app.test_client()
    base = {"scan_minutes": 30, "min_discount": 25, "enabled": True}
    for filters in ({"mobo": "B550"},              # unknown category
                    {"hdd": 123},                  # not a string
                    {"hdd": "x" * 400},            # absurd length
                    "6TB"):                        # not an object
        resp = client.post("/api/bin-settings", json={**base, "filters": filters})
        assert resp.status_code == 400, filters
