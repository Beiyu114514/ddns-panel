#!/usr/bin/env python3
"""详情页性能门禁：1 万条 changes 下 p95 < 200ms。

用法：
    .venv/bin/python scripts/bench_detail.py --data-dir /tmp/ddns-seedcheck
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="/tmp/ddns-demo")
    ap.add_argument("--domain-id", type=int, default=1)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--requests", type=int, default=25)
    ap.add_argument("--limit-ms", type=float, default=200.0)
    args = ap.parse_args()

    os.environ["DDNS_DATA_DIR"] = os.path.abspath(args.data_dir)
    from fastapi.testclient import TestClient  # noqa: E402
    from app import db  # noqa: E402
    from app.main import app  # noqa: E402

    c = db.conn()
    rows = c.execute("SELECT COUNT(*) AS n FROM changes WHERE domain_id = ?",
                     (args.domain_id,)).fetchone()["n"]
    pw = (Path(db.DATA_DIR) / "initial_password.txt").read_text(encoding="utf-8").strip()
    client = TestClient(app, base_url="https://testserver")
    assert client.post("/login", data={"password": pw},
                       follow_redirects=False).status_code == 303
    times, size = [], 0
    for _ in range(args.requests):
        t = time.perf_counter()
        r = client.get(f"/domain/{args.domain_id}?days={args.days}")
        times.append((time.perf_counter() - t) * 1000)
        if r.status_code != 200:
            print(f"详情页返回 {r.status_code}", file=sys.stderr)
            return 1
        size = len(r.content)
    times.sort()
    p95 = times[max(0, int(len(times) * 0.95) - 1)]
    print(f"changes={rows} requests={len(times)} bytes={size}")
    print(f"min {times[0]:.1f}ms  median {times[len(times)//2]:.1f}ms  "
          f"p95 {p95:.1f}ms  max {times[-1]:.1f}ms")
    if p95 >= args.limit_ms:
        print(f"FAIL p95 {p95:.1f}ms >= {args.limit_ms:.0f}ms", file=sys.stderr)
        return 1
    print(f"PASS p95 < {args.limit_ms:.0f}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
