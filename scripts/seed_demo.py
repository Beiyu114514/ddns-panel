#!/usr/bin/env python3
"""生成演示/压测数据：3 个域名、1 万条变动、marker、9 个 IP、240/241 段时间线边界。

用法：
    .venv/bin/python scripts/seed_demo.py
    .venv/bin/python scripts/seed_demo.py --data-dir /tmp/ddns-demo --changes 10000

脚本只写 DDNS_DATA_DIR 指向的 SQLite，不会碰真实 data/（除非显式指定）。
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

IPS = [
    "203.0.113.7", "198.51.100.9", "192.0.2.5",
    "203.0.113.42", "198.51.100.77", "192.0.2.88",
    "203.0.113.201", "198.51.100.23", "192.0.2.144",
]

DOMAINS = [
    ("home.demo.example", "A", 300, "家庭宽带 A 记录（1 万条变动压测）"),
    ("edge-240.demo.example", "AAAA", 600, "240 段边界"),
    ("edge-241.demo.example", "A", 900, "241 段边界"),
]


def _prepare(data_dir: str | None):
    if data_dir:
        os.environ["DDNS_DATA_DIR"] = os.path.abspath(data_dir)
    from app import db
    db.init()
    return db


def _drop_demo(db):
    for name, rrtype, *_ in DOMAINS:
        old = db.get_domain_by_name(name, rrtype)
        if old:
            db.delete_domain(old["id"])


def _checks(c, did: int, now: int, *, bad_every: int = 17):
    rows = []
    for i in range(96):  # 最近 48 小时，每 30 分钟
        status = "timeout" if i % bad_every == 0 else "ok"
        rows.append((did, now - i * 1800, status,
                     "203.0.113.7" if status == "ok" else None,
                     round(12 + (i % 13) * 1.7, 1) if status == "ok" else None,
                     "解析超时" if status != "ok" else None))
    c.executemany(
        "INSERT INTO checks (domain_id, ts, status, ip, rtt_ms, error) "
        "VALUES (?,?,?,?,?,?)", rows)


def _changes(c, did: int, now: int, count: int, *, span_days: int = 90,
             marker_days: int | None = None):
    """按时间升序插入 count 行，首行是首次记录；可选插入一次基线重置 marker。"""
    if count <= 0:
        return
    start = now - span_days * 86400
    step = max(1, (span_days * 86400) // max(1, count))
    rows = [(did, start, None, IPS[0], 1)]
    marker_ts = now - marker_days * 86400 if marker_days else None
    inserted_marker = False
    normal = count - 2 if marker_ts and count > 2 else count
    for i in range(1, normal):
        ts = start + i * step
        if marker_ts and not inserted_marker and ts >= marker_ts:
            rows.append((did, marker_ts, None, None, 1))
            rows.append((did, marker_ts + 1, None, IPS[i % len(IPS)], 1))
            inserted_marker = True
        old = rows[-1][3] if rows[-1][3] is not None else None
        new = IPS[(i * 5 + 2) % len(IPS)]
        if new == old:
            new = IPS[(i * 5 + 3) % len(IPS)]
        rows.append((did, ts, old, new, 1))
    c.executemany(
        "INSERT INTO changes (domain_id, ts, old_ip, new_ip, notified) "
        "VALUES (?,?,?,?,?)", rows)


def seed(db, changes: int, seed_value: int = 20261109) -> dict:
    random.seed(seed_value)
    now = int(time.time())
    _drop_demo(db)
    c = db.conn()
    report = {}
    for idx, (name, rrtype, interval, note) in enumerate(DOMAINS):
        did = db.create_domain(name, rrtype, "223.5.5.5", interval, note)
        n = changes if idx == 0 else (240 if idx == 1 else 241)
        _changes(c, did, now, n, span_days=90 if idx == 0 else 30,
                 marker_days=40 if idx == 0 else None)
        _checks(c, did, now, bad_every=17 if idx else (5 if idx == 1 else 23))
        report[name] = {"id": did, "rows": c.execute(
            "SELECT COUNT(*) AS n FROM changes WHERE domain_id = ?", (did,)).fetchone()["n"]}
    c.commit()
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", help="数据目录（默认沿用 DDNS_DATA_DIR 或仓库 data/）")
    ap.add_argument("--changes", type=int, default=10000, help="第一个域名的变动行数")
    args = ap.parse_args()
    db = _prepare(args.data_dir)
    report = seed(db, args.changes)
    print(f"数据目录: {db.DATA_DIR}")
    for name, info in report.items():
        print(f"  {name}: id={info['id']} changes={info['rows']}")
    pw = Path(db.DATA_DIR) / "initial_password.txt"
    if pw.exists():
        print(f"初始密码文件: {pw}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
