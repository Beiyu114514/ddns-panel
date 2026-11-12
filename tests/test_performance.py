import time
import uuid
import importlib.util
import os

import pytest

from app import db


@pytest.fixture
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setenv("DDNS_DATA_DIR", str(tmp_path / "private-data"))
    spec = importlib.util.spec_from_file_location("isolated_perf_db", db.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    if module._conn is not None:
        module._conn.close()


def test_large_query_plans_use_time_indexes(tmp_path, monkeypatch):
    monkeypatch.setenv("DDNS_DATA_DIR", str(tmp_path / "private-data"))
    spec = importlib.util.spec_from_file_location("perf_isolated_db", db.__file__)
    isolated_db = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(isolated_db)
    isolated_db.init()
    did = isolated_db.create_domain(f"perf-{uuid.uuid4().hex}.example", "A",
                                    "1.1.1.1", 300, "test")
    now = int(time.time())
    c = isolated_db.conn()
    c.executemany(
        "INSERT INTO changes (domain_id, ts, old_ip, new_ip, notified) VALUES (?, ?, ?, ?, 1)",
        [(did, now - i, "192.0.2.1", "192.0.2.2") for i in range(25000)],
    )
    c.executemany(
        "INSERT INTO checks (domain_id, ts, status) VALUES (?, ?, ?)",
        [(did, now - i, "ok" if i % 10 else "timeout") for i in range(25000)],
    )
    c.commit()
    c.execute("DELETE FROM settings WHERE key = 'check_hourly_backfilled'")
    isolated_db._migrate_check_hourly(c)
    assert c.execute("SELECT SUM(total) FROM check_hourly").fetchone()[0] == 25000
    isolated_db._migrate_check_hourly(c)
    assert c.execute("SELECT SUM(total) FROM check_hourly").fetchone()[0] == 25000

    change_plan = " ".join(r["detail"] for r in c.execute(
        "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM changes WHERE ts >= ? AND old_ip IS NOT NULL",
        (now - 86400,),
    ))
    check_plan = " ".join(r["detail"] for r in c.execute(
        "EXPLAIN QUERY PLAN SELECT hour_ts, total, fail FROM check_hourly "
        "WHERE hour_ts >= ? AND hour_ts < ?",
        (now - 86400, now),
    ))
    pending_plan = " ".join(r["detail"] for r in c.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM changes WHERE domain_id = ? "
        "AND notified = 0 AND new_ip IS NOT NULL ORDER BY ts, id", (did,),
    ))
    assert "SCAN changes" not in change_plan
    assert "SCAN check_hourly" not in check_plan
    assert "idx_check_hourly_ts" in check_plan
    stats_plan = " ".join(r["detail"] for r in c.execute(
        "EXPLAIN QUERY PLAN SELECT SUM(total) FROM check_hourly WHERE hour_ts >= ?",
        (now - 86400,),
    ))
    assert "idx_check_hourly_ts" in stats_plan
    assert "idx_changes_pending" in pending_plan
    latest_check_plan = " ".join(r["detail"] for r in c.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM checks WHERE domain_id = ? "
        "ORDER BY ts DESC, id DESC LIMIT 1", (did,),
    ))
    assert "idx_checks_dom_ts" in latest_check_plan
    c.close()


def test_cleanup_preserves_retention_boundary_hour(isolated_database, monkeypatch):
    isolated_database.init()
    did = isolated_database.create_domain(f"cleanup-{uuid.uuid4().hex}.example", "A",
                                          "1.1.1.1", 300, "test")
    now = 2_000_000_000
    cutoff = now - 86400
    c = isolated_database.conn()
    c.executemany(
        "INSERT INTO checks (domain_id, ts, status) VALUES (?, ?, ?)",
        [(did, cutoff - 1, "ok"), (did, cutoff + 1, "timeout")],
    )
    c.execute(
        "INSERT INTO check_hourly (domain_id, hour_ts, total, fail) "
        "SELECT domain_id, ts - (ts % 3600), COUNT(*), "
        "SUM(CASE WHEN status != 'ok' THEN 1 ELSE 0 END) FROM checks "
        "WHERE domain_id = ? GROUP BY domain_id, ts - (ts % 3600)", (did,),
    )
    c.commit()
    monkeypatch.setattr(isolated_database.time, "time", lambda: now)

    isolated_database.cleanup_checks(1)
    assert c.execute("SELECT COUNT(*) FROM checks WHERE domain_id = ?", (did,)).fetchone()[0] == 1
    row = c.execute("SELECT total, fail FROM check_hourly WHERE domain_id = ?", (did,)).fetchone()
    assert tuple(row) == (1, 1)
    c.close()


def test_homepage_20w_checks_uses_hourly_summary(auth_client):
    from app.main import db as app_db

    did = app_db.create_domain(f"home-perf-{uuid.uuid4().hex}.example", "A",
                               "1.1.1.1", 300, "test")
    now = int(time.time())
    c = app_db.conn()
    c.executemany(
        "INSERT INTO checks (domain_id, ts, status) VALUES (?, ?, ?)",
        [(did, now - (i % 86400), "ok" if i % 10 else "timeout")
         for i in range(200000)],
    )
    c.execute(
        "INSERT INTO check_hourly (domain_id, hour_ts, total, fail) "
        "SELECT domain_id, ts - (ts % 3600), COUNT(*), "
        "SUM(CASE WHEN status != 'ok' THEN 1 ELSE 0 END) FROM checks "
        "WHERE domain_id = ? GROUP BY domain_id, ts - (ts % 3600)", (did,),
    )
    c.commit()

    started = time.perf_counter()
    response = auth_client.get("/")
    elapsed = time.perf_counter() - started
    assert response.status_code == 200
    assert elapsed < 0.3


def test_detail_request_stays_under_one_second(auth_client):
    from app.main import db as app_db

    did = app_db.create_domain(f"perf-{uuid.uuid4().hex}.example", "A",
                               "1.1.1.1", 300, "test")
    now = int(time.time())
    c = app_db.conn()
    c.executemany(
        "INSERT INTO changes (domain_id, ts, old_ip, new_ip, notified) VALUES (?, ?, ?, ?, 1)",
        [(did, now - i, f"192.0.2.{i % 250 + 1}", f"192.0.2.{(i + 1) % 250 + 1}")
         for i in range(20000)],
    )
    c.commit()
    started = time.perf_counter()
    response = auth_client.get(f"/domain/{did}?days=90")
    elapsed = time.perf_counter() - started
    assert response.status_code == 200
    assert elapsed < 1.0
