import re
import time

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def ui_client(private_auth_db, monkeypatch):
    client = TestClient(main.app, base_url="https://testserver")
    exp = int(time.time()) + 3600
    client.cookies.set(main.COOKIE, f"{exp}.{main._sign(exp)}",
                       domain="testserver.local", path="/")
    client.get("/settings")
    monkeypatch.setattr(main.monitor, "poke", lambda: None)
    return client


def _post(client, path, data):
    return client.post(path, data={"_csrf": client.cookies.get("ddns_csrf"), **data},
                       follow_redirects=False)


def _notify(**overrides):
    return {"tg_enabled": "on", "tg_token": "", "tg_chat": "12345",
            "notify_on_change": "on", "notify_on_fail": "on",
            "fail_cooldown_min": "60", "change_cooldown_min": "0", **overrides}


def _edit(**overrides):
    return {"rrtype": "A", "resolver": "1.1.1.1", "interval_sec": "600",
            "note": "保留本次输入", "enabled": "on", "days": "90", **overrides}


@pytest.mark.parametrize("path", ["/settings/notify", "/settings/test_tg"])
@pytest.mark.parametrize("missing", ["tg_token", "tg_chat"])
def test_missing_notify_credentials_do_not_save_or_send(
        ui_client, private_auth_db, monkeypatch, path, missing):
    private_auth_db.set_settings({"tg_enabled": "0", "tg_token": "saved-token",
                                 "tg_chat": "old-chat"})
    if missing == "tg_token":
        private_auth_db.set_setting("tg_token", "")
    before = list(private_auth_db.conn().execute("SELECT * FROM settings ORDER BY key"))
    monkeypatch.setattr(main, "send_tg", lambda *args: pytest.fail("不得发送消息"))
    response = _post(ui_client, path, _notify(**{missing: ""}))
    assert response.status_code == 400
    assert 'data-error-summary' in response.text
    assert re.search(rf'id="{missing}"[^>]*aria-invalid="true"', response.text)
    assert [tuple(r) for r in private_auth_db.conn().execute("SELECT * FROM settings ORDER BY key")] == [tuple(r) for r in before]
    assert 'class="nav-item" href="/settings" aria-current="page"' in response.text


@pytest.mark.parametrize("enabled", ["", "on"])
def test_clear_token_and_test_rejects_without_writing(
        ui_client, private_auth_db, monkeypatch, enabled):
    private_auth_db.set_settings({"tg_enabled": "1", "tg_token": "saved-token",
                                 "tg_chat": "old-chat"})
    monkeypatch.setattr(main, "send_tg", lambda *args: pytest.fail("不得使用未保存 Token"))
    response = _post(ui_client, "/settings/test_tg", _notify(
        tg_enabled=enabled, tg_token="replacement-token", clear_tg_token="on"))
    assert response.status_code == 400
    assert private_auth_db.get_setting("tg_token") == "saved-token"
    assert private_auth_db.get_setting("tg_chat") == "old-chat"
    assert "replacement-token" not in response.text
    assert "saved-token" not in response.text


def test_cannot_clear_token_while_enabled(ui_client, private_auth_db):
    private_auth_db.set_settings({"tg_enabled": "1", "tg_token": "saved-token"})
    response = _post(ui_client, "/settings/notify", _notify(clear_tg_token="on"))
    assert response.status_code == 400
    assert private_auth_db.get_setting("tg_token") == "saved-token"
    assert private_auth_db.get_setting("tg_enabled") == "1"


@pytest.mark.parametrize("submitted", ["", "new-token"])
def test_notification_test_uses_the_saved_effective_config(
        ui_client, private_auth_db, monkeypatch, submitted):
    private_auth_db.set_setting("tg_token", "saved-token")
    calls = []

    async def send(token, chat, message):
        calls.append((token, chat))
        assert token == private_auth_db.get_setting("tg_token")
        assert chat == private_auth_db.get_setting("tg_chat")
        return True, "模拟发送"

    monkeypatch.setattr(main, "send_tg", send)
    response = _post(ui_client, "/settings/test_tg", _notify(tg_token=submitted))
    assert response.status_code == 303
    assert calls == [(submitted or "saved-token", "12345")]


def test_disabled_notify_allows_incomplete_draft(ui_client, private_auth_db):
    response = _post(ui_client, "/settings/notify", _notify(
        tg_enabled="", tg_token="", tg_chat=""))
    assert response.status_code == 303
    assert private_auth_db.get_setting("tg_enabled") == "0"


def test_legacy_incomplete_notify_config_is_not_shown_as_ready(ui_client, private_auth_db):
    private_auth_db.set_settings({"tg_enabled": "1", "tg_token": "", "tg_chat": ""})
    response = ui_client.get("/settings")
    assert "配置不完整" in response.text
    assert '<span class="badge ok">已启用</span>' not in response.text


@pytest.mark.parametrize("resolver", ["1.1.1.999", "dns.example.com", "", "2001:db8::gg"])
@pytest.mark.parametrize("create", [True, False])
def test_invalid_resolver_keeps_draft_without_any_write(
        ui_client, private_auth_db, monkeypatch, resolver, create):
    did = private_auth_db.create_domain("dns-audit.example", "A", "8.8.8.8", 300, "旧备注")
    before = dict(private_auth_db.get_domain(did))
    monkeypatch.setattr(main.monitor, "run_check", lambda *args: pytest.fail("不得查 DNS"))
    path = "/domain/new" if create else f"/domain/{did}/update"
    response = _post(ui_client, path, _edit(name="new-dns-audit.example", resolver=resolver))
    assert response.status_code == 400
    assert re.search(r'id="resolver"[^>]*aria-invalid="true"', response.text)
    assert re.search(rf'id="resolver"[^>]*value="{re.escape(resolver)}"', response.text)
    assert 'value="保留本次输入"' in response.text
    assert private_auth_db.get_domain_by_name("new-dns-audit.example", "A") is None
    assert dict(private_auth_db.get_domain(did)) == before


@pytest.mark.parametrize("resolver", [" 1.1.1.1 ", " 2606:4700:4700::1111 "])
def test_valid_resolvers_are_saved_explicitly(ui_client, private_auth_db, resolver):
    did = private_auth_db.create_domain("valid-dns.example", "A", "8.8.8.8", 300, "")
    response = _post(ui_client, f"/domain/{did}/update", _edit(resolver=resolver))
    assert response.status_code == 303
    assert private_auth_db.get_domain(did)["resolver"] == resolver.strip()
    assert "days=90" in response.headers["location"]


@pytest.mark.parametrize("race", [False, True])
def test_type_conflict_preserves_draft_and_actual_status(
        ui_client, private_auth_db, monkeypatch, race):
    did = private_auth_db.create_domain("conflict.example", "A", "8.8.8.8", 300, "旧备注")
    private_auth_db.create_domain("conflict.example", "AAAA", "8.8.8.8", 300, "")
    before = dict(private_auth_db.get_domain(did))
    if race:
        # 模拟前置查询后才出现的冲突，最终由数据库唯一约束拒绝。
        monkeypatch.setattr(private_auth_db, "get_domain_by_name", lambda *args: None)
    response = _post(ui_client, f"/domain/{did}/update", _edit(
        rrtype="AAAA", resolver="2606:4700:4700::1111", enabled=""))
    assert response.status_code == 400
    assert re.search(r'id="rrtype"[^>]*aria-invalid="true"', response.text)
    assert '<option value="AAAA" selected>' in response.text
    assert '<option value="600" selected>' in response.text
    assert 'value="保留本次输入"' in response.text
    assert 'value="2606:4700:4700::1111"' in response.text
    assert re.search(r'id="enabled"[^>]*checked', response.text) is None
    assert 'name="days" value="90"' in response.text
    assert '<span class="badge type">A</span>' in response.text
    assert dict(private_auth_db.get_domain(did)) == before
    assert private_auth_db.change_stats(did)["rows"] == 0


def test_history_links_preserve_range_and_position(ui_client, private_auth_db):
    did = private_auth_db.create_domain("pages.example", "A", "1.1.1.1", 300, "")
    c = private_auth_db.conn()
    now = int(time.time())
    c.executemany("INSERT INTO changes (domain_id, ts, old_ip, new_ip) VALUES (?, ?, ?, ?)",
                  [(did, now - i, "203.0.113.1", "203.0.113.2") for i in range(65)])
    c.commit()
    first = ui_client.get(f"/domain/{did}?days=90")
    older = re.search(r'href="([^\"]*before=[^\"]+)"', first.text).group(1)
    assert older.endswith("#changes") and "days=90" in older
    second = ui_client.get(f"/domain/{did}" + older.replace("&amp;", "&"))
    newer = re.search(r'href="([^\"]*after=[^\"]+)"', second.text).group(1)
    assert newer.endswith("#changes") and "days=90" in newer


@pytest.mark.parametrize("path, data, target", [
    ("/settings/password", {"current_password": "old", "password": "long-password-1",
                            "password2": "long-password-2"}, "password2"),
    ("/settings/data", {"retention_days": "0"}, "retention_days"),
])
def test_settings_errors_have_focus_target_and_current_navigation(ui_client, path, data, target):
    response = _post(ui_client, path, data)
    assert response.status_code == 400
    assert 'data-error-summary' in response.text and 'tabindex="-1"' in response.text
    assert f'href="#{target}"' in response.text
    assert 'class="nav-item" href="/settings" aria-current="page"' in response.text
    assert re.search(rf'id="{target}"[^>]*aria-invalid="true"', response.text)
