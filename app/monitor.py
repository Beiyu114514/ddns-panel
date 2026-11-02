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
        self.locks: dict[int, asyncio.Lock] = {}
        self.pending_unlock: set[int] = set()
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
                lk = self.locks.get(did)
                # 有检查正在持锁时不丢弃它，否则下一次手动检查会拿到新锁并发执行；
                # 但条目已从 tasks 移除，之后不会再被遍历到，需登记到待回收集合
                if lk is not None and lk.locked():
                    self.pending_unlock.add(did)
                else:
                    self.locks.pop(did, None)
        self._reap_locks()

    def _reap_locks(self):
        for did in list(self.pending_unlock):
            lk = self.locks.get(did)
            if lk is None or not lk.locked():
                self.locks.pop(did, None)
                self.pending_unlock.discard(did)

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
        # 页面触发与调度 worker 可能同时检查同一域名（多 IP 记录两次结果可能不同），
        # 按域名加锁串行化：并发检查会互相覆盖基线、写出假变动。
        # 锁只覆盖"解析 + 落库"，通知在锁外发送，避免 Telegram 超时拖住手动检查。
        notify: list[tuple[str, str, callable]] = []
        async with self.locks.setdefault(domain_id, asyncio.Lock()):
            d = db.get_domain(domain_id)
            if not d:
                return {"status": "error", "ip": None, "rtt": None, "error": "监控项不存在"}
            snap = (d["rrtype"], d["resolver"], d["enabled"])
            res = await resolve_once(d["name"], d["rrtype"], d["resolver"])
            now_d = db.get_domain(domain_id)
            if not now_d:
                return {"status": "error", "ip": None, "rtt": None, "error": "监控项已删除"}
            if (now_d["rrtype"], now_d["resolver"], now_d["enabled"]) != snap:
                # 解析期间配置变了（典型：切换 A/AAAA 触发基线重置），旧类型的
                # 结果不能当成基线写入；但检查本身发生了，如实记录以免成功率统计缺口
                db.add_check(domain_id, res["status"], res["ip"], res["rtt"], res["error"])
                return {"status": "error", "ip": None, "rtt": None,
                        "error": "检查期间配置已变更，本次结果未计入基线"}
            db.add_check(domain_id, res["status"], res["ip"], res["rtt"], res["error"])
            notify = await self._handle(d, res)
        for text, kind, rollback in notify:
            if not await self._send(text, kind=kind):
                # 仅"实际发送失败"回滚：Telegram 故障时恢复通知不应就此丢失；
                # 被开关关闭时不入队，因此不涉及回滚
                rollback()
        return res

    async def _handle(self, d, res) -> list[tuple[str, str, callable]]:
        """更新状态并返回待发送的消息。

        发送在锁外由 run_check 执行。状态在入队时即推进，这样 Telegram 不可达时
        冷却与告警标记仍然生效——否则每次检查都会重试并各阻塞一次 15s 超时。
        被总开关/分类开关关闭时不推进状态，避免"从未发出的告警被当作已通知"。
        """
        now = int(time.time())
        pending: list[tuple[str, str, callable]] = []
        st = self.fail_state.setdefault(
            d["id"], {"fails": 0, "alerted": False, "last_alert": 0, "last_try": 0})
        st["last_try"] = now
        if res["status"] == "ok":
            st["fails"] = 0
            prev = db.current_ip(d["id"])
            if prev != res["ip"]:
                db.add_change(d["id"], prev, res["ip"])
                print(f"[change] {d['name']}: {prev} -> {res['ip']}", flush=True)
                if prev is not None and self._deliverable("change") and \
                        now - st.get("last_change_notify", 0) >= \
                        self._tg_conf()["change_cooldown_min"] * 60:
                    st["last_change_notify"] = now
                    pending.append((
                        f"DDNS 解析变动\n域名: {d['name']} ({d['rrtype']})\n"
                        f"时间: {fmt_short(now)}\n旧 IP: {prev}\n新 IP: {res['ip']}",
                        "change", lambda: None))
            if st["alerted"]:
                st["alerted"] = False
                if self._deliverable("recover"):
                    pending.append((
                        f"解析恢复正常\n域名: {d['name']} ({d['rrtype']})\n当前 IP: {res['ip']}",
                        "recover", lambda: st.__setitem__("alerted", True)))
        else:
            st["fails"] += 1
            if st["fails"] >= FAIL_THRESHOLD and self._deliverable("fail"):
                if not st["alerted"] or now - st["last_alert"] >= \
                        self._tg_conf()["cooldown_min"] * 60:
                    st["alerted"] = True
                    st["last_alert"] = now
                    pending.append((
                        f"解析持续失败\n域名: {d['name']} ({d['rrtype']})\n"
                        f"错误: {res['error'] or res['status']}\n已连续失败 {st['fails']} 次",
                        "fail", lambda: None))
        return pending

    @staticmethod
    def _deliverable(kind: str) -> bool:
        """当前配置下这类消息是否处于启用状态。关闭时不应推进告警/冷却状态，
        否则重新开启后会出现"从未发出的告警被当作已通知"。"""
        conf = Monitor._tg_conf()
        if not (conf["on"] and conf["token"] and conf["chat"]):
            return False
        return kind != "change" or conf["on_change"]

    @staticmethod
    def _tg_conf() -> dict:
        return {
            "on": db.get_setting("tg_enabled") == "1",
            "on_change": db.get_setting("notify_on_change", "1") == "1",
            "on_fail": db.get_setting("notify_on_fail", "1") == "1",
            "token": db.get_setting("tg_token", ""),
            "chat": db.get_setting("tg_chat", ""),
            "cooldown_min": to_int(db.get_setting("fail_cooldown_min", "60"), 60),
            # 0 = 不限制，与旧版"每次变动都通知"的行为一致
            "change_cooldown_min": to_int(db.get_setting("change_cooldown_min", "0"), 0),
        }

    async def _send(self, text: str, kind: str = "") -> bool:
        """是否发出/无需发出。开关与冷却已在 _handle 判定，这里只兜底配置缺失。"""
        conf = self._tg_conf()
        if not (conf["on"] and conf["token"] and conf["chat"]):
            return True
        ok, info = await send_tg(conf["token"], conf["chat"], text)
        if not ok:
            print(f"[notify] Telegram 发送失败: {info}", flush=True)
            return False
        return True
