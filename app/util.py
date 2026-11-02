import datetime
import hashlib
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


def ip_color(ip: str) -> str:
    h = int(hashlib.md5(ip.encode()).hexdigest(), 16) % 360
    return f"hsl({h}, 60%, 55%)"


def to_int(v, default: int) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default
