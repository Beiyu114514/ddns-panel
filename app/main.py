import asyncio
import datetime
import hashlib
import hmac
import math
import os
import re
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

monitor = Monitor()


async def _cleanup_loop():
    while True:
        try:
            days = to_int(db.get_setting("retention_days", "30"), 30)
            db.cleanup_checks(days)
        except Exception:
            pass
        await asyncio.sleep(6 * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    await monitor.start()
    cleanup = asyncio.create_task(_cleanup_loop())
    yield
    monitor.stop()
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


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    p = request.url.path
    if p != "/login" and not p.startswith("/static") \
            and not _cookie_ok(request.cookies.get(COOKIE, "")):
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


LOGIN_FAILS: dict[str, tuple[int, int]] = {}


@app.get("/login")
async def login_page(request: Request):
    if _cookie_ok(request.cookies.get(COOKIE, "")):
        return RedirectResponse("/", 303)
    return templates.TemplateResponse(request, "login.html", {"hide_nav": True})


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    pw = str(form.get("password", ""))
    ip = _client_ip(request)
    fails, lock = LOGIN_FAILS.get(ip, (0, 0))
    now = int(time.time())
    if lock > now:
        return RedirectResponse("/login?err=" + quote("尝试次数过多，请稍后再试"), 303)
    if db.verify_password(pw, db.get_setting("password_hash", "")):
        LOGIN_FAILS.pop(ip, None)
        exp = now + 30 * 86400
        resp = RedirectResponse("/", 303)
        resp.set_cookie(COOKIE, f"{exp}.{_sign(exp)}", max_age=30 * 86400,
                        httponly=True, samesite="lax")
        return resp
    LOGIN_FAILS[ip] = (fails + 1, now + 60 if fails + 1 >= 5 else 0)
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
        last_ch = db.last_change_ts(d["id"])
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
            "stable": fmt_dur(now - (last_ch or d["created_at"])),
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
    did = db.create_domain(name, rrtype, resolver, interval, note)
    monitor.poke()
    res = await monitor.run_check(did)
    if res["status"] == "ok":
        msg = f"添加成功，首次解析: {res['ip']} ({res['rtt']:.0f}ms)"
        if is_cloudflare(res["ip"]):
            msg += ("。注意: 解析结果疑似 Cloudflare 代理节点（橙云），监控到的是 CF IP 而非源站真实 IP；"
                    "如需监控源站请将记录设为仅 DNS（灰云）")
        return RedirectResponse(f"/domain/{did}?msg=" + quote(msg), 303)
    err = f"已添加，但首次解析失败: {res['error'] or res['status']}"
    return RedirectResponse(f"/domain/{did}?err=" + quote(err), 303)


@app.get("/domain/{domain_id}")
async def domain_detail(request: Request, domain_id: int, days: int = 30):
    d = db.get_domain(domain_id)
    if not d:
        return RedirectResponse("/?err=" + quote("监控项不存在"), 303)
    if days not in (7, 30, 90):
        days = 30
    now = int(time.time())
    start = now - days * 86400
    total = max(1, now - start)

    segs_raw = []
    before = db.last_change_before(domain_id, start)
    if before:
        segs_raw.append([start, None, before["new_ip"]])
    for c in db.changes_since(domain_id, start):
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
    dur_pct = {ip: round(dur / total * 100) for ip, dur in legend.items()}

    last_ch = db.last_change_ts(domain_id)
    return templates.TemplateResponse(request, "domain.html", {
        "d": d,
        "resolvers": RESOLVERS,
        "cur": db.current_ip(domain_id),
        "last": db.last_check(domain_id),
        "stable": fmt_dur(now - (last_ch or d["created_at"])),
        "total_changes": db.count_changes(domain_id),
        "days": days,
        "segs": segs,
        "legend": legend_items,
        "dur_pct": dur_pct,
        "changes": db.changes_list(domain_id),
        "checks": db.recent_checks(domain_id, 20),
    })


@app.post("/domain/{domain_id}/update")
async def domain_update(request: Request, domain_id: int):
    d = db.get_domain(domain_id)
    if not d:
        return RedirectResponse("/", 303)
    form = await request.form()
    rrtype = str(form.get("rrtype", d["rrtype"]))
    if rrtype not in ("A", "AAAA"):
        rrtype = d["rrtype"]
    resolver = _clean_resolver(str(form.get("resolver", "")), d["resolver"])
    interval = max(60, min(86400, to_int(form.get("interval_sec"), d["interval_sec"])))
    note = str(form.get("note", "")).strip()[:200]
    enabled = 1 if form.get("enabled") else 0
    db.update_domain(domain_id, rrtype=rrtype, resolver=resolver,
                     interval_sec=interval, note=note, enabled=enabled)
    if rrtype != d["rrtype"]:
        db.reset_baseline(domain_id)
    monitor.poke()
    return RedirectResponse(f"/domain/{domain_id}?msg=" + quote("设置已保存"), 303)


@app.post("/domain/{domain_id}/delete")
async def domain_delete(domain_id: int):
    db.delete_domain(domain_id)
    monitor.poke()
    return RedirectResponse("/?msg=" + quote("已删除"), 303)


@app.post("/domain/{domain_id}/check")
async def domain_check(domain_id: int):
    if not db.get_domain(domain_id):
        return RedirectResponse("/", 303)
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
    db.set_setting("password_hash", db.hash_password(p1))
    return RedirectResponse("/settings?msg=" + quote("密码已修改"), 303)


def _save_notify(form):
    db.set_setting("tg_enabled", "1" if form.get("tg_enabled") else "0")
    db.set_setting("notify_on_change", "1" if form.get("notify_on_change") else "0")
    db.set_setting("notify_on_fail", "1" if form.get("notify_on_fail") else "0")
    db.set_setting("tg_token", str(form.get("tg_token", "")).strip())
    db.set_setting("tg_chat", str(form.get("tg_chat", "")).strip())
    cd = max(5, min(10080, to_int(form.get("fail_cooldown_min"), 60)))
    db.set_setting("fail_cooldown_min", str(cd))


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
