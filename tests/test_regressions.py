import asyncio
import os
import time
import uuid

import pytest
from starlette.requests import Request

from app import db
from app.main import build_timeline
from app.monitor import Monitor


def _domain(name=None, interval=300):
    return db.create_domain(name or f"r-{uuid.uuid4().hex[:8]}.example", "A",
                            "1.1.1.1", interval, "test")


def _change(did, ts, old, new, notified=1):
    cur = db.conn().execute(
        "INSERT INTO changes (domain_id, ts, old_ip, new_ip, notified) VALUES (?,?,?,?,?)",
        (did, ts, old, new, notified),
    )
    db.conn().commit()
    return cur.lastrowid


def _request(peer, xff):
    return Request({
        "type": "http", "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": "/login", "raw_path": b"/login",
        "query_string": b"", "headers": [(b"x-forwarded-for", xff.encode())],
        "client": (peer, 12345), "server": ("testserver", 80),
    })


@pytest.fixture
def tg_settings():
    keys = ["tg_enabled", "notify_on_change", "notify_on_fail", "tg_token",
            "tg_chat", "fail_cooldown_min", "change_cooldown_min"]
    old = {k: db.get_setting(k) for k in keys}
    yield
    c = db.conn()
    for key, value in old.items():
        if value is None:
            c.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            c.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
    c.commit()


def test_chart_series_classes_match_stylesheet(auth_client):
    did = _domain()
    _change(did, int(time.time()) - 3600, None, "203.0.113.1")
    html = auth_client.get("/").text
    assert 'class="series changes"' in html
    assert 'class="series fail"' in html
    assert "s-changes" not in html and "s-fail" not in html


def test_client_ip_rejects_forged_short_chain(monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "TRUSTED_PROXY_HOPS", 2)
    assert main._client_ip(_request("172.17.0.1", "1.2.3.4, 9.9.9.9")) == "172.17.0.1"


def test_client_ip_accepts_trusted_and_cdn_hops(monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "TRUSTED_PROXY_HOPS", 2)
    assert main._client_ip(_request("172.17.0.1", "1.2.3.4, 172.18.0.2")) == "1.2.3.4"
    assert main._client_ip(_request("172.17.0.1", "1.2.3.4, 104.16.0.1")) == "1.2.3.4"


def test_session_cookie_is_secure(client):
    path = os.path.join(db.DATA_DIR, "initial_password.txt")
    with open(path, encoding="utf-8") as f:
        password = f.read().strip()
    resp = client.post("/login", data={"password": password}, follow_redirects=False)
    assert resp.status_code == 303
    cookies = [c for c in resp.headers.get_list("set-cookie")
               if c.startswith("ddns_session=")]
    assert cookies and "Secure" in cookies[0]


def test_flash_set_by_handler_survives_middleware(auth_client):
    data = {"tg_chat": "1", "fail_cooldown_min": "60", "change_cooldown_min": "0"}
    first = auth_client.post("/settings/notify", data=data, follow_redirects=False)
    assert any(c.startswith("ddns_flash=")
               for c in first.headers.get_list("set-cookie"))
    second = auth_client.post("/settings/notify", data=data, follow_redirects=False)
    flash = [c for c in second.headers.get_list("set-cookie")
             if c.startswith("ddns_flash=")]
    assert len(flash) == 1 and "Max-Age=0" not in flash[0]
    assert "通知设置已保存" in auth_client.get("/settings").text


def test_custom_interval_renders_selected_option(auth_client):
    did = _domain(interval=900)
    html = auth_client.get(f"/domain/{did}").text
    assert '<option value="900" selected>' in html


def test_count_changes_window_excludes_first_record():
    did = _domain()
    now = int(time.time())
    _change(did, now - 100, None, "10.0.0.1")
    assert db.count_changes_window(did, now - 200) == 0
    _change(did, now - 50, "10.0.0.1", "10.0.0.2")
    assert db.count_changes_window(did, now - 200) == 1


def test_global_last_bad_check_ignores_disabled_domains():
    did = _domain()
    db.update_domain(did, enabled=0)
    db.conn().execute(
        "INSERT INTO checks (domain_id, ts, status, ip, rtt_ms, error) "
        "VALUES (?,?,?,?,?,?)",
        (did, int(time.time()) + 100000, "timeout", None, None, "解析超时"),
    )
    db.conn().commit()
    bad = db.global_last_bad_check()
    assert bad is None or bad["domain_id"] != did


def test_timeline_sampling_uses_drawable_rows():
    did = _domain()
    now = int(time.time())
    start = now - 30 * 86400
    _change(did, start, None, "10.0.0.1")
    for i in range(1, 241):
        _change(did, start + i * 60, f"10.0.0.{i}", f"10.0.0.{i + 1}")
    assert db.count_changes_window(did, start) == 240
    assert db.count_timeline_rows(did, start) == 241
    tl = build_timeline(did, 30, now)
    assert tl["sampled"] is True
    assert len(tl["segments"]) <= 240


def test_timeline_baseline_zero_width_at_window_start():
    did = _domain()
    now = int(time.time())
    start = now - 30 * 86400
    _change(did, start - 100, None, "10.0.0.1")
    _change(did, start, "10.0.0.1", "10.0.0.2")
    tl = build_timeline(did, 30, now)
    legend = {item["ip"]: item for item in tl["legend"]}
    assert "10.0.0.1" not in legend or legend["10.0.0.1"]["pct"] == 0
    assert sum(item["pct"] for item in tl["legend"]) <= 100


def test_change_notification_kept_when_credentials_missing(tg_settings):
    db.set_settings({"tg_enabled": "1", "notify_on_change": "1",
                     "tg_token": "", "tg_chat": "", "change_cooldown_min": "0"})
    did = _domain()
    _change(did, int(time.time()), "10.0.0.1", "10.0.0.2", notified=0)
    mon = Monitor()
    state, pending = {}, []
    d = {"id": did, "name": "x", "rrtype": "A"}
    mon._queue_change(d, state, int(time.time()), pending)
    assert pending == [] and db.pending_change(did) is not None

    db.set_settings({"tg_token": "token", "tg_chat": "chat"})
    mon._queue_change(d, state, int(time.time()), pending)
    assert [item[1] for item in pending] == ["change"]


def test_change_notification_not_duplicated_while_in_flight(tg_settings):
    db.set_settings({"tg_enabled": "1", "notify_on_change": "1",
                     "tg_token": "token", "tg_chat": "chat",
                     "change_cooldown_min": "0"})
    did = _domain()
    _change(did, int(time.time()), "10.0.0.1", "10.0.0.2", notified=0)
    mon = Monitor()
    state, pending = {}, []
    d = {"id": did, "name": "x", "rrtype": "A"}
    mon._queue_change(d, state, 1000, pending)
    mon._queue_change(d, state, 1001, pending)
    assert len(pending) == 1
    pending[0][3]()
    assert state["change_pending"] is False
    assert db.pending_change(did) is None


def test_alert_state_not_clobbered_by_stale_rollback(tg_settings):
    db.set_settings({"tg_enabled": "1", "notify_on_change": "0",
                     "notify_on_fail": "1", "tg_token": "token",
                     "tg_chat": "chat", "fail_cooldown_min": "60"})
    did = _domain()
    _change(did, int(time.time()) - 10, None, "10.0.0.1")
    mon = Monitor()
    d = {"id": did, "name": "x", "rrtype": "A"}
    fail = {"status": "timeout", "ip": None, "error": "解析超时"}
    ok = {"status": "ok", "ip": "10.0.0.1", "error": None}

    for _ in range(3):
        fail_pending = asyncio.run(mon._handle(d, fail))
    assert [item[1] for item in fail_pending] == ["fail"]
    state = mon.fail_state[did]
    assert state["alerted"] is True and state["alert_pending"] is True

    # 失败消息还在队列时恢复：不排恢复消息，也不清 alerted
    pending = asyncio.run(mon._handle(d, ok))
    assert all(item[1] != "recover" for item in pending)
    assert state["alerted"] is True

    # 失败消息发送失败：回滚后允许下次失败重新告警，且不产生无源恢复
    fail_pending[0][2]()
    assert state["alerted"] is False and state["alert_pending"] is False
    pending = asyncio.run(mon._handle(d, ok))
    assert all(item[1] != "recover" for item in pending)
