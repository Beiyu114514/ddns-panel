import asyncio
import base64
import datetime
import hashlib
import heapq
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db
from .monitor import Monitor, RESOLVERS, resolve_once
from .notify import send_tg
from .util import (IP_PALETTE_SIZE, fmt_ago, fmt_dur, fmt_empty, fmt_rtt,
                   fmt_short, fmt_ts, is_cloudflare, status_label, to_int)

BASE = os.path.dirname(os.path.abspath(__file__))
COOKIE = "ddns_session"
FLASH_COOKIE = "ddns_flash"


def _cookie_secure() -> bool:
    # 只有明确关闭才允许 HTTP 调试；拼写错误或空值仍保持安全默认值。
    return os.environ.get("COOKIE_SECURE", "true").strip().lower() not in {
        "0", "false", "no", "off",
    }


COOKIE_SECURE = _cookie_secure()
CHANGES_PAGE_SIZE = 20
TIMELINE_MAX_SEGMENTS = 240
TIMELINE_LEGEND_MAX = 8
TIMELINE_TICKS = 5
INTERVAL_OPTIONS = [
    (60, "1 分钟"), (300, "5 分钟"), (600, "10 分钟"), (1800, "30 分钟"),
    (3600, "1 小时"), (7200, "2 小时"), (21600, "6 小时"), (86400, "24 小时"),
]

monitor = Monitor()


async def _cleanup_loop():
    while True:
        try:
            days = to_int(db.get_setting("retention_days", "30"), 30)
            db.cleanup_checks(days)
        except Exception as e:
            print(f"[cleanup] 检查记录清理失败: {e}", flush=True)
        await asyncio.sleep(6 * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    await monitor.start()
    cleanup = asyncio.create_task(_cleanup_loop())
    yield
    await monitor.stop()
    cleanup.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))
templates.env.filters["fmt_ts"] = fmt_ts
templates.env.filters["fmt_short"] = fmt_short
templates.env.filters["fmt_ago"] = fmt_ago
templates.env.filters["fmt_dur"] = fmt_dur
templates.env.filters["fmt_rtt"] = fmt_rtt
templates.env.filters["fmt_empty"] = fmt_empty
templates.env.filters["status_label"] = status_label
templates.env.globals["INTERVAL_OPTIONS"] = INTERVAL_OPTIONS


# ---------- flash 消息（A11：不放在 URL 里，刷新不重复） ----------

def _flash_redirect(url: str, msg: str | None = None,
                    err: str | None = None, field: str | None = None):
    resp = RedirectResponse(url, status_code=303)
    if msg or err:
        # cookie 值只能是 latin-1/ASCII：先 UTF-8 序列化再 base64url，
        # 否则中文提示会在 Set-Cookie 编码时抛 UnicodeEncodeError
        payload = json.dumps({"msg": msg, "err": err, "field": field},
                             ensure_ascii=True).encode("utf-8")
        token = base64.urlsafe_b64encode(payload).decode("ascii")
        resp.set_cookie(FLASH_COOKIE, token, max_age=30, httponly=True,
                        samesite="lax", path="/")
    return resp


def _read_flash(request: Request) -> dict:
    token = request.cookies.get(FLASH_COOKIE)
    if not token:
        return {}
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}


@app.middleware("http")
async def flash_middleware(request: Request, call_next):
    request.state.flash = _read_flash(request)
    response = await call_next(request)
    if request.cookies.get(FLASH_COOKIE):
        # 服务端已消费，立刻清掉；刷新页面不会重复提示。
        # 但处理器刚设置的新 flash 不能被这条删除头覆盖（同名 Set-Cookie 后者生效）。
        set_cookies = response.headers.getlist("set-cookie")
        if not any(c.startswith(f"{FLASH_COOKIE}=") for c in set_cookies):
            response.delete_cookie(FLASH_COOKIE, path="/")
    return response


# ---------- 认证 ----------

def _sign(exp: int) -> str:
    secret = bytes.fromhex(db.get_setting("auth_secret"))
    return hmac.new(secret, f"admin:{exp}".encode(), hashlib.sha256).hexdigest()


def _cookie_ok(value: str) -> bool:
    try:
        exp, sig = value.split(".", 1)
        exp = int(exp)
        return exp > int(time.time()) and hmac.compare_digest(sig, _sign(exp))
    except Exception:
        return False


def _trusted_nets() -> list:
    # 默认只信任 Docker 桥接网段（容器内看到的宿主机反代来源）。回环不默认信任：
    # 直连本机时不存在代理，采信其 XFF 只会让伪造头生效。
    raw = os.environ.get("TRUSTED_PROXIES", "172.16.0.0/12")
    nets = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            print(f"[auth] 忽略无效的 TRUSTED_PROXIES 项: {item}", flush=True)
    return nets


TRUSTED_PROXIES = _trusted_nets()


def _is_trusted(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    # IPv4-mapped IPv6（双栈监听下对端常表现为 ::ffff:172.17.0.1）需要按 IPv4 判断，
    # 否则 in 运算恒为 False，整段反代会被当成不可信来源
    candidates = [addr]
    if addr.version == 6 and addr.ipv4_mapped:
        candidates.append(addr.ipv4_mapped)
    return any(a in n for a in candidates for n in TRUSTED_PROXIES)


def _is_trusted_hop(ip: str) -> bool:
    # CDN 回源地址不在本机反代网段内，但同样是可信代理链的一跳
    return _is_trusted(ip) or is_cloudflare(ip)


def _proxy_hops() -> int:
    return max(1, to_int(os.environ.get("TRUSTED_PROXY_HOPS", "1"), 1))


TRUSTED_PROXY_HOPS = _proxy_hops()


def _client_ip(request: Request) -> str:
    # 直连时用 TCP 对端；仅当对端是可信反代时才解析 XFF。
    # 右侧 TRUSTED_PROXY_HOPS 个条目是可信代理逐跳追加的，其左侧第一个才是客户端。
    # 不能"从右往左跳过所有可信地址"：客户端本身在可信网段时会被跳过，
    # 从而采信它伪造的最左侧条目，限速 key 可被随意轮换。
    peer = request.client.host if request.client else "?"
    if not _is_trusted(peer):
        return peer
    xff = request.headers.get("x-forwarded-for")
    if not xff:
        return peer
    chain = [p.strip() for p in xff.split(",") if p.strip()]
    idx = len(chain) - TRUSTED_PROXY_HOPS
    if idx < 0:
        # XFF 比声明的代理层数还短，头不完整，退回对端地址
        return peer
    # 被跳过的右侧跳数必须是已知代理（本机反代网段或 CDN 回源网段），
    # 否则短链/伪造链会让客户端自己填的条目成为限速 key
    if not all(_is_trusted_hop(p) for p in chain[idx + 1:]):
        return peer
    return chain[idx]


def _rate_limit_key(ip: str) -> str:
    # IPv6 按 /64 聚合：同一用户可随意轮换后缀，不聚合的话失败计数会被
    # 拆成无数条记录，既绕过阈值又撑爆 LOGIN_FAILS
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if addr.version == 6:
        if addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return f"{ipaddress.ip_network((addr, 64), strict=False).network_address}/64"
    return str(addr)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    p = request.url.path
    if p != "/login" and not p.startswith("/static") \
            and not _cookie_ok(request.cookies.get(COOKIE, "")):
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


LOGIN_FAILS: dict[str, tuple[int, int, int]] = {}  # ip -> (fails, lock_until, last_seen)
LOGIN_FAILS_MAX = 8192


def _prune_login_fails(now: int):
    if len(LOGIN_FAILS) <= LOGIN_FAILS_MAX:
        return
    # 只清理"锁定已过期且一小时未活动"的条目；未锁定的计数条目 (lock=0) 不按时间
    # 清理，否则批量散布错误密码就能把所有部分计数清零、绕过阈值
    for k in [k for k, (_, lock, seen) in LOGIN_FAILS.items()
              if lock and lock <= now and now - seen > 3600]:
        del LOGIN_FAILS[k]
    excess = len(LOGIN_FAILS) - LOGIN_FAILS_MAX
    if excess <= 0:
        return
    # 只淘汰超出上限的数量，且先淘汰最久未活动的未锁定/已过期条目。
    # 不能一次多删（旧实现每次至少删 1024 条）：那会成批清零部分失败计数，
    # 未锁定条目不够时还会删掉仍在生效的锁定，等于替攻击者解锁。
    for k in heapq.nsmallest(
            excess,
            (k for k, (_, lock, _) in LOGIN_FAILS.items() if lock <= now),
            key=lambda k: LOGIN_FAILS[k][2]):
        del LOGIN_FAILS[k]
    excess = len(LOGIN_FAILS) - LOGIN_FAILS_MAX
    if excess > 0:
        # 极端情况下剩余条目全部在锁定中：只牺牲最少量的、最早到期的锁定，
        # 保证字典有界；正常情况不会走到这里
        for k in heapq.nsmallest(
                excess, LOGIN_FAILS, key=lambda k: LOGIN_FAILS[k][1]):
            del LOGIN_FAILS[k]


@app.get("/login")
async def login_page(request: Request):
    if _cookie_ok(request.cookies.get(COOKIE, "")):
        return RedirectResponse("/", 303)
    return templates.TemplateResponse(request, "login.html", {
        "hide_nav": True, "error": None, "cookie_secure": COOKIE_SECURE,
    })


def _login_error(request: Request, message: str, status: int = 400):
    # 登录失败留在当前页原位提示，不把错误塞进 URL（A11），也避免刷新丢状态
    return templates.TemplateResponse(request, "login.html", {
        "hide_nav": True, "error": message, "cookie_secure": COOKIE_SECURE,
    }, status_code=status)


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    pw = str(form.get("password", ""))
    ip = _rate_limit_key(_client_ip(request))
    now = int(time.time())
    _prune_login_fails(now)
    fails, lock, seen = LOGIN_FAILS.get(ip, (0, 0, 0))
    if lock > now:
        return _login_error(request, "尝试次数过多，请稍后再试", 429)
    if lock:
        fails = 0  # 锁定过期后重新计数，否则一次错误就永久再锁
    # 占位计数必须在 await 之前完成，否则并发请求都读到旧值、各自写回 1，
    # 5 次阈值永远触发不了。占位只累加计数，锁定由“确认失败”触发；本次密码
    # 正确时占位被清除，因此不会把自己的正确登录挡在门外。
    # 超额请求在验证前就拒绝，否则并发探测会把 5 次上限放大成 N 次密码校验。
    pending = fails + 1
    if pending > 5:
        LOGIN_FAILS[ip] = (pending, now + 60, now)
        return _login_error(request, "尝试次数过多，请稍后再试", 429)
    LOGIN_FAILS[ip] = (pending, 0, now)
    # 快照在本请求内保持一致：hash 与 sign 用的 secret 都取自动作前的同一时刻，
    # 否则登录与改密码重叠时会用新 secret 签出本应失效的旧密码会话
    stored_hash = db.get_setting("password_hash", "")
    ok = await asyncio.to_thread(db.verify_password, pw, stored_hash)
    if ok and stored_hash == db.get_setting("password_hash", ""):
        LOGIN_FAILS.pop(ip, None)
        exp = now + 30 * 86400
        resp = RedirectResponse("/", 303)
        resp.set_cookie(COOKIE, f"{exp}.{_sign(exp)}", max_age=30 * 86400,
                        httponly=True, samesite="lax", secure=COOKIE_SECURE)
        return resp
    if ok:
        # 校验通过但密码已在验证期间被改掉：不签 cookie，也不计入失败次数
        LOGIN_FAILS.pop(ip, None)
        return _login_error(request, "密码已变更，请用新密码登录")
    if pending >= 5:
        LOGIN_FAILS[ip] = (pending, now + 60, now)
    return _login_error(request, "密码错误")


@app.post("/logout")
async def logout():
    resp = _flash_redirect("/login", msg="已退出登录")
    resp.delete_cookie(COOKIE, path="/")
    return resp


# ---------- 总览 ----------

CHART_W, CHART_H = 760, 344
CHART_PAD_L, CHART_PAD_R = 46, 16
CHART_UP_TOP, CHART_UP_H = 24, 132
CHART_LO_TOP, CHART_LO_H = 196, 104
CHART_X_LABEL_Y = 322


def _nice_max(m: int) -> int:
    for s in (4, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000):
        if m <= s:
            return s
    return int(math.ceil(m / 10000.0)) * 10000


def _hour_label(dt: datetime.datetime) -> str:
    now = datetime.datetime.now()
    hm = dt.strftime("%H:%M")
    if dt.date() == now.date():
        return f"今日 {hm}"
    if dt.date() == (now - datetime.timedelta(days=1)).date():
        return f"昨日 {hm}"
    return dt.strftime("%m-%d ") + hm


def _build_chart(hours: int = 24) -> dict:
    """R4：上下两图共享 x 轴。上=检查次数柱，下=IP 变动/失败折线。

    窄屏用 viewBox 等比缩放，不再出现 4.7px 的柱子；每个小时一个
    `<g class="hit">` 悬浮区，触屏也能点出数值，且只有整块区域可聚焦。
    """
    h0 = datetime.datetime.now().replace(minute=0, second=0, microsecond=0) \
        - datetime.timedelta(hours=hours - 1)
    start = int(h0.timestamp())
    b_checks, b_fail, b_changes = [0] * hours, [0] * hours, [0] * hours
    check_groups, change_groups = db.chart_counts(start, hours)
    for row in check_groups:
        b_checks[row["bucket"]] = row["total"]
        b_fail[row["bucket"]] = row["fail"] or 0
    for row in change_groups:
        b_changes[row["bucket"]] = row["total"]

    plot_w = CHART_W - CHART_PAD_L - CHART_PAD_R
    colw = plot_w / hours
    centers = [CHART_PAD_L + colw * (i + 0.5) for i in range(hours)]
    bar_w = round(max(4.0, min(18.0, colw * 0.56)), 1)
    up_max = _nice_max(max(b_checks))
    lo_max = _nice_max(max(max(b_changes), max(b_fail)))

    def _up_y(v):
        return round(CHART_UP_TOP + (1 - v / up_max) * CHART_UP_H, 1)

    def _lo_y(v):
        return round(CHART_LO_TOP + (1 - v / lo_max) * CHART_LO_H, 1)

    def _ticks(ymax, y):
        return [{"v": v, "y": y(v)} for v in (ymax, ymax // 2, 0)]

    def _hour_full(ts: int) -> str:
        a = datetime.datetime.fromtimestamp(ts)
        b = a + datetime.timedelta(hours=1)
        return f"{_hour_label(a)} – {_hour_label(b)}"

    up_bars = [{
        "x": round(centers[i] - bar_w / 2, 1),
        "y": _up_y(b_checks[i]),
        "w": bar_w,
        "h": round(CHART_UP_TOP + CHART_UP_H - _up_y(b_checks[i]), 1),
        "v": b_checks[i],
    } for i in range(hours)]

    def _line(vals, key, label, cls, y):
        pts = [{"x": round(centers[i], 1), "y": y(vals[i]), "v": vals[i],
                "label": f"{_hour_full(start + i * 3600)}：{label} {vals[i]}"}
               for i in range(hours)]
        return {"key": key, "label": label, "class": cls,
                "d": "M " + " L ".join(f"{p['x']} {p['y']}" for p in pts),
                "dots": pts}

    lower_series = [
        _line(b_changes, "changes", "IP 变动", "changes", _lo_y),
        _line(b_fail, "fail", "失败", "fail", _lo_y),
    ]

    x_idx = [0, 6, 12, 18, hours - 1]
    x_ticks = []
    for n, i in enumerate(x_idx):
        anchor = "start" if n == 0 else ("end" if n == len(x_idx) - 1 else "middle")
        x_ticks.append({"x": round(centers[i], 1), "anchor": anchor,
                        "label": _hour_label(h0 + datetime.timedelta(hours=i))})

    hits, rows = [], []
    for i in range(hours):
        ts = start + i * 3600
        rows.append({"t": _hour_label(datetime.datetime.fromtimestamp(ts)),
                     "checks": b_checks[i], "changes": b_changes[i],
                     "fail": b_fail[i]})
        hits.append({
            "x": round(CHART_PAD_L + colw * i, 1),
            "w": round(colw, 1),
            "cx": round(centers[i], 1),
            "label": f"{_hour_full(ts)}：检查 {b_checks[i]} 次，失败 {b_fail[i]} 次，"
                     f"IP 变动 {b_changes[i]} 次",
        })

    payload = {
        "labels": [_hour_label(h0 + datetime.timedelta(hours=i)) for i in range(hours)],
        "checks": b_checks, "changes": b_changes, "fail": b_fail,
    }
    return {
        "w": CHART_W, "h": CHART_H,
        "pad_l": CHART_PAD_L, "pad_r": CHART_PAD_R,
        "up_top": CHART_UP_TOP, "up_h": CHART_UP_H,
        "lo_top": CHART_LO_TOP, "lo_h": CHART_LO_H,
        "x_label_y": CHART_X_LABEL_Y,
        "divider_y": CHART_UP_TOP + CHART_UP_H + 16,
        "up_ticks": _ticks(up_max, _up_y),
        "lo_ticks": _ticks(lo_max, _lo_y),
        "up_bars": up_bars,
        "lower_series": lower_series,
        "x_ticks": x_ticks,
        "hits": hits,
        "rows": rows,
        "payload": payload,
        "legend": [{"key": "checks", "label": "检查次数", "shape": "bar"},
                   {"key": "changes", "label": "IP 变动", "shape": "line"},
                   {"key": "fail", "label": "失败", "shape": "line"}],
    }


@app.get("/")
async def index(request: Request):
    now = int(time.time())
    today_start = int(datetime.datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp())
    rows = []
    abnormal_rows = []
    for d in db.overview_domains(now - 7 * 86400, now - 30 * 86400):
        last = (dict(id=d["last_id"], ts=d["last_ts"], status=d["last_status"],
                     ip=d["last_check_ip"]) if d["last_id"] is not None else None)
        if not d["enabled"]:
            state = "disabled"
        elif last and last["status"] != "ok":
            state = "bad"
        elif not last:
            state = "pending"
        else:
            state = "ok"
        row = {
            "d": d,
            "last": last,
            "state": state,
            "cur": d["cur"],
            "stable": fmt_dur(now - (d["activity"] or d["created_at"])),
            "c7": d["c7"],
            "c30": d["c30"],
        }
        rows.append(row)
        if state == "bad":
            abnormal_rows.append(row)
    order = {"bad": 0, "pending": 1, "ok": 2, "disabled": 3}
    rows.sort(key=lambda r: (order[r["state"]], r["d"]["name"]))
    abnormal_rows.sort(key=lambda r: r["last"]["ts"], reverse=True)

    totals = db.count_domains()
    stats = db.check_stats_since(today_start)
    total, ok = stats["total"] or 0, stats["ok"] or 0
    return templates.TemplateResponse(request, "index.html", {
        "rows": rows,
        "overview": {"total": totals["total"], "enabled": totals["enabled"],
                     "disabled": totals["total"] - totals["enabled"],
                     "c7": db.changes_count_since(now - 7 * 86400),
                     "c30": db.changes_count_since(now - 30 * 86400)},
        "today": {"total": total, "ok": ok, "fail": stats["fail"] or 0,
                  "changes": db.changes_count_since(today_start),
                  "rate": round(ok / total * 100) if total else 0},
        "abnormal_count": len(abnormal_rows),
        "abnormal_rows": abnormal_rows[:5],
        "recent_bad": db.global_last_bad_check(),
        "chart": _build_chart() if totals["total"] else None,
    })


# ---------- 域名管理 ----------

_LABEL_RE = re.compile(r"^[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?$")


def _valid_domain(name: str):
    name = name.strip().rstrip(".").lower()
    if not (3 < len(name) <= 253):
        return None
    labels = name.split(".")
    if len(labels) < 2 or not all(_LABEL_RE.match(l) for l in labels):
        return None
    return name


def _clean_resolver(value: str, fallback: str) -> str:
    import ipaddress
    value = value.strip()
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        return fallback


def _parse_cursor(raw) -> tuple:
    """游标格式 ts_id；非法值按“第一页”处理，不让手工改 URL 触发 500。"""
    if not raw:
        return None, None
    try:
        ts, rid = str(raw).split("_", 1)
        return int(ts), int(rid)
    except (ValueError, TypeError):
        return None, None


def _build_timeline(domain_id: int, days: int, now: int) -> dict:
    """C1/C2/C4/C5/C8：时间线视图模型。

    - 刻度：5 个等距时间点，含首尾；
    - 分段：基线 + 变动，最多 240 段（超出按时间桶取样）；
    - 图例时长：始终用未采样全量记录计算（R1）；
    - 段与图例共享 12 色板（R5），相邻同色靠纹理兜底。
    """
    start = now - days * 86400
    total = max(1, now - start)
    marker = db.last_reset(domain_id)
    if marker and marker["ts"] >= start:
        window_since, after_id = marker["ts"], marker["id"]
        marker_left = (marker["ts"] - start) / total * 100
        baseline = None
    else:
        window_since, after_id = start, None
        marker_left = None
        before = db.last_change_before(domain_id, start)
        baseline = before["new_ip"] if before and before["new_ip"] is not None else None

    counts = db.change_stats(domain_id, since=window_since)
    window_count = db.count_changes_window(domain_id, window_since, after_id)
    # 采样阈值按可绘制行数（含首次记录）判断，展示计数只算真实变动
    sampled = db.count_timeline_rows(domain_id, window_since, after_id) > TIMELINE_MAX_SEGMENTS
    bucket = None
    bucket_label = None
    if sampled:
        span = max(1, now - window_since)
        unit = 3600 if span >= 86400 else (60 if span >= 3600 else 1)
        bucket = max(unit, math.ceil(span / TIMELINE_MAX_SEGMENTS / unit) * unit)
        if bucket >= 3600:
            bucket_label = f"{bucket // 3600} 小时"
        elif bucket >= 60:
            bucket_label = f"{bucket // 60} 分钟"
        else:
            bucket_label = f"{bucket} 秒"

    rows = db.changes_window(domain_id, window_since, after_id, bucket=bucket)
    first_ts = db.first_change_ts(domain_id, window_since, after_id)
    raw = []
    if baseline:
        raw.append((window_since, baseline))
    raw.extend((r["ts"], r["new_ip"]) for r in rows if r["new_ip"])

    colors: dict = {}

    def color_for(ip) -> int:
        if ip not in colors:
            colors[ip] = len(colors) % IP_PALETTE_SIZE + 1
        return colors[ip]

    segments = []
    for i, (s0, ip) in enumerate(raw):
        s1 = raw[i + 1][0] if i + 1 < len(raw) else now
        if s1 <= s0 or not ip:
            continue
        segments.append({
            "ip": ip,
            "start": s0,
            "end": s1,
            "left": round((s0 - start) / total * 100, 3),
            "width": round((s1 - s0) / total * 100, 3),
            "color": color_for(ip),
            "color_index": color_for(ip),
            "t0": fmt_short(s0),
            "t1": fmt_short(s1),
            "dur": fmt_dur(s1 - s0),
            "label": f"{ip}  {fmt_short(s0)} – {fmt_short(s1)}（{fmt_dur(s1 - s0)}）",
            "title": f"{ip}  {fmt_short(s0)} – {fmt_short(s1)}",
        })
    segments.sort(key=lambda s: s["left"])

    durations = {d["ip"]: d["dur"] for d in db.change_durations(
        domain_id, window_since, after_id, now)}
    if baseline:
        # first_ts == window_since 时基线段宽度为 0，不能把整个窗口都算给它
        end = first_ts if first_ts is not None else now
        durations[baseline] = durations.get(baseline, 0) + max(0, min(end, now) - window_since)
    ordered = sorted(((ip, dur) for ip, dur in durations.items() if ip and dur > 0),
                     key=lambda kv: (-kv[1], kv[0]))
    legend = [{"ip": ip, "dur": dur, "pct": round(dur / total * 100),
               "color": color_for(ip), "color_index": color_for(ip), "exact": True}
              for ip, dur in ordered[:TIMELINE_LEGEND_MAX]]
    rest = ordered[TIMELINE_LEGEND_MAX:]
    if rest:
        legend.append({
            "ip": f"其他 {len(rest)} 个 IP",
            "dur": sum(dur for _, dur in rest),
            "pct": round(sum(dur for _, dur in rest) / total * 100),
            "other": True,
        })

    ticks = []
    for i in range(TIMELINE_TICKS):
        ts = start + round(total * i / (TIMELINE_TICKS - 1))
        dt = datetime.datetime.fromtimestamp(ts)
        ticks.append({
            "ts": ts,
            "left": round((ts - start) / total * 100, 3),
            "label": dt.strftime("%m-%d"),
            "full": dt.strftime("%Y-%m-%d %H:%M"),
            "anchor": "start" if i == 0 else ("end" if i == TIMELINE_TICKS - 1 else "middle"),
        })

    return {
        "days": days,
        "start": start,
        "end": now,
        "window": {"start": start, "end": now},
        "total": total,
        "bucket_seconds": bucket,
        "segments": segments,
        "legend": legend,
        "ticks": ticks,
        "marker_left": marker_left,
        "sampled": sampled,
        "bucket_label": bucket_label,
        "counts": {"window_changes": window_count, "rows": counts["rows"],
                   "markers": counts["markers"]},
        "empty": not segments,
    }


def build_timeline(domain_id: int, days: int, now: int) -> dict:
    """公开视图模型（§5.2 契约），模板与测试统一从这里取时间线。"""
    return _build_timeline(domain_id, days, now)


@app.get("/domain/new")
async def domain_new(request: Request):
    return templates.TemplateResponse(request, "new.html", {
        "resolvers": RESOLVERS, "error": None,
        "draft": {"rrtype": "A", "resolver": RESOLVERS[0],
                  "interval_sec": 300, "note": "", "name": ""},
    })


@app.post("/domain/new")
async def domain_create(request: Request):
    form = await request.form()
    name = _valid_domain(str(form.get("name", "")))
    rrtype = str(form.get("rrtype", "A"))
    if rrtype not in ("A", "AAAA"):
        rrtype = "A"
    resolver = _clean_resolver(str(form.get("resolver", "")), RESOLVERS[0])
    interval = max(60, min(86400, to_int(form.get("interval_sec"), 300)))
    note = str(form.get("note", "")).strip()[:200]
    draft = {"name": str(form.get("name", "")).strip(), "rrtype": rrtype,
             "resolver": resolver, "interval_sec": interval, "note": note}
    if not name:
        return templates.TemplateResponse(request, "new.html", {
            "resolvers": RESOLVERS, "draft": draft,
            "error": "域名格式不正确，例如 example.com",
        }, status_code=400)
    if db.get_domain_by_name(name, rrtype):
        return templates.TemplateResponse(request, "new.html", {
            "resolvers": RESOLVERS, "draft": draft,
            "error": f"{name} ({rrtype}) 已存在",
        }, status_code=400)
    try:
        did = db.create_domain(name, rrtype, resolver, interval, note)
    except sqlite3.IntegrityError:
        return templates.TemplateResponse(request, "new.html", {
            "resolvers": RESOLVERS, "draft": draft,
            "error": f"{name} ({rrtype}) 已存在",
        }, status_code=400)
    # 先同步检查落基线，再唤醒调度器；worker 会跳过刚完成的检查，
    # 避免同一域名连续解析两次、多 IP 记录被记成假变动
    try:
        res = await monitor.run_check(did)
    finally:
        monitor.poke()
    if res["status"] == "ok":
        msg = f"添加成功，首次解析: {res['ip']} ({res['rtt']:.0f}ms)"
        if is_cloudflare(res["ip"]):
            msg += ("。注意: 解析结果疑似 Cloudflare 代理节点（橙云），监控到的是 CF IP 而非源站真实 IP；"
                    "如需监控源站请将记录设为仅 DNS（灰云）")
        return _flash_redirect(f"/domain/{did}", msg=msg)
    err = f"已添加，但首次解析失败: {res['error'] or res['status']}"
    return _flash_redirect(f"/domain/{did}", err=err)


@app.get("/domain/{domain_id}")
async def domain_detail(request: Request, domain_id: int, days: int = 30, page: int = 1):
    d = db.get_domain(domain_id)
    if not d:
        return _flash_redirect("/", err="监控项不存在")
    if days not in (7, 30, 90):
        days = 30
    if page and page > 1:
        # 旧 OFFSET 链接没有游标可用，退回第一页而不是猜位置
        return RedirectResponse(f"/domain/{domain_id}?days={days}", 303)
    now = int(time.time())

    timeline = _build_timeline(domain_id, days, now)
    before_ts, before_id = _parse_cursor(request.query_params.get("before"))
    after_ts, after_id = _parse_cursor(request.query_params.get("after"))
    changes = db.changes_list(domain_id, CHANGES_PAGE_SIZE,
                              before_ts, before_id, after_ts, after_id)
    has_older = has_newer = False
    next_before = prev_after = None
    if changes:
        has_older = db.has_older(domain_id, changes[-1]["ts"], changes[-1]["id"])
        has_newer = db.has_newer(domain_id, changes[0]["ts"], changes[0]["id"])
        if has_older:
            last = changes[-1]
            next_before = f"{last['ts']}_{last['id']}"
        if has_newer:
            first = changes[0]
            prev_after = f"{first['ts']}_{first['id']}"

    stats = db.change_stats(domain_id)
    cur = db.current_ip(domain_id)
    # 历史数据/脚本可能写入不在下拉档位里的间隔；补一个当前值选项，
    # 否则浏览器会回落到第一个选项，保存时静默改掉检查间隔
    interval_custom = d["interval_sec"] if all(
        d["interval_sec"] != sec for sec, _ in INTERVAL_OPTIONS) else None
    return templates.TemplateResponse(request, "domain.html", {
        "d": d,
        "interval_custom": interval_custom,
        "resolvers": RESOLVERS,
        "cur": cur,
        "cf_warn": is_cloudflare(cur),
        "last": db.last_check(domain_id),
        "stable": fmt_dur(now - (db.last_activity_ts(domain_id) or d["created_at"])),
        "total_changes": stats["changes"],
        "days": days,
        "tl": timeline,
        "stats": stats,
        "changes": changes,
        "has_older": has_older,
        "has_newer": has_newer,
        "next_before": next_before,
        "prev_after": prev_after,
        "checks": db.recent_checks(domain_id, 20),
    })


@app.post("/domain/{domain_id}/update")
async def domain_update(request: Request, domain_id: int):
    form = await request.form()
    # 必须在 await 之后再读：表单解析期间另一个请求可能已改过记录类型，
    # 用旧值决定是否重置基线会把新类型的基线写坏
    d = db.get_domain(domain_id)
    if not d:
        return RedirectResponse("/", 303)
    rrtype = str(form.get("rrtype", d["rrtype"]))
    if rrtype not in ("A", "AAAA"):
        rrtype = d["rrtype"]
    resolver = _clean_resolver(str(form.get("resolver", "")), d["resolver"])
    interval = max(60, min(86400, to_int(form.get("interval_sec"), d["interval_sec"])))
    note = str(form.get("note", "")).strip()[:200]
    enabled = 1 if form.get("enabled") else 0
    if rrtype != d["rrtype"] and db.get_domain_by_name(d["name"], rrtype):
        return _flash_redirect(f"/domain/{domain_id}",
                               err=f"{d['name']} ({rrtype}) 已存在，无法切换")
    try:
        db.update_domain(domain_id, reset_marker=(rrtype != d["rrtype"]),
                         rrtype=rrtype, resolver=resolver,
                         interval_sec=interval, note=note, enabled=enabled)
    except sqlite3.IntegrityError:
        return _flash_redirect(f"/domain/{domain_id}",
                               err=f"{d['name']} ({rrtype}) 已存在，无法切换")
    monitor.poke()
    return _flash_redirect(f"/domain/{domain_id}", msg="设置已保存")


@app.post("/domain/{domain_id}/delete")
async def domain_delete(domain_id: int):
    db.delete_domain(domain_id)
    monitor.poke()
    return _flash_redirect("/", msg="已删除")


@app.post("/domain/{domain_id}/check")
async def domain_check(domain_id: int):
    d = db.get_domain(domain_id)
    if not d:
        return _flash_redirect("/", err="监控项不存在")
    if not d["enabled"]:
        return _flash_redirect(f"/domain/{domain_id}", err="监控项已停用，无法手动检查")
    res = await monitor.run_check(domain_id)
    if res["status"] == "ok":
        msg = f"解析成功: {res['ip']} ({res['rtt']:.0f}ms)"
        return _flash_redirect(f"/domain/{domain_id}", msg=msg)
    err = f"解析失败: {res['error'] or res['status']}"
    return _flash_redirect(f"/domain/{domain_id}", err=err)


# ---------- 设置 ----------

def _settings_ctx() -> dict:
    # 只给“是否已配置”，绝不把 Bot Token 回显到 HTML（A12）
    return {
        "tg_enabled": db.get_setting("tg_enabled") == "1",
        "tg_token_set": bool(db.get_setting("tg_token", "")),
        "tg_chat": db.get_setting("tg_chat", ""),
        "notify_on_change": db.get_setting("notify_on_change", "1") == "1",
        "notify_on_fail": db.get_setting("notify_on_fail", "1") == "1",
        "fail_cooldown_min": to_int(db.get_setting("fail_cooldown_min", "60"), 60),
        "change_cooldown_min": to_int(db.get_setting("change_cooldown_min", "0"), 0),
        "retention_days": to_int(db.get_setting("retention_days", "30"), 30),
    }


def _settings_render(request: Request, *, errors=None, error_form=None,
                     extra=None, status_code: int = 200):
    ctx = {"s": _settings_ctx(), "errors": errors or {}, "error_form": error_form}
    if extra:
        ctx["s"].update(extra)
    return templates.TemplateResponse(request, "settings.html", ctx,
                                      status_code=status_code)


@app.get("/settings")
async def settings_page(request: Request):
    return _settings_render(request)


@app.post("/settings/password")
async def settings_password(request: Request):
    form = await request.form()
    p1, p2 = str(form.get("password", "")), str(form.get("password2", ""))
    if len(p1) < 6:
        return _settings_render(request, error_form="password",
                                errors={"password": "密码至少 6 位"},
                                status_code=400)
    if p1 != p2:
        return _settings_render(request, error_form="password",
                                errors={"password2": "两次输入的密码不一致"},
                                status_code=400)
    db.set_settings({
        "password_hash": await asyncio.to_thread(db.hash_password, p1),
        # 与 hash 同一事务轮换，确保不会出现“密码已改但旧 cookie 仍有效”的中间态
        "auth_secret": secrets.token_hex(32),
    })
    try:
        os.remove(os.path.join(db.DATA_DIR, "initial_password.txt"))
    except OSError:
        pass
    return _flash_redirect("/login", msg="密码已修改，请用新密码重新登录")


def _int_in(raw, lo: int, hi: int):
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def _save_notify(form):
    """保存通知设置；错误只返回字段级消息，不写入半套配置。"""
    errors = {}
    cd = _int_in(form.get("fail_cooldown_min"), 5, 10080)
    ccd = _int_in(form.get("change_cooldown_min"), 0, 1440)
    if cd is None:
        errors["fail_cooldown_min"] = "失败提醒冷却需为 5–10080 分钟"
    if ccd is None:
        errors["change_cooldown_min"] = "变动提醒冷却需为 0–1440 分钟"
    if errors:
        return errors, None
    raw_token = str(form.get("tg_token", "")).strip()
    token = raw_token or db.get_setting("tg_token", "")
    vals = {
        "tg_enabled": "1" if form.get("tg_enabled") else "0",
        "notify_on_change": "1" if form.get("notify_on_change") else "0",
        "notify_on_fail": "1" if form.get("notify_on_fail") else "0",
        "tg_token": token,
        "tg_chat": str(form.get("tg_chat", "")).strip(),
        "fail_cooldown_min": cd,
        "change_cooldown_min": ccd,
    }
    db.set_settings(vals)
    return {}, vals


def _notify_echo(form) -> dict:
    return {
        "tg_enabled": bool(form.get("tg_enabled")),
        "notify_on_change": bool(form.get("notify_on_change")),
        "notify_on_fail": bool(form.get("notify_on_fail")),
        "tg_chat": str(form.get("tg_chat", "")).strip(),
        "fail_cooldown_min": str(form.get("fail_cooldown_min", "")),
        "change_cooldown_min": str(form.get("change_cooldown_min", "")),
        "tg_token_pending": bool(str(form.get("tg_token", "")).strip()),
    }


@app.post("/settings/notify")
async def settings_notify(request: Request):
    form = await request.form()
    errors, _ = _save_notify(form)
    if errors:
        return _settings_render(request, errors=errors, error_form="notify",
                                extra=_notify_echo(form), status_code=400)
    return _flash_redirect("/settings", msg="通知设置已保存")


@app.post("/settings/test_tg")
async def settings_test_tg(request: Request):
    form = await request.form()
    errors, _ = _save_notify(form)
    if errors:
        return _settings_render(request, errors=errors, error_form="notify",
                                extra=_notify_echo(form), status_code=400)
    # 留空表示沿用已保存的 Token；两者都没有才是配置错误
    token = str(form.get("tg_token", "")).strip() or db.get_setting("tg_token", "")
    chat = str(form.get("tg_chat", "")).strip()
    if not token or not chat:
        return _flash_redirect("/settings", err="请先填写 Bot Token 和 Chat ID 再测试")
    ok, info = await send_tg(token, chat,
                             f"DDNS 监控测试消息\n时间: {fmt_ts(int(time.time()))}")
    if ok:
        return _flash_redirect("/settings", msg="测试消息已发送，请在 Telegram 查收")
    return _flash_redirect("/settings", err=f"测试失败: {info}")


@app.post("/settings/data")
async def settings_data(request: Request):
    form = await request.form()
    raw = form.get("retention_days")
    days = _int_in(raw, 1, 365)
    if days is None:
        return _settings_render(request, error_form="data",
                                errors={"retention_days": "请输入 1–365 的整数"},
                                extra={"retention_days": str(raw or "")},
                                status_code=400)
    db.set_setting("retention_days", str(days))
    return _flash_redirect("/settings", msg="已保存，到期检查记录会自动清理")
