"""HTTP Basic Auth gate on the Flask app (App.py before_request hook).

App.py runs its ensure_* table migrations at import; without a reachable DB
those log errors and continue, so the app object is importable in tests.
Auth env vars are read at import time, so each scenario re-imports App.
"""
import base64
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_app(monkeypatch, user=None, password=None):
    if user is None:
        monkeypatch.delenv("HTTP_USER", raising=False)
    else:
        monkeypatch.setenv("HTTP_USER", user)
    if password is None:
        monkeypatch.delenv("HTTP_PASS", raising=False)
    else:
        monkeypatch.setenv("HTTP_PASS", password)
    sys.modules.pop("App", None)
    import App
    importlib.reload(App)
    return App.app


def _basic(user, password):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_open_when_unconfigured(monkeypatch):
    app = _load_app(monkeypatch)
    resp = app.test_client().get("/outcomes")
    assert resp.status_code == 200


def test_401_without_credentials(monkeypatch):
    app = _load_app(monkeypatch, "cam", "hunter2")
    resp = app.test_client().get("/")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"].startswith("Basic")


def test_401_with_wrong_credentials(monkeypatch):
    app = _load_app(monkeypatch, "cam", "hunter2")
    client = app.test_client()
    assert client.get("/", headers=_basic("cam", "wrong")).status_code == 401
    assert client.get("/", headers=_basic("eve", "hunter2")).status_code == 401


def test_200_with_correct_credentials(monkeypatch):
    app = _load_app(monkeypatch, "cam", "hunter2")
    resp = app.test_client().get("/outcomes", headers=_basic("cam", "hunter2"))
    assert resp.status_code == 200


def test_api_routes_also_gated(monkeypatch):
    app = _load_app(monkeypatch, "cam", "hunter2")
    client = app.test_client()
    for route in ("/api/stats", "/api/deals", "/api/outcomes", "/api/notify-settings"):
        assert client.get(route).status_code == 401, route


def test_half_configured_stays_open(monkeypatch):
    """Only one of the two vars set → auth off (misconfig shouldn't lock out)."""
    app = _load_app(monkeypatch, "cam", None)
    assert app.test_client().get("/outcomes").status_code == 200
