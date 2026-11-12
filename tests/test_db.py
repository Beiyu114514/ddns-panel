import importlib.util
import os
import stat
import time
import uuid

import pytest

from app import db


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    # 独立模块从临时环境变量读取路径，避免关闭会话级测试库的共享连接。
    monkeypatch.setenv("DDNS_DATA_DIR", str(tmp_path / "private-data"))
    spec = importlib.util.spec_from_file_location("isolated_db", db.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    if module._conn is not None:
        module._conn.close()


def _assert_private_modes(module):
    assert stat.S_IMODE(os.stat(module.DATA_DIR).st_mode) == 0o700
    for suffix in ("", "-wal", "-shm"):
        assert stat.S_IMODE(os.stat(module.DB_PATH + suffix).st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX 文件权限")
@pytest.mark.parametrize("mask", [0o000, 0o022, 0o077])
def test_init_restricts_new_data_permissions(isolated_db, mask):
    previous = os.umask(mask)
    try:
        isolated_db.init()
    finally:
        os.umask(previous)
    _assert_private_modes(isolated_db)


@pytest.mark.skipif(os.name != "posix", reason="POSIX 文件权限")
def test_conn_restricts_existing_data_permissions(isolated_db):
    isolated_db.init()
    first = isolated_db._conn
    os.chmod(isolated_db.DATA_DIR, 0o755)
    for suffix in ("", "-wal", "-shm"):
        os.chmod(isolated_db.DB_PATH + suffix, 0o644)
    # 保留旧连接，确保重新打开时确实存在旧部署遗留的 WAL/SHM。
    isolated_db._conn = None
    try:
        isolated_db.init()
        _assert_private_modes(isolated_db)
        assert isolated_db.get_setting("auth_secret")
    finally:
        first.close()


@pytest.mark.parametrize("error", [PermissionError, NotImplementedError])
def test_chmod_failure_logs_without_blocking_init(isolated_db, monkeypatch, caplog, error):
    def fail_chmod(path, mode):
        raise error("不支持修改权限")

    monkeypatch.setattr(isolated_db.os, "chmod", fail_chmod)
    isolated_db.init()
    assert isolated_db.get_setting("password_hash")
    assert "权限收紧" in caplog.text


def _domain(name=None):
    return db.create_domain(name or f"t-{uuid.uuid4().hex[:8]}.example", "A",
                            "1.1.1.1", 300, "test")


def _change(did, ts, old, new):
    cur = db.conn().execute(
        "INSERT INTO changes (domain_id, ts, old_ip, new_ip, notified) VALUES (?,?,?,?,1)",
        (did, ts, old, new),
    )
    db.conn().commit()
    return cur.lastrowid


def test_change_stats_contract():
    did = _domain()
    now = int(time.time())
    _change(did, now - 40, None, "192.0.2.1")       # 首次记录
    _change(did, now - 30, "192.0.2.1", "192.0.2.2")  # 变动
    _change(did, now - 20, None, None)               # 基线重置 marker
    _change(did, now - 10, "192.0.2.2", "192.0.2.3")  # 变动
    stats = db.change_stats(did)
    assert stats == {"rows": 4, "changes": 2, "first_records": 1, "markers": 1}
    assert db.change_stats(did, since=now - 25)["rows"] == 2


def test_keyset_pagination_survives_concurrent_insert():
    did = _domain()
    now = int(time.time())
    ids = [_change(did, now - i, f"10.0.0.{i}", f"10.0.0.{i + 1}") for i in range(45)]

    page = db.changes_list(did, 10)
    seen = [r["id"] for r in page]
    assert len(seen) == 10
    cursor = page[-1]
    # 翻页过程中插入一条更新的记录：不应造成旧页重复或漏行
    _change(did, now + 100, None, "10.0.0.99")
    while True:
        page = db.changes_list(did, 10, before_ts=cursor["ts"], before_id=cursor["id"])
        if not page:
            break
        seen.extend(r["id"] for r in page)
        cursor = page[-1]

    assert len(seen) == len(set(seen)) == 45
    assert set(seen) == set(ids)


def test_has_older_newer_and_cursor_semantics():
    did = _domain()
    now = int(time.time())
    _change(did, now - 30, "10.0.0.1", "10.0.0.2")
    mid = _change(did, now - 20, "10.0.0.2", "10.0.0.3")
    _change(did, now - 10, "10.0.0.3", "10.0.0.4")
    row = db.conn().execute("SELECT * FROM changes WHERE id = ?", (mid,)).fetchone()
    assert db.has_older(did, row["ts"], row["id"])
    assert db.has_newer(did, row["ts"], row["id"])
    newer = db.changes_list(did, 10, after_ts=row["ts"], after_id=row["id"])
    assert [r["id"] for r in newer][0] > mid  # after_* 返回紧邻的更新页（升序）


def test_change_durations_include_open_ended_segment():
    did = _domain()
    now = int(time.time())
    _change(did, now - 7200, None, "10.1.0.1")
    _change(did, now - 3600, "10.1.0.1", "10.1.0.2")
    got = {d["ip"]: d["dur"] for d in
           db.change_durations(did, now - 7200, None, now)}
    assert got["10.1.0.1"] == 3600
    assert got["10.1.0.2"] == 3600
    assert sum(got.values()) == 7200


def test_changes_list_query_plan_uses_index():
    plan = db.conn().execute(
        "EXPLAIN QUERY PLAN SELECT * FROM changes WHERE domain_id = ? "
        "AND (ts < ? OR (ts = ? AND id < ?)) ORDER BY ts DESC, id DESC LIMIT ?",
        (1, 0, 0, 0, 20),
    ).fetchall()
    text = " ".join(r["detail"] for r in plan)
    assert "USING INDEX" in text.upper()
    assert "TEMP B-TREE" not in text.upper()
    names = {r["name"] for r in db.conn().execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='changes'")}
    assert "idx_changes_dom_ts_id" in names


def test_global_last_bad_check_prefers_latest_failure():
    did = _domain()
    now = int(time.time())
    db.conn().execute(
        "INSERT INTO checks (domain_id, ts, status, ip, rtt_ms, error) VALUES (?,?,?,?,?,?)",
        (did, now + 10, "ok", "10.2.0.1", 12, None))
    db.conn().execute(
        "INSERT INTO checks (domain_id, ts, status, ip, rtt_ms, error) VALUES (?,?,?,?,?,?)",
        (did, now + 20, "timeout", None, None, "解析超时"))
    db.conn().commit()
    bad = db.global_last_bad_check()
    assert bad["domain_id"] == did
    assert bad["status"] == "timeout"
    assert bad["domain_name"].startswith("t-")
