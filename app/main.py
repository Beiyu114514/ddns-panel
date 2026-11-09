import asyncio
import datetime
import hashlib
import heapq
import hmac
import ipaddress
import math
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db
from .monitor import Monitor, RESOLVERS, resolve_once
from .notify import send_tg
from .util import fmt_dur, fmt_ago, fmt_short, fmt_ts, ip_color, is_cloudflare, to_int

BASE = os.path.dirname(os.path.abspath(__file__))
COOKIE = "ddns_session"
CHANGES_PAGE_SIZE = 20
TIMELINE_MAX_SEGMENTS = 240
TIMELINE_LEGEND_MAX = 8

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
templates.env.globals["ip_color"] = ip_color


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
    return templates.TemplateResponse(request, "login.html", {"hide_nav": True})


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    pw = str(form.get("password", ""))
    ip = _rate_limit_key(_client_ip(request))
    now = int(time.time())
    _prune_login_fails(now)
    fails, lock, seen = LOGIN_FAILS.get(ip, (0, 0, 0))
    if lock > now:
        return RedirectResponse("/login?err=" + quote("尝试次数过多，请稍后再试"), 303)
    if lock:
        fails = 0  # 锁定过期后重新计数，否则一次错误就永久再锁
    # 占位计数必须在 await 之前完成，否则并发请求都读到旧值、各自写回 1，
    # 5 次阈值永远触发不了。占位只累加计数，锁定由"确认失败"触发；本次密码
    # 正确时占位被清除，因此不会把自己的正确登录挡在门外。
    # 超额请求在验证前就拒绝，否则并发探测会把 5 次上限放大成 N 次密码校验。
    pending = fails + 1
    if pending > 5:
        LOGIN_FAILS[ip] = (pending, now + 60, now)
        return RedirectResponse("/login?err=" + quote("尝试次数过多，请稍后再试"), 303)
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
                        httponly=True, samesite="lax")
        return resp
    if ok:
        # 校验通过但密码已在验证期间被改掉：不签 cookie，也不计入失败次数
        LOGIN_FAILS.pop(ip, None)
        return RedirectResponse("/login?err=" + quote("密码已变更，请用新密码登录"), 303)
    if pending >= 5:
        LOGIN_FAILS[ip] = (pending, now + 60, now)
    return RedirectResponse("/login?err=" + quote("密码错误"), 303)


@app.post("/logout")
async def logout():
    resp = RedirectResponse("/login", 303)
    resp.delete_cookie(COOKIE)
    return resp


# ---------- 总览 ----------

CHART_SERIES = (("checks", "检查次数"), ("changes", "IP 变动"), ("fail", "失败"))
CHART_W, CHART_H = 720, 240
CHART_PAD_L, CHART_PAD_R, CHART_PAD_T, CHART_PAD_B = 38, 12, 12, 30


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
    h0 = datetime.datetime.now().replace(minute=0, second=0, microsecond=0) \
        - datetime.timedelta(hours=hours - 1)
    start = int(h0.timestamp())
    b_checks, b_fail, b_changes = [0] * hours, [0] * hours, [0] * hours
    for c in db.checks_since(start):
        i = int((datetime.datetime.fromtimestamp(c["ts"]) - h0).total_seconds() // 3600)
        if 0 <= i < hours:
            b_checks[i] += 1
            if c["status"] != "ok":
                b_fail[i] += 1
    for c in db.changes_ts_since(start):
        i = int((datetime.datetime.fromtimestamp(c["ts"]) - h0).total_seconds() // 3600)
        if 0 <= i < hours:
            b_changes[i] += 1

    plot_w = CHART_W - CHART_PAD_L - CHART_PAD_R
    plot_h = CHART_H - CHART_PAD_T - CHART_PAD_B
    xs = [CHART_PAD_L + i * plot_w / (hours - 1) for i in range(hours)]
    ymax = _nice_max(max(max(b_checks), max(b_changes), max(b_fail)))

    def _y(v):
        return round(CHART_PAD_T + (1 - v / ymax) * plot_h, 1)

    series = []
    for vals, key, label in ((b_checks, "checks", "检查次数"),
                             (b_changes, "changes", "IP 变动"),
                             (b_fail, "fail", "失败")):
        ys = [_y(v) for v in vals]
        series.append({
            "key": key, "label": label, "vs": vals, "ys": ys,
            "d": "M " + " L ".join(f"{xs[i]:.1f} {ys[i]:.1f}" for i in range(hours)),
            "end_x": round(xs[-1], 1), "end_y": ys[-1],
        })

    yt = [{"v": v, "y": _y(v)} for v in (ymax, ymax // 2, 0)]
    xt = [
        {"x": xs[0], "anchor": "start", "label": _hour_label(h0)},
        {"x": xs[hours // 2], "anchor": "middle",
         "label": _hour_label(h0 + datetime.timedelta(hours=hours // 2))},
        {"x": xs[-1], "anchor": "end",
         "label": _hour_label(h0 + datetime.timedelta(hours=hours - 1))},
    ]
    colw = plot_w / (hours - 1)
    hits, rows, titles = [], [], []
    for i in range(hours):
        bs = h0 + datetime.timedelta(hours=i)
        title = f"{_hour_label(bs)} – {_hour_label(bs + datetime.timedelta(hours=1))}"
        titles.append(title)
        rows.append({"t": _hour_label(bs), "checks": b_checks[i],
                     "fail": b_fail[i], "changes": b_changes[i]})
        hx = max(0.0, xs[i] - colw / 2)
        hits.append({"x": round(hx, 1), "w": round(min(colw, CHART_W - hx), 1), "label": title})

    payload = {
        "xs": [round(x, 1) for x in xs],
        "hours": titles,
        "series": [{"key": s["key"], "label": s["label"], "ys": s["ys"], "vs": s["vs"]}
                   for s in series],
    }
    return {"w": CHART_W, "h": CHART_H, "pad_l": CHART_PAD_L, "pad_r": CHART_PAD_R,
            "pad_t": CHART_PAD_T, "plot_h": plot_h, "yt": yt, "xt": xt,
            "hits": hits, "series": series, "rows": rows, "payload": payload}


@app.get("/")
async def index(request: Request):
    now = int(time.time())
    today_start = int(datetime.datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp())
    rows = []
    abnormal = v4 = v6 = 0
    for d in db.list_domains():
        last = db.last_check(d["id"])
        if d["rrtype"] == "AAAA":
            v6 += 1
        else:
            v4 += 1
        if d["enabled"] and last and last["status"] != "ok":
            abnormal += 1
        rows.append({
            "d": d,
            "last": last,
            "cur": db.current_ip(d["id"]),
            "stable": fmt_dur(now - (db.last_activity_ts(d["id"]) or d["created_at"])),
            "c7": db.change_count(d["id"], now - 7 * 86400),
            "c30": db.change_count(d["id"], now - 30 * 86400),
        })
    totals = db.count_domains()
    stats = db.check_stats_since(today_start)
    total, ok = stats["total"] or 0, stats["ok"] or 0
    changes_all = db.count_all_changes()
    return templates.TemplateResponse(request, "index.html", {
        "rows": rows,
        "g": db.global_last_check(),
        "any_enabled": totals["enabled"] > 0,
        "today": {"total": total, "ok": ok, "fail": stats["fail"] or 0,
                  "changes": db.changes_count_since(today_start),
                  "rate": round(ok / total * 100) if total else 0},
        "abnormal": abnormal,
        "overview": {"total": totals["total"], "enabled": totals["enabled"],
                     "disabled": totals["total"] - totals["enabled"],
                     "v4": v4, "v6": v6, "changes_all": changes_all,
                     "c7": db.changes_count_since(now - 7 * 86400),
                     "c30": db.changes_count_since(now - 30 * 86400)},
        "total_changes": changes_all,
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


@app.get("/domain/new")
async def domain_new(request: Request):
    return templates.TemplateResponse(request, "new.html", {"resolvers": RESOLVERS})


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
    if not name:
        return RedirectResponse("/domain/new?err=" + quote("域名格式不正确"), 303)
    if db.get_domain_by_name(name, rrtype):
        return RedirectResponse("/domain/new?err=" + quote(f"{name} ({rrtype}) 已存在"), 303)
    try:
        did = db.create_domain(name, rrtype, resolver, interval, note)
    except sqlite3.IntegrityError:
        return RedirectResponse("/domain/new?err=" + quote(f"{name} ({rrtype}) 已存在"), 303)
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
        return RedirectResponse(f"/domain/{did}?msg=" + quote(msg), 303)
    err = f"已添加，但首次解析失败: {res['error'] or res['status']}"
    return RedirectResponse(f"/domain/{did}?err=" + quote(err), 303)


@app.get("/domain/{domain_id}")
async def domain_detail(request: Request, domain_id: int, days: int = 30, page: int = 1):
    d = db.get_domain(domain_id)
    if not d:
        return RedirectResponse("/?err=" + quote("监控项不存在"), 303)
    if days not in (7, 30, 90):
        days = 30
    now = int(time.time())
    start = now - days * 86400
    total = max(1, now - start)

    # 基线重置后旧历史作废：以 marker 的 (ts,id) 作复合边界，重置后只取更晚的记录，
    # 同一秒内先于 marker 的旧记录不会被误当作重置后的基线
    marker = db.last_reset(domain_id)
    if marker and marker["ts"] >= start:
        window_since, window_after_id = marker["ts"], marker["id"]
        window_count = db.count_changes_window(domain_id, window_since, window_after_id)
        baseline = None
    else:
        window_since, window_after_id = start, None
        window_count = db.count_changes_window(domain_id, window_since)
        before = db.last_change_before(domain_id, start)
        baseline = before["new_ip"] if before and before["new_ip"] is not None else None

    # 变动过密时按时间桶取样：桶宽随有效窗口自动放大，保证最多约 240 段。
    agg_bucket = None
    agg_label = None
    if window_count > TIMELINE_MAX_SEGMENTS:
        span = max(1, now - window_since)
        if span >= 86400:
            unit = 3600
        elif span >= 3600:
            unit = 60
        else:
            unit = 1
        agg_bucket = max(unit, math.ceil(span / TIMELINE_MAX_SEGMENTS / unit) * unit)
        if agg_bucket >= 3600:
            agg_label = f"{agg_bucket // 3600} 小时"
        elif agg_bucket >= 60:
            agg_label = f"{agg_bucket // 60} 分钟"
        else:
            agg_label = f"{agg_bucket} 秒"

    segs_raw = []
    if baseline is not None:
        segs_raw.append([start, None, baseline])
    for c in db.changes_window(domain_id, window_since, window_after_id, bucket=agg_bucket):
        segs_raw.append([c["ts"], None, c["new_ip"]])
    for i, s in enumerate(segs_raw):
        s[1] = segs_raw[i + 1][0] if i + 1 < len(segs_raw) else now

    segs, legend = [], {}
    for s0, s1, ip in segs_raw:
        if s1 <= s0 or not ip:
            continue
        dur = s1 - s0
        segs.append({
            "ip": ip,
            "left": (s0 - start) / total * 100,
            "width": dur / total * 100,
            "title": f"{ip}  {fmt_short(s0)} – {fmt_short(s1)} ({fmt_dur(dur)})",
        })
        legend[ip] = legend.get(ip, 0) + dur
    segs.sort(key=lambda x: x["left"])
    legend_items = sorted(legend.items(), key=lambda kv: -kv[1])
    legend_rows = [
        {"label": ip, "dur": dur, "pct": round(dur / total * 100), "other": False}
        for ip, dur in legend_items[:TIMELINE_LEGEND_MAX]
    ]
    rest = legend_items[TIMELINE_LEGEND_MAX:]
    if rest:
        rest_dur = sum(dur for _, dur in rest)
        legend_rows.append({
            "label": f"其他 {len(rest)} 个 IP",
            "dur": rest_dur,
            "pct": round(rest_dur / total * 100),
            "other": True,
        })

    changes_total = db.count_change_rows(domain_id)
    pages = max(1, (changes_total + CHANGES_PAGE_SIZE - 1) // CHANGES_PAGE_SIZE)
    page = max(1, min(page, pages))

    cur = db.current_ip(domain_id)
    return templates.TemplateResponse(request, "domain.html", {
        "d": d,
        "resolvers": RESOLVERS,
        "cur": cur,
        "cf_warn": is_cloudflare(cur),
        "last": db.last_check(domain_id),
        "stable": fmt_dur(now - (db.last_activity_ts(domain_id) or d["created_at"])),
        "total_changes": db.count_changes(domain_id),
        "days": days,
        "segs": segs,
        "legend": legend_rows,
        "agg_label": agg_label,
        "window_count": window_count,
        "changes": db.changes_list(
            domain_id, CHANGES_PAGE_SIZE, (page - 1) * CHANGES_PAGE_SIZE),
        "changes_total": changes_total,
        "page": page,
        "pages": pages,
        "page_size": CHANGES_PAGE_SIZE,
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
        return RedirectResponse(f"/domain/{domain_id}?err="
                                + quote(f"{d['name']} ({rrtype}) 已存在，无法切换"),
                                303)
    try:
        db.update_domain(domain_id, reset_marker=(rrtype != d["rrtype"]),
                         rrtype=rrtype, resolver=resolver,
                         interval_sec=interval, note=note, enabled=enabled)
    except sqlite3.IntegrityError:
        return RedirectResponse(f"/domain/{domain_id}?err="
                                + quote(f"{d['name']} ({rrtype}) 已存在，无法切换"),
                                303)
    monitor.poke()
    return RedirectResponse(f"/domain/{domain_id}?msg=" + quote("设置已保存"), 303)


@app.post("/domain/{domain_id}/delete")
async def domain_delete(domain_id: int):
    db.delete_domain(domain_id)
    monitor.poke()
    return RedirectResponse("/?msg=" + quote("已删除"), 303)


@app.post("/domain/{domain_id}/check")
async def domain_check(domain_id: int):
    d = db.get_domain(domain_id)
    if not d:
        return RedirectResponse("/", 303)
    if not d["enabled"]:
        return RedirectResponse(f"/domain/{domain_id}?err="
                                + quote("监控项已停用，无法手动检查"), 303)
    res = await monitor.run_check(domain_id)
    if res["status"] == "ok":
        msg = f"解析成功: {res['ip']} ({res['rtt']:.0f}ms)"
        return RedirectResponse(f"/domain/{domain_id}?msg=" + quote(msg), 303)
    err = f"解析失败: {res['error'] or res['status']}"
    return RedirectResponse(f"/domain/{domain_id}?err=" + quote(err), 303)


# ---------- 设置 ----------

def _settings_ctx() -> dict:
    return {
        "tg_enabled": db.get_setting("tg_enabled") == "1",
        "tg_token": db.get_setting("tg_token", ""),
        "tg_chat": db.get_setting("tg_chat", ""),
        "notify_on_change": db.get_setting("notify_on_change", "1") == "1",
        "notify_on_fail": db.get_setting("notify_on_fail", "1") == "1",
        "fail_cooldown_min": to_int(db.get_setting("fail_cooldown_min", "60"), 60),
        "change_cooldown_min": to_int(db.get_setting("change_cooldown_min", "0"), 0),
        "retention_days": to_int(db.get_setting("retention_days", "30"), 30),
    }


@app.get("/settings")
async def settings_page(request: Request):
    return templates.TemplateResponse(request, "settings.html", {"s": _settings_ctx()})


@app.post("/settings/password")
async def settings_password(request: Request):
    form = await request.form()
    p1, p2 = str(form.get("password", "")), str(form.get("password2", ""))
    if len(p1) < 6:
        return RedirectResponse("/settings?err=" + quote("密码至少 6 位"), 303)
    if p1 != p2:
        return RedirectResponse("/settings?err=" + quote("两次输入的密码不一致"), 303)
    db.set_settings({
        "password_hash": await asyncio.to_thread(db.hash_password, p1),
        # 与 hash 同一事务轮换，确保不会出现"密码已改但旧 cookie 仍有效"的中间态
        "auth_secret": secrets.token_hex(32),
    })
    try:
        os.remove(os.path.join(db.DATA_DIR, "initial_password.txt"))
    except OSError:
        pass
    return RedirectResponse("/login?msg=" + quote("密码已修改，请用新密码重新登录"), 303)


def _save_notify(form):
    cd = max(5, min(10080, to_int(form.get("fail_cooldown_min"), 60)))
    ccd = max(0, min(1440, to_int(form.get("change_cooldown_min"), 0)))
    db.set_settings({
        "tg_enabled": "1" if form.get("tg_enabled") else "0",
        "notify_on_change": "1" if form.get("notify_on_change") else "0",
        "notify_on_fail": "1" if form.get("notify_on_fail") else "0",
        "tg_token": str(form.get("tg_token", "")).strip(),
        "tg_chat": str(form.get("tg_chat", "")).strip(),
        "fail_cooldown_min": cd,
        "change_cooldown_min": ccd,
    })


@app.post("/settings/notify")
async def settings_notify(request: Request):
    form = await request.form()
    _save_notify(form)
    return RedirectResponse("/settings?msg=" + quote("通知设置已保存"), 303)


@app.post("/settings/test_tg")
async def settings_test_tg(request: Request):
    form = await request.form()
    _save_notify(form)
    token = str(form.get("tg_token", "")).strip()
    chat = str(form.get("tg_chat", "")).strip()
    ok, info = await send_tg(token, chat, f"DDNS 监控测试消息\n时间: {fmt_ts(int(time.time()))}")
    if ok:
        return RedirectResponse("/settings?msg=" + quote("测试消息已发送，请在 Telegram 查收"), 303)
    return RedirectResponse("/settings?err=" + quote(f"测试失败: {info}"), 303)


@app.post("/settings/data")
async def settings_data(request: Request):
    form = await request.form()
    days = max(1, min(365, to_int(form.get("retention_days"), 30)))
    db.set_setting("retention_days", str(days))
    return RedirectResponse("/settings?msg=" + quote("已保存，到期检查记录会自动清理"), 303)
