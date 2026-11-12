import datetime
import ipaddress

# Cloudflare 官方公布的 IP 段，用于识别"橙云"代理域名
CF_V4 = [
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
]
CF_V6 = [
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
    "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
]

# 固定 12 色调色板：颜色索引由首次出现顺序分配，实际色值在 style.css 的
# --ip-1..--ip-12 中按浅色/深色主题分别定义，保证两种主题下图形对比度 ≥ 3:1。
IP_PALETTE_SIZE = 12

STATUS_LABELS = {
    "ok": "正常",
    "timeout": "超时",
    "nxdomain": "域名不存在",
    "noanswer": "无应答",
    "error": "解析错误",
    "pending": "待检查",
}

INTERVAL_OPTIONS = [
    (60, "1 分钟"), (300, "5 分钟"), (600, "10 分钟"), (1800, "30 分钟"),
    (3600, "1 小时"), (7200, "2 小时"), (21600, "6 小时"), (86400, "24 小时"),
]


def interval_label(sec: int) -> str:
    """将检查间隔转换为常用档位文案，非标准值保留秒数。"""
    return next((label for value, label in INTERVAL_OPTIONS if value == sec), f"{sec} 秒")


def is_cloudflare(ip: str) -> bool:
    if not ip:
        return False
    for part in ip.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            addr = ipaddress.ip_address(part)
        except ValueError:
            continue
        nets = CF_V4 if addr.version == 4 else CF_V6
        if any(addr in ipaddress.ip_network(n) for n in nets):
            return True
    return False


def status_label(status) -> str:
    """把内部状态码统一成中文文案，模板不再直接输出 ok/timeout。"""
    if not status:
        return STATUS_LABELS["pending"]
    return STATUS_LABELS.get(str(status), str(status))


def fmt_empty(value, kind: str = "text") -> str:
    """统一空值表达：文本 —、时间 从未、列表 暂无。"""
    if value is None or value == "":
        return {"time": "从未", "list": "暂无"}.get(kind, "—")
    return value


def fmt_ts(ts) -> str:
    if not ts:
        return "—"
    return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")


def fmt_short(ts) -> str:
    if not ts:
        return "—"
    return datetime.datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")


def fmt_ago(ts) -> str:
    if not ts:
        return "从未"
    d = int(datetime.datetime.now().timestamp()) - int(ts)
    if d < 0:
        d = 0
    if d < 60:
        return "刚刚"
    if d < 3600:
        return f"{d // 60} 分钟前"
    if d < 86400:
        return f"{d // 3600} 小时前"
    return f"{d // 86400} 天前"


def fmt_dur(sec) -> str:
    sec = max(0, int(sec or 0))
    if sec >= 86400:
        return f"{sec / 86400:.1f} 天"
    if sec >= 3600:
        return f"{sec / 3600:.1f} 小时"
    if sec >= 60:
        return f"{sec // 60} 分钟"
    return f"{sec} 秒"


def fmt_rtt(ms) -> str:
    if not ms:
        return "—"
    return f"{round(ms):d}ms"


def to_int(v, default: int) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default
