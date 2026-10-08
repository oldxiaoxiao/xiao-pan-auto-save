"""发布回归：密码必须真正保护管理 API，Token API 保持独立。"""

import base64

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.main import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "WEBUI_USERNAME", "admin")
    monkeypatch.setattr(config, "WEBUI_PASSWORD", "测试-password")
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("path", ["/api/tasks", "/api/accounts", "/api/settings", "/api/tokens", "/api/logs", "/api/downloads", "/openapi.json"])
def test_password_blocks_anonymous_management(client, path):
    assert client.get(path).status_code == 401


def test_login_cookie_protects_management_and_logout_revokes_it(client):
    assert client.get("/api/auth/status").json() == {"required": True, "authenticated": False}
    assert client.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    result = client.post("/api/auth/login", json={"username": "admin", "password": "测试-password"})
    assert result.status_code == 200
    assert "httponly" in result.headers["set-cookie"].lower()
    assert "samesite=strict" in result.headers["set-cookie"].lower()
    assert client.get("/api/tasks").status_code == 200
    assert client.get("/api/auth/status").json()["authenticated"]
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/tasks").status_code == 401


def test_basic_auth_and_malformed_headers(client):
    encoded = base64.b64encode("admin:测试-password".encode()).decode()
    assert client.get("/api/settings", headers={"Authorization": f"Basic {encoded}"}).status_code == 200
    assert client.get("/api/settings", headers={"Authorization": "Basic invalid!"}).status_code == 401


def test_password_change_invalidates_session(client, monkeypatch):
    client.post("/api/auth/login", json={"username": "admin", "password": "测试-password"})
    monkeypatch.setattr(config, "WEBUI_PASSWORD", "changed")
    assert client.get("/api/settings").status_code == 401


def test_local_no_password_and_health_remain_available(client, monkeypatch):
    assert client.get("/api/health").status_code == 200
    assert client.get("/tasks").status_code != 401
    monkeypatch.setattr(config, "WEBUI_PASSWORD", "")
    assert client.get("/api/settings").status_code == 200
    assert client.get("/api/auth/status").json() == {"required": False, "authenticated": True}


def test_external_token_is_independent_of_web_password(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "external-test-token")
    assert client.get("/api/v1/task/list").status_code == 401
    assert client.get("/api/v1/task/list", headers={"Authorization": "Bearer external-test-token"}).status_code == 200
    assert client.get("/api/settings", headers={"Authorization": "Bearer external-test-token"}).status_code == 401


def test_desktop_bootstrap_is_one_use(client, monkeypatch):
    monkeypatch.setattr(config, "DESKTOP_TOKEN", "desktop-test-token", raising=False)
    assert client.get("/api/auth/desktop?token=wrong", follow_redirects=False).status_code == 401
    result = client.get("/api/auth/desktop?token=desktop-test-token", follow_redirects=False)
    assert result.status_code == 303 and result.headers["location"] == "/"
    assert client.get("/api/settings").status_code == 200
    assert client.get("/api/auth/desktop?token=desktop-test-token", follow_redirects=False).status_code == 401


def test_cross_origin_management_write_is_rejected(client):
    assert client.post("/api/auth/login", json={"username": "admin", "password": "测试-password"}, headers={"Origin": "https://other.example"}).status_code == 403


def test_static_route_cannot_escape_dist(client):
    assert client.get("/%2e%2e/%2e%2e/backend/config.py").status_code == 404


def test_desktop_session_last_for_the_process_lifetime(client, monkeypatch):
    from types import SimpleNamespace
    from backend.api import auth

    monkeypatch.setattr(config, "DESKTOP_MODE", True)
    monkeypatch.setattr(config, "DESKTOP_TOKEN", "lifetime-token")
    monkeypatch.setattr(auth, "time", SimpleNamespace(time=lambda: 1000000))
    result = client.get("/api/auth/desktop?token=lifetime-token", follow_redirects=False)
    assert "max-age" not in result.headers["set-cookie"].lower()
    monkeypatch.setattr(auth, "time", SimpleNamespace(time=lambda: 1000000 + 2 * auth.SESSION_SECONDS))
    assert client.get("/api/settings").status_code == 200
