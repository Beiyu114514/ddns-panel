import asyncio
import ipaddress
import re
import sqlite3
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.middleware import CSRF_COOKIE


CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'; object-src 'none'"
)
HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "permissions-policy": "geolocation=(), microphone=(), camera=()",
    "content-security-policy": CSP,
    "strict-transport-security": "max-age=31536000; includeSubDomains",
}


@pytest.fixture(autouse=True)
def isolated_security_state(monkeypatch):
    monkeypatch.setattr(main, "LOGIN_FAILS", {})
    monkeypatch.setattr(main, "SCRYPT_SEMAPHORE", asyncio.Semaphore(4))


def _signed_client(*, raise_server_exceptions=True):
    client = TestClient(main.app, base_url="https://testserver",
                        raise_server_exceptions=raise_server_exceptions)
    exp = int(time.time()) + 3600
    client.cookies.set(main.COOKIE, f"{exp}.{main._sign(exp)}", domain="testserver.local", path="/")
    client.get("/settings")
    return client


def _initial_password(database):
    return (Path(database.DATA_DIR) / "initial_password.txt").read_text().strip()


def _snapshot(database):
    return {table: [tuple(row) for row in database.conn().execute(f"SELECT * FROM {table}")]
            for table in ("settings", "domains", "checks", "changes", "check_hourly")}


def _must_not_run(*args, **kwargs):
    pytest.fail("拒绝的请求不能写库、执行 scrypt、查 DNS 或发通知")


@pytest.mark.parametrize("url, status, html", [
    ("/login", 200, True), ("/healthz", 200, False), ("/settings", 303, False),
    ("/static/style.css", 200, False), ("/static/missing.js", 404, False),
])
def test_security_headers_cover_pages_redirects_and_static(url, status, html):
    client = TestClient(main.app, base_url="https://testserver")
    response = client.get(url, follow_redirects=False)
    assert response.status_code == status
    for name, value in HEADERS.items():
        assert response.headers[name] == value
    assert (response.headers.get("cache-control") == "no-store") is (not url.startswith("/static/"))
    assert (CSRF_COOKIE in response.cookies) is html


def test_healthz_is_public_and_only_exposes_status():
    client = TestClient(main.app, base_url="https://testserver")
    response = client.get("/healthz", follow_redirects=False)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "location" not in response.headers
    assert not response.cookies


@pytest.mark.parametrize("failure_at", ["connect", "execute", "fetchone"])
def test_healthz_database_errors_return_503_with_security_headers(monkeypatch, failure_at):
    def broken(*args):
        raise sqlite3.OperationalError("private /srv/data/ddns.db details")

    class Connection:
        def execute(self, sql):
            assert sql == "SELECT 1"
            if failure_at == "execute":
                broken()
            return self

        def fetchone(self):
            broken()

    monkeypatch.setattr(main.db, "conn", broken if failure_at == "connect" else Connection)
    client = TestClient(main.app, base_url="https://testserver")
    response = client.get("/healthz", follow_redirects=False)
    assert response.status_code == 503
    assert response.json() == {"status": "error"}
    assert not response.cookies
    for name, value in HEADERS.items():
        assert response.headers[name] == value


def test_healthz_is_absent_from_pages(auth_client):
    for url in ("/", "/settings", "/domain/new"):
        response = auth_client.get(url)
        assert response.status_code == 200
        assert "/healthz" not in response.text
    client = TestClient(main.app, base_url="https://testserver")
    assert "/healthz" not in client.get("/login").text


@pytest.mark.parametrize("status", [400, 403, 404, 422, 500])
def test_security_headers_cover_authenticated_errors(private_auth_db, monkeypatch, status):
    client = _signed_client(raise_server_exceptions=False)
    if status == 500:
        def broken_context():
            raise RuntimeError("模拟服务端错误")
        monkeypatch.setattr(main, "_settings_ctx", broken_context)
        response = client.get("/settings")
    elif status == 400:
        response = client.post("/settings/data", data={
            "_csrf": client.cookies.get(CSRF_COOKIE), "retention_days": "999"})
    elif status == 403:
        response = client.post("/settings/data", data={"retention_days": "30"})
    elif status == 422:
        response = client.get("/domain/1?days=invalid")
    else:
        response = client.get("/missing-page")
    assert response.status_code == status
    for name, value in HEADERS.items():
        assert response.headers[name] == value
    if status in (400, 403):
        assert response.headers["cache-control"] == "no-store"
        assert "text/html" in response.headers["content-type"]


@pytest.mark.parametrize("peer, hops, xff, proto, expected", [
    ("203.0.113.1", 1, "", "https", False),
    ("127.0.0.1", 1, "", "https", False),
    ("172.17.0.1", 1, "", "https", True),
    ("::ffff:172.17.0.1", 1, "203.0.113.1", "https", True),
    ("172.17.0.1", 1, "203.0.113.1", "http", False),
    ("172.17.0.1", 1, "203.0.113.1", "https, http", False),
    ("172.17.0.1", 1, "garbage", "https", False),
    ("172.17.0.1", 2, "203.0.113.1", "https", False),
    ("172.17.0.1", 2, "203.0.113.1, 9.9.9.9", "https", False),
    ("172.17.0.1", 2, "203.0.113.1, 104.16.0.1", "https", True),
    ("172.17.0.1", 2, "203.0.113.1, 172.18.0.1", "https, http", True),
    ("172.17.0.1", 2, "203.0.113.1, 172.18.0.1", "https, http, http", False),
    ("172.17.0.1", 2, "203.0.113.1, 172.18.0.1", "garbage, https", False),
    ("172.17.0.1", 1, "", "", False),
])
def test_hsts_only_trusts_valid_https_proxy_chains(monkeypatch, peer, hops, xff, proto, expected):
    monkeypatch.setattr(main, "TRUSTED_PROXIES", [ipaddress.ip_network("172.16.0.0/12")])
    monkeypatch.setattr(main, "TRUSTED_PROXY_HOPS", hops)

    async def request():
        transport = httpx.ASGITransport(app=main.app, client=(peer, 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/login", headers={
                "X-Forwarded-For": xff, "X-Forwarded-Proto": proto})

    response = asyncio.run(request())
    assert ("strict-transport-security" in response.headers) is expected


@pytest.mark.parametrize("secure", [True, False])
def test_csrf_cookie_flags_reuse_and_forms(private_auth_db, monkeypatch, secure):
    monkeypatch.setattr(main, "COOKIE_SECURE", secure)
    client = TestClient(main.app, base_url="https://testserver")
    login = client.get("/login")
    cookie = next(c for c in login.headers.get_list("set-cookie") if c.startswith(CSRF_COOKIE + "="))
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
    assert ("Secure" in cookie) is secure
    token = client.cookies.get(CSRF_COOKIE)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
    assert client.post("/login", data={"password": _initial_password(private_auth_db)},
                       follow_redirects=False).status_code == 303
    did = private_auth_db.create_domain("csrf.example", "A", "1.1.1.1", 300, "")
    for url in ("/", "/settings", "/domain/new", f"/domain/{did}"):
        response = client.get(url)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert not any(c.startswith(CSRF_COOKIE + "=") for c in response.headers.get_list("set-cookie"))
        forms = re.findall(r'<form\b[^>]*method="post"[^>]*>(.*?)</form>', response.text, re.S)
        assert forms
        for form in forms:
            assert f'name="_csrf" value="{token}"' in form
        assert not re.search(r'<script(?![^>]*\bsrc=)', response.text)
        assert '<script src="/static/app.js" defer></script>' in response.text
    assert '<script src="/static/chart.js" defer></script>' in client.get("/").text
    assert '<script src="/static/timeline.js" defer></script>' in client.get(f"/domain/{did}").text
    for name in ("app", "chart", "timeline"):
        assert client.get(f"/static/{name}.js").status_code == 200


POSTS = ["/logout", "/domain/new", "/domain/{did}/update", "/domain/{did}/delete",
         "/domain/{did}/check", "/settings/password", "/settings/notify",
         "/settings/test_tg", "/settings/data"]


def _form(database):
    return {"name": "new.example", "rrtype": "A", "resolver": "1.1.1.1",
            "interval_sec": "300", "note": "new note", "enabled": "on",
            "current_password": _initial_password(database),
            "password": "new-password-123", "password2": "new-password-123",
            "retention_days": "17", "tg_token": "test-token", "tg_chat": "123",
            "fail_cooldown_min": "60", "change_cooldown_min": "0"}


@pytest.mark.parametrize("url", POSTS)
@pytest.mark.parametrize("mode", ["missing", "wrong", "unicode", "no_cookie"])
def test_invalid_csrf_has_no_side_effects(private_auth_db, monkeypatch, url, mode):
    did = private_auth_db.create_domain("existing.example", "A", "1.1.1.1", 300, "original")
    client = _signed_client()
    data = _form(private_auth_db)
    if mode != "missing":
        data["_csrf"] = {"wrong": "wrong-token", "unicode": "错误令牌", "no_cookie": client.cookies.get(CSRF_COOKIE)}[mode]
    if mode == "no_cookie":
        client.cookies.delete(CSRF_COOKIE)
    before = _snapshot(private_auth_db)
    for name in ("set_settings", "set_setting", "delete_domain", "update_domain", "create_domain",
                 "verify_password", "hash_password"):
        monkeypatch.setattr(private_auth_db, name, _must_not_run)
    monkeypatch.setattr(main.monitor, "run_check", _must_not_run)
    monkeypatch.setattr(main.monitor, "poke", _must_not_run)
    monkeypatch.setattr(main, "send_tg", _must_not_run)
    response = client.post(url.format(did=did), data=data, follow_redirects=False)
    assert response.status_code == 403
    assert "text/html" in response.headers["content-type"]
    assert "表单安全令牌" in response.text
    assert _snapshot(private_auth_db) == before
    assert (Path(private_auth_db.DATA_DIR) / "initial_password.txt").exists()
    assert client.cookies.get(main.COOKIE)


@pytest.mark.parametrize("url", POSTS)
def test_valid_csrf_allows_post(private_auth_db, monkeypatch, url):
    did = private_auth_db.create_domain("existing.example", "A", "1.1.1.1", 300, "original")
    client = _signed_client()
    data = {**_form(private_auth_db), "_csrf": client.cookies.get(CSRF_COOKIE)}
    checks, notifications = [], []

    async def run_check(domain_id):
        checks.append(domain_id)
        return {"status": "ok", "ip": "203.0.113.1", "rtt": 10}

    async def send_tg(*args):
        notifications.append(args)
        return True, "ok"

    monkeypatch.setattr(main.monitor, "run_check", run_check)
    monkeypatch.setattr(main.monitor, "poke", lambda: None)
    monkeypatch.setattr(main, "send_tg", send_tg)
    response = client.post(url.format(did=did), data=data, follow_redirects=False)
    assert response.status_code == 303
    if url == "/domain/new":
        assert private_auth_db.get_domain_by_name("new.example", "A")
        assert len(checks) == 1
    elif url.endswith("/delete"):
        assert private_auth_db.get_domain(did) is None
    elif url.endswith("/update"):
        assert private_auth_db.get_domain(did)["note"] == "new note"
    elif url.endswith("/check"):
        assert checks == [did]
    elif url == "/settings/data":
        assert private_auth_db.get_setting("retention_days") == "17"
    elif url in ("/settings/notify", "/settings/test_tg"):
        assert private_auth_db.get_setting("tg_chat") == "123"
        assert bool(notifications) is url.endswith("test_tg")
    elif url == "/logout":
        assert client.cookies.get(main.COOKIE) is None
    else:
        assert private_auth_db.verify_password("new-password-123", private_auth_db.get_setting("password_hash"))


@pytest.mark.parametrize("current", ["wrong-current-password", "", "x" * 1025])
def test_wrong_current_password_keeps_settings_and_login(private_auth_db, monkeypatch, current):
    client = _signed_client()
    old_password = _initial_password(private_auth_db)
    before = _snapshot(private_auth_db)
    monkeypatch.setattr(private_auth_db, "hash_password", _must_not_run)
    response = client.post("/settings/password", data={
        "_csrf": client.cookies.get(CSRF_COOKIE), "current_password": current,
        "password": "new-password-123", "password2": "new-password-123"})
    assert response.status_code == 400
    assert re.search(r'<input[^>]*id="current_password"[^>]*aria-invalid="true"', response.text, re.S)
    assert 'id="current-password-error"' in response.text
    assert _snapshot(private_auth_db) == before
    assert _initial_password(private_auth_db) == old_password
    assert client.post("/login", data={"password": old_password}, follow_redirects=False).status_code == 303


def test_correct_current_password_revokes_other_sessions(private_auth_db):
    old_password = _initial_password(private_auth_db)
    client, other = _signed_client(), _signed_client()
    old_secret = private_auth_db.get_setting("auth_secret")
    response = client.post("/settings/password", data={
        "_csrf": client.cookies.get(CSRF_COOKIE), "current_password": old_password,
        "password": "new-password-123", "password2": "new-password-123"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert private_auth_db.get_setting("auth_secret") != old_secret
    assert not (Path(private_auth_db.DATA_DIR) / "initial_password.txt").exists()
    for old_client in (client, other):
        assert old_client.get("/settings", follow_redirects=False).headers["location"] == "/login"
    assert other.post("/login", data={"password": old_password}, follow_redirects=False).status_code == 400
    assert other.post("/login", data={"password": "new-password-123"}, follow_redirects=False).status_code == 303


def test_current_password_verification_uses_global_scrypt_slots(private_auth_db, monkeypatch):
    client = _signed_client()
    monkeypatch.setattr(main, "SCRYPT_SEMAPHORE", asyncio.Semaphore(0))
    monkeypatch.setattr(private_auth_db, "verify_password", _must_not_run)
    before = _snapshot(private_auth_db)
    response = client.post("/settings/password", data={
        "_csrf": client.cookies.get(CSRF_COOKIE), "current_password": _initial_password(private_auth_db),
        "password": "new-password-123", "password2": "new-password-123"})
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    assert _snapshot(private_auth_db) == before
