import time
import uuid
import importlib.util
import os

from app import db


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

    change_plan = " ".join(r["detail"] for r in c.execute(
        "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM changes WHERE ts >= ? AND old_ip IS NOT NULL",
        (now - 86400,),
    ))
    check_plan = " ".join(r["detail"] for r in c.execute(
        "EXPLAIN QUERY PLAN SELECT CAST((ts - ?) / 3600 AS INTEGER), COUNT(*), "
        "SUM(CASE WHEN status != 'ok' THEN 1 ELSE 0 END) FROM checks "
        "WHERE ts >= ? AND ts < ? GROUP BY 1",
        (now - 86400, now - 86400, now),
    ))
    pending_plan = " ".join(r["detail"] for r in c.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM changes WHERE domain_id = ? "
        "AND notified = 0 AND new_ip IS NOT NULL ORDER BY ts, id", (did,),
    ))
    assert "SCAN changes" not in change_plan
    assert "SCAN checks" not in check_plan
    assert "idx_changes_pending" in pending_plan
    c.close()


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
