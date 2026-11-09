#!/usr/bin/env python3
"""页面结构门禁：用 TestClient 渲染五个页面，检查可访问性契约。

用法：
    .venv/bin/python scripts/check_pages.py

检查项：唯一 h1/title、lang、skip link、导航 aria-current、登录 autocomplete、
图表键盘入口、时间线文本等价物、keyset 分页 disabled 态、表单 label、令牌不回显。
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DDNS_DATA_DIR", tempfile.mkdtemp(prefix="ddns-pages-"))

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402


def _titles(html: str) -> list[str]:
    return re.findall(r"<title>(.*?)</title>", html, re.S)


def _seed() -> int:
    did = db.create_domain(f"pages-{uuid.uuid4().hex[:6]}.example", "A",
                           "1.1.1.1", 300, "页面检查")
    now = int(time.time())
    c = db.conn()
    for i in range(3):
        c.execute("INSERT INTO changes (domain_id, ts, old_ip, new_ip, notified) "
                  "VALUES (?,?,?,?,1)",
                  (did, now - (i + 1) * 3600, None if i == 2 else "1.1.1.1", "2.2.2.2"))
    c.commit()
    return did


def main() -> int:
    db.init()
    did = _seed()
    from app.main import app

    client = TestClient(app)
    pw = (Path(db.DATA_DIR) / "initial_password.txt").read_text(encoding="utf-8").strip()
    client.post("/login", data={"password": pw}, follow_redirects=False)

    pages = {
        "登录": (client, "/login"),
        "首页": (client, "/"),
        "域名详情": (client, f"/domain/{did}"),
        "添加域名": (client, "/domain/new"),
        "设置": (client, "/settings"),
    }
    # 登录页需要未登录状态，单独用新 client 取
    anon = TestClient(app)

    failures: list[str] = []
    titles: list[str] = []
    htmls: dict[str, str] = {}

    for label, (cli, url) in pages.items():
        if label == "登录":
            cli = anon
        resp = cli.get(url)
        html = resp.text
        htmls[label] = html
        if resp.status_code != 200:
            failures.append(f"{label} {url} 状态码 {resp.status_code}")
            continue
        if len(_titles(html)) != 1 or not _titles(html)[0].strip():
            failures.append(f"{label} 缺少唯一非空 <title>")
        else:
            titles.append(_titles(html)[0].strip())
        if len(re.findall(r"<h1\b", html)) != 1:
            failures.append(f"{label} <h1> 数量不是 1")
        if 'lang="zh-CN"' not in html:
            failures.append(f"{label} 缺少 lang=zh-CN")
        if 'class="skip-link"' not in html:
            failures.append(f"{label} 缺少 skip link")
        if label != "登录":
            if 'aria-current="page"' not in html:
                failures.append(f"{label} 导航缺少 aria-current=page")

    if len(set(titles)) != len(titles):
        failures.append("五个页面 title 不唯一")

    login = htmls.get("登录", "")
    if 'autocomplete="current-password"' not in login:
        failures.append("登录页缺少 autocomplete=current-password")
    if "autofocus" in login:
        failures.append("登录页仍有 autofocus")

    index = htmls.get("首页", "")
    if not re.search(r'id="chart-svg"[^>]*tabindex="0"', index):
        failures.append("首页图表缺少 tabindex=0 键盘入口")
    if 'role="group"' not in index or 'role="img"' in index:
        failures.append("首页图表 role 不正确（应 group，不应 img）")
    if index.count('class="chart-hit"') != 24:
        failures.append("首页图表可交互点不是 24 个")
    if "查看逐小时数据表" not in index:
        failures.append("首页图表缺少逐小时数据表文本等价物")
    if 'id="domains"' not in index or 'class="card chart-card"' not in index:
        failures.append("首页缺少监控项或图表区块")
    elif index.index('id="domains"') > index.index('class="card chart-card"'):
        failures.append("首页监控项未排在图表之前")
    if "立即检查" not in index:
        failures.append("首页缺少「立即检查」")

    detail = htmls.get("域名详情", "")
    if 'id="tl-track"' not in detail or 'tabindex="0"' not in detail:
        failures.append("时间线缺少 tabindex=0")
    if "时间线明细" not in detail:
        failures.append("时间线缺少 details 文本等价物")
    if "该窗口内" not in detail:
        failures.append("时间线缺少窗口内变动计数")
    if len(re.findall(r'class="tl-tick[ \"]', detail)) != 5:
        failures.append("时间线刻度不是 5 个")
    if 'aria-disabled="true"' not in detail:
        failures.append("分页缺少 disabled 语义")
    if "onsubmit" in detail or "confirm(" in detail:
        failures.append("详情页仍有内联字符串拼接确认")

    new = htmls.get("添加域名", "")
    if len(re.findall(r'<option value="(?:60|300|600|1800|3600|7200|21600|86400)"', new)) != 8:
        failures.append("添加域名页间隔选项不是 8 档")
    for attr in ('autocorrect="off"', 'spellcheck="false"', 'inputmode="url"'):
        if attr not in new:
            failures.append(f"域名输入缺少 {attr}")
    ids = re.findall(r'<input[^>]*\bid="([^"]+)"', new)
    for input_id in ids:
        if f'for="{input_id}"' not in new:
            failures.append(f"添加域名页 input#{input_id} 缺少 label")

    settings = htmls.get("设置", "")
    if "SECRET" in settings:
        failures.append("设置页回显了令牌")
    if "保存数据保留设置" not in settings:
        failures.append("数据保留按钮文案不明确")

    for label, html in htmls.items():
        for form in re.findall(r'<form\b[^>]*method="post"[^>]*>', html):
            if "data-pending=" not in form:
                failures.append(f"{label} 存在无 pending 的 POST 表单")

    if failures:
        print("页面结构检查失败：", file=sys.stderr)
        for f in failures:
            print(" - " + f, file=sys.stderr)
        return 1

    print(f"页面结构检查通过（{len(pages)} 页）：")
    for title in titles:
        print(f"  PASS {title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
