import os
import datetime
import random
import re
import time
import uuid

from fastapi.testclient import TestClient
import pytest

from app import db
from app import main as main_module
from app.main import app, build_timeline


def _seed_domain():
    did = db.create_domain(f"ui-{uuid.uuid4().hex[:8]}.example", "A",
                           "1.1.1.1", 300, "测试")
    now = int(time.time())
    c = db.conn()
    for i in range(48):
        status = "timeout" if i in (0, 7) else "ok"
        c.execute(
            "INSERT INTO checks (domain_id, ts, status, ip, rtt_ms, error) "
            "VALUES (?,?,?,?,?,?)",
            (did, now - i * 1800, status, "203.0.113.7" if status == "ok" else None,
             18 + i % 9, None if status == "ok" else "解析超时"),
        )
    ips = ["203.0.113.7", "198.51.100.9", "203.0.113.7", "192.0.2.5"]
    for i in range(40):
        c.execute(
            "INSERT INTO changes (domain_id, ts, old_ip, new_ip, notified) "
            "VALUES (?,?,?,?,1)",
            (did, now - i * 3600 * 6,
             None if i == 39 else ips[i % 4], ips[(i + 1) % 4]),
        )
    c.execute(
        "INSERT INTO changes (domain_id, ts, old_ip, new_ip, notified) VALUES (?,?,?,?,1)",
        (did, now - 5 * 86400, None, None),
    )
    c.commit()
    return did


def _count(html, needle):
    return len(re.findall(needle, html))


def test_pages_have_single_h1_title_and_skip_link(auth_client):
    did = _seed_domain()
    for url in ["/", f"/domain/{did}", "/settings", "/domain/new"]:
        html = auth_client.get(url).text
        assert _count(html, r"<h1\b") == 1, url
        assert _count(html, r"<title>") == 1, url
        assert 'lang="zh-CN"' in html
        assert 'class="skip-link"' in html
        assert 'aria-current="page"' in html


def test_homepage_abnormal_row_keeps_latest_check_error(auth_client):
    marker = f"unique-error-{uuid.uuid4().hex}"
    did = db.create_domain(f"error-{uuid.uuid4().hex}.example", "A",
                           "1.1.1.1", 300, "test")
    db.conn().execute(
        "INSERT INTO checks (domain_id, ts, status, error) VALUES (?, ?, ?, ?)",
        (did, int(time.time()), "timeout", marker),
    )
    db.conn().commit()

    response = auth_client.get("/")
    assert response.status_code == 200
    assert "需要处理" in response.text
    assert marker in response.text


def test_hourly_chart_matches_python_aggregation_for_random_checks(tmp_path, monkeypatch):
    monkeypatch.setenv("DDNS_DATA_DIR", str(tmp_path / "private-data"))
    import importlib.util
    db_spec = importlib.util.spec_from_file_location("hourly_isolated_db", db.__file__)
    isolated_db = importlib.util.module_from_spec(db_spec)
    db_spec.loader.exec_module(isolated_db)
    isolated_db.init()
    monkeypatch.setattr(main_module, "db", isolated_db)
    did = isolated_db.create_domain(f"hourly-{uuid.uuid4().hex}.example", "A",
                                    "1.1.1.1", 300, "test")
    h0 = datetime.datetime.now().replace(minute=0, second=0, microsecond=0) \
        - datetime.timedelta(hours=23)
    start = int(h0.timestamp())
    rng = random.Random(20261113)
    sample_span = min(24 * 3600, int(time.time()) - start + 1)
    samples = [(start + rng.randrange(sample_span),
                "timeout" if rng.randrange(5) == 0 else "ok") for _ in range(600)]
    for ts, status in samples:
        isolated_db.add_check(did, status, None, None, ts=ts)

    expected = [{"checks": 0, "changes": 0, "fail": 0} for _ in range(24)]
    for ts, status in samples:
        index = int((datetime.datetime.fromtimestamp(ts) - h0).total_seconds() // 3600)
        if 0 <= index < 24:
            expected[index]["checks"] += 1
            expected[index]["fail"] += int(status != "ok")
    for change in isolated_db.conn().execute(
            "SELECT ts FROM changes WHERE ts >= ? AND old_ip IS NOT NULL", (start,)):
        index = int((datetime.datetime.fromtimestamp(change["ts"]) - h0).total_seconds() // 3600)
        if 0 <= index < 24:
            expected[index]["changes"] += 1
    actual = main_module._build_chart()["rows"]
    assert [{key: row[key] for key in ("checks", "changes", "fail")} for row in actual] == expected
    isolated_db.conn().close()


@pytest.mark.parametrize("tz_name", ["Asia/Shanghai", "Asia/Kolkata"])
def test_timezone_hourly_chart_and_today_stats_match_details(tmp_path, monkeypatch, tz_name):
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = tz_name
    time.tzset()
    isolated_db = None
    try:
        import importlib.util
        db_spec = importlib.util.spec_from_file_location("kolkata_isolated_db", db.__file__)
        isolated_db = importlib.util.module_from_spec(db_spec)
        db_spec.loader.exec_module(isolated_db)
        isolated_db.init()
        monkeypatch.setattr(main_module, "db", isolated_db)
        did = isolated_db.create_domain(f"kolkata-{uuid.uuid4().hex}.example", "A",
                                        "1.1.1.1", 300, "test")
        h0 = datetime.datetime.now().replace(minute=0, second=0, microsecond=0) \
            - datetime.timedelta(hours=23)
        start = int(h0.timestamp())
        rng = random.Random(530)
        sample_span = min(24 * 3600, int(time.time()) - start + 1)
        samples = [(start + rng.randrange(sample_span),
                    "timeout" if rng.randrange(4) == 0 else "ok") for _ in range(1200)]
        for ts, status in samples:
            isolated_db.add_check(did, status, None, None, ts=ts)

        expected = [0] * 24
        for ts, _ in samples:
            index = int((datetime.datetime.fromtimestamp(ts) - h0).total_seconds() // 3600)
            if 0 <= index < 24:
                expected[index] += 1
        actual = [row["checks"] for row in main_module._build_chart()["rows"]]
        assert actual == expected
        assert sum(actual) == 1200

        today_start = int(datetime.datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp())
        stats = isolated_db.check_stats_since(today_start)
        details = isolated_db.conn().execute(
            "SELECT COUNT(*) AS total, SUM(status = 'ok') AS ok, "
            "SUM(status != 'ok') AS fail FROM checks WHERE ts >= ?", (today_start,),
        ).fetchone()
        assert tuple(stats) == tuple(details)
    finally:
        if isolated_db is not None and isolated_db._conn is not None:
            isolated_db._conn.close()
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()


def test_chart_is_keyboard_focusable_with_text_alternative(auth_client):
    _seed_domain()
    html = auth_client.get("/").text
    assert 'id="chart-svg"' in html
    assert 'role="group"' in html
    assert 'role="img"' not in html
    assert re.search(r'id="chart-svg"[^>]*tabindex="0"', html)
    assert _count(html, r'class="chart-hit"') == 24
    assert _count(html, r'class="chart-cursor"') == 1
    assert "查看逐小时数据表" in html


def test_timeline_has_ticks_text_equivalent_and_disabled_pager(auth_client):
    did = _seed_domain()
    html = auth_client.get(f"/domain/{did}").text
    assert _count(html, r'class="tl-tick[ \"]') == 5
    assert _count(html, r'class="tl-seg ') <= 240
    assert 'id="tl-track"' in html and 'tabindex="0"' in html
    assert "时间线明细" in html
    assert "基线重置" in html
    assert 'aria-disabled="true"' in html


def test_delete_confirmation_has_no_inline_string_building(auth_client):
    did = _seed_domain()
    html = auth_client.get(f"/domain/{did}").text
    assert "onsubmit" not in html
    assert "confirm(" not in html
    assert "确认删除" in html


def test_token_is_never_echoed_and_blank_keeps_old_value(auth_client):
    db.set_setting("tg_token", "SECRET-TOKEN-XYZ")
    html = auth_client.get("/settings").text
    assert "SECRET-TOKEN-XYZ" not in html
    assert 'id="tg_token"' in html
    resp = auth_client.post("/settings/notify", data={
        "tg_enabled": "on", "notify_on_change": "on", "notify_on_fail": "on",
        "tg_token": "", "tg_chat": "12345", "fail_cooldown_min": "60",
        "change_cooldown_min": "0",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert db.get_setting("tg_token") == "SECRET-TOKEN-XYZ"


def test_flash_cookie_is_ascii_and_consumed_once(auth_client):
    resp = auth_client.post("/settings/notify", data={
        "tg_chat": "1", "fail_cooldown_min": "60", "change_cooldown_min": "0",
    }, follow_redirects=False)
    cookie = resp.headers.get("set-cookie", "")
    assert "ddns_flash=" in cookie
    cookie.encode("ascii")  # 中文提示不能进 Set-Cookie
    html = auth_client.get("/settings").text
    assert "通知设置已保存" in html and 'role="status"' in html
    again = auth_client.get("/settings").text
    assert "通知设置已保存" not in again


def test_create_validation_keeps_draft(auth_client):
    resp = auth_client.post("/domain/new", data={
        "name": "bad name", "rrtype": "A", "resolver": "1.1.1.1",
        "interval_sec": "300", "note": "保留我",
    }, follow_redirects=False)
    assert resp.status_code == 400
    assert "域名格式不正确" in resp.text
    assert 'value="bad name"' in resp.text
    assert "保留我" in resp.text


def test_forms_have_labels_and_password_autocomplete():
    fresh = TestClient(app)
    html = fresh.get("/login").text
    assert "autocomplete=\"current-password\"" in html
    assert "autofocus" not in html
    assert "登录 DDNS 监控" in html

    ids = re.findall(r'<input[^>]*\bid="([^"]+)"', html)
    for input_id in ids:
        assert f'for="{input_id}"' in html, input_id


def test_settings_interval_options_and_selected_value(auth_client):
    did = _seed_domain()
    html = auth_client.get(f"/domain/{did}").text
    assert _count(html, r'<option value="(?:60|300|600|1800|3600|7200|21600|86400)"') == 8
    assert re.search(r'<option value="300"\s+selected>', html)


def test_public_timeline_contract():
    did = _seed_domain()
    now = int(time.time())
    tl = build_timeline(did, 30, now)
    assert {"days", "start", "end", "window", "bucket_seconds", "segments",
            "legend", "ticks", "counts", "sampled"} <= set(tl)
    assert tl["window"] == {"start": tl["start"], "end": tl["end"]}
    assert len(tl["ticks"]) == 5 and all("ts" in t for t in tl["ticks"])
    assert all({"start", "end", "color_index"} <= set(s) for s in tl["segments"])
    assert all(l["exact"] is True and "color_index" in l for l in tl["legend"])
    assert "window_changes" in tl["counts"]


def test_all_post_forms_announce_pending(auth_client):
    did = _seed_domain()
    for url in ["/", f"/domain/{did}", "/settings", "/domain/new"]:
        html = auth_client.get(url).text
        forms = re.findall(r"<form\b[^>]*method=\"post\"[^>]*>", html)
        assert forms, url
        for form in forms:
            assert "data-pending=" in form, (url, form)


def test_mobile_input_attributes_and_first_screen_order(auth_client):
    _seed_domain()
    new_html = auth_client.get("/domain/new").text
    assert 'autocorrect="off"' in new_html
    assert 'inputmode="url"' in new_html
    index = auth_client.get("/").text
    assert index.index('id="domains"') < index.index('class="card chart-card"')
    assert "立即检查" in index


def test_settings_data_field_error(auth_client):
    resp = auth_client.post("/settings/data", data={"retention_days": "999"},
                            follow_redirects=False)
    assert resp.status_code == 400
    assert 'aria-invalid="true"' in resp.text
    assert "1–365" in resp.text
    assert "保存数据保留设置" in resp.text


def test_password_field_error_is_next_to_field(auth_client):
    resp = auth_client.post("/settings/password", data={
        "password": "123", "password2": "123"}, follow_redirects=False)
    assert resp.status_code == 400
    assert 'id="password-error"' in resp.text
    assert 'aria-invalid="true"' in resp.text
    assert "密码至少 8 位" in resp.text


def test_login_error_marks_password_invalid():
    fresh = TestClient(app)
    resp = fresh.post("/login", data={"password": "wrong-password"},
                      follow_redirects=False)
    assert resp.status_code == 400
    assert 'id="login-error"' in resp.text
    assert re.search(r'<input[^>]*id="password"[^>]*aria-invalid="true"', resp.text, re.S)
