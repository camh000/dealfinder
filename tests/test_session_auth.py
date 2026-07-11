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


def test_gate_closes_once_users_exist(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "_users_exist", lambda: True)
    client = app_module.app.test_client()
    resp = client.get("/outcomes")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    assert "next=/outcomes" in resp.headers["Location"]
    assert client.get("/api/stats").status_code == 401
    # exempt paths must stay reachable or nobody can ever sign in
    assert client.get("/login").status_code == 200
    assert client.get("/sw.js").status_code == 200


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
    assert client.get("/api/stats").status_code == 401


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
