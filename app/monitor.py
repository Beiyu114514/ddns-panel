import asyncio
import random
import time

import dns.asyncresolver
import dns.exception
import dns.resolver

from . import db
from .notify import send_tg
from .util import fmt_short, to_int

RESOLVERS = ["223.5.5.5", "119.29.29.29", "1.1.1.1", "8.8.8.8",
             "2400:3200::1", "2606:4700:4700::1111"]
FAIL_THRESHOLD = 3  # 连续失败达到该次数才告警，避免公共 DNS 抖动误报


async def resolve_once(name: str, rrtype: str, resolver: str) -> dict:
    r = dns.asyncresolver.Resolver(configure=False)
    r.nameservers = [resolver]
    r.timeout = 5.0
    r.lifetime = 10.0
    t0 = time.monotonic()
    try:
        ans = await r.resolve(name, rrtype)
    except dns.resolver.NXDOMAIN:
        return {"status": "nxdomain", "ip": None, "rtt": None, "error": "域名不存在 (NXDOMAIN)"}
    except dns.resolver.Timeout:
        return {"status": "timeout", "ip": None, "rtt": None, "error": "解析超时"}
    except dns.resolver.NoAnswer:
        return {"status": "noanswer", "ip": None, "rtt": None, "error": "无应答记录 (NoAnswer)"}
    except dns.exception.DNSException as e:
        return {"status": "error", "ip": None, "rtt": None, "error": f"DNS 错误: {e}"}
    except Exception as e:
        return {"status": "error", "ip": None, "rtt": None, "error": str(e)[:200]}
    rtt = (time.monotonic() - t0) * 1000
    ips = sorted({rr.address for rr in ans})
    return {"status": "ok", "ip": ", ".join(ips), "rtt": rtt, "error": None}


class Monitor:
    def __init__(self):
        self.sup = None
        self.tasks: dict[int, asyncio.Task] = {}
        self.cfg_hash: dict[int, str] = {}
        self.fail_state: dict[int, dict] = {}
        self.wake = asyncio.Event()

    def poke(self):
        self.wake.set()

    async def start(self):
        self.sup = asyncio.create_task(self._supervisor())

    def stop(self):
        if self.sup:
            self.sup.cancel()
        for t in self.tasks.values():
            t.cancel()

    async def _supervisor(self):
        while True:
            try:
                self._reconcile()
            except Exception as e:
                print(f"[monitor] 调度错误: {e}", flush=True)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=10)
            except TimeoutError:
                pass
            self.wake.clear()

    def _reconcile(self):
        want = {d["id"]: d for d in db.list_domains(enabled_only=True)}
        for did, d in want.items():
            h = f"{d['name']}|{d['rrtype']}|{d['resolver']}|{d['interval_sec']}"
            t = self.tasks.get(did)
            if t and not t.done() and self.cfg_hash.get(did) == h:
                continue
            if t:
                t.cancel()
            self.tasks[did] = asyncio.create_task(self._worker(did))
            self.cfg_hash[did] = h
        for did in list(self.tasks):
            if did not in want:
                self.tasks.pop(did).cancel()
                self.cfg_hash.pop(did, None)
                self.fail_state.pop(did, None)

    async def _worker(self, domain_id: int):
        while True:
            d = db.get_domain(domain_id)
            if not d or not d["enabled"]:
                return
            try:
                await self.run_check(domain_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[monitor] {d['name']} 检查异常: {e}", flush=True)
            await asyncio.sleep(d["interval_sec"] + random.uniform(0, 3))

    async def run_check(self, domain_id: int) -> dict:
        d = db.get_domain(domain_id)
        if not d:
            return {"status": "error", "ip": None, "rtt": None, "error": "监控项不存在"}
        res = await resolve_once(d["name"], d["rrtype"], d["resolver"])
        db.add_check(domain_id, res["status"], res["ip"], res["rtt"], res["error"])
        await self._handle(d, res)
        return res

    async def _handle(self, d, res):
        now = int(time.time())
        st = self.fail_state.setdefault(d["id"], {"fails": 0, "alerted": False, "last_alert": 0})
        if res["status"] == "ok":
            st["fails"] = 0
            prev = db.current_ip(d["id"])
            if prev != res["ip"]:
                db.add_change(d["id"], prev, res["ip"])
                print(f"[change] {d['name']}: {prev} -> {res['ip']}", flush=True)
                conf = self._tg_conf()
                if prev is not None and conf["on"] and conf["on_change"]:
                    await self._send(
                        f"DDNS 解析变动\n域名: {d['name']} ({d['rrtype']})\n"
                        f"时间: {fmt_short(now)}\n旧 IP: {prev}\n新 IP: {res['ip']}"
                    )
                if st["alerted"]:
                    await self._send(
                        f"解析恢复正常\n域名: {d['name']} ({d['rrtype']})\n当前 IP: {res['ip']}"
                    )
                    st["alerted"] = False
        else:
            st["fails"] += 1
            conf = self._tg_conf()
            if conf["on"] and conf["on_fail"] and st["fails"] >= FAIL_THRESHOLD:
                if not st["alerted"] or now - st["last_alert"] >= conf["cooldown_min"] * 60:
                    await self._send(
                        f"解析持续失败\n域名: {d['name']} ({d['rrtype']})\n"
                        f"错误: {res['error'] or res['status']}\n已连续失败 {st['fails']} 次"
                    )
                    st["alerted"] = True
                    st["last_alert"] = now

    @staticmethod
    def _tg_conf() -> dict:
        return {
            "on": db.get_setting("tg_enabled") == "1",
            "on_change": db.get_setting("notify_on_change", "1") == "1",
            "on_fail": db.get_setting("notify_on_fail", "1") == "1",
            "token": db.get_setting("tg_token", ""),
            "chat": db.get_setting("tg_chat", ""),
            "cooldown_min": to_int(db.get_setting("fail_cooldown_min", "60"), 60),
        }

    async def _send(self, text: str):
        conf = self._tg_conf()
        if not (conf["token"] and conf["chat"]):
            return
        ok, info = await send_tg(conf["token"], conf["chat"], text)
        if not ok:
            print(f"[notify] Telegram 发送失败: {info}", flush=True)
