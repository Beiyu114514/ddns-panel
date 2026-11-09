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
RECENT_CHECK_SEC = 5  # 路由同步首检后，worker 在该窗口内不再重复解析
SEND_QUEUE_MAX = 1000  # Telegram 长时间不可达时限制积压，避免队列无界增长


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
        self.sender = None
        self.tasks: dict[int, asyncio.Task] = {}
        self.cfg_hash: dict[int, tuple] = {}
        self.fail_state: dict[int, dict] = {}
        # 锁对象只增不删：删掉一把仍有等待者的 asyncio.Lock 会让新检查拿到另一把锁，
        # 与等待者并发执行；单个锁开销很小，按域名保留即可
        self.locks: dict[int, asyncio.Lock] = {}
        self.recent_check: dict[int, float] = {}
        self.send_q: asyncio.Queue = asyncio.Queue(maxsize=SEND_QUEUE_MAX)
        self.wake = asyncio.Event()

    def poke(self):
        self.wake.set()

    async def start(self):
        self.sup = asyncio.create_task(self._supervisor())
        self.sender = asyncio.create_task(self._sender())

    async def stop(self):
        tasks = [t for t in (self.sup, self.sender) if t is not None]
        tasks.extend(self.tasks.values())
        for t in tasks:
            t.cancel()
        # 必须等被取消的任务真正退出：发送中的消息要靠 CancelledError 分支
        # 回滚状态，只 cancel 不 await 的话事件循环可能先关掉
        await asyncio.gather(*tasks, return_exceptions=True)
        # 队列里还没开始发送的消息：回滚状态，重启后由 DB 中的未通知记录补发
        while not self.send_q.empty():
            item = self.send_q.get_nowait()
            item[2]()

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
            h = (d["name"], d["rrtype"], d["resolver"], d["interval_sec"])
            t = self.tasks.get(did)
            old = self.cfg_hash.get(did)
            if t and not t.done() and old == h:
                continue
            if t:
                t.cancel()
            if old is not None and old != h:
                # 配置变了：新 worker 要立即按新配置检查，不能被旧配置的"刚检查过"拦住
                self.recent_check.pop(did, None)
            if old is not None and old[1:3] != h[1:3]:
                # 记录类型/解析器变了：旧配置的失败计数与告警状态不能带入新配置，
                # 否则切换后第一次失败就会凑满阈值、首次成功还会误发"恢复"
                self.fail_state.pop(did, None)
            self.tasks[did] = asyncio.create_task(self._worker(did))
            self.cfg_hash[did] = h
        for did in list(self.tasks):
            if did not in want:
                self.tasks.pop(did).cancel()
                self.cfg_hash.pop(did, None)
        # 停用/删除的监控项不再有 worker，状态条目不能留在内存里
        for did in list(self.fail_state):
            if did not in want:
                self.fail_state.pop(did, None)
        for did in list(self.recent_check):
            if did not in want:
                self.recent_check.pop(did, None)

    async def _worker(self, domain_id: int):
        while True:
            d = db.get_domain(domain_id)
            if not d or not d["enabled"]:
                return
            try:
                await self.run_check(domain_id, skip_if_recent=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[monitor] {d['name']} 检查异常: {e}", flush=True)
            await asyncio.sleep(d["interval_sec"] + random.uniform(0, 3))

    async def run_check(self, domain_id: int, skip_if_recent: bool = False) -> dict:
        # 页面触发与调度 worker 可能同时检查同一域名（多 IP 记录两次结果可能不同），
        # 按域名加锁串行化：并发检查会互相覆盖基线、写出假变动。
        # 锁只覆盖"解析 + 落库"，通知交给后台队列发送，避免 Telegram 超时拖住手动检查。
        async with self.locks.setdefault(domain_id, asyncio.Lock()):
            if skip_if_recent and \
                    time.monotonic() - self.recent_check.get(domain_id, 0.0) < RECENT_CHECK_SEC:
                # 创建域名时路由已同步检查过，worker 启动后不要紧接着再解析一次
                return {"status": "skipped", "ip": None, "rtt": None, "error": None}
            d = db.get_domain(domain_id)
            if not d:
                return {"status": "error", "ip": None, "rtt": None, "error": "监控项不存在"}
            if not d["enabled"]:
                return {"status": "error", "ip": None, "rtt": None, "error": "监控项已停用"}
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
            self.recent_check[domain_id] = time.monotonic()
            # 在锁内入队（put_nowait 不阻塞）：_handle 已推进状态，若此处被取消，
            # 消息既没入队也不会回滚，告警就永久丢了
            for item in notify:
                try:
                    self.send_q.put_nowait(item)
                except asyncio.QueueFull:
                    # 积压过多（Telegram 长时间不可达）：回滚状态，等下次检查重试
                    item[2]()
        return res

    async def _sender(self):
        while True:
            text, kind, rollback, commit = await self.send_q.get()
            try:
                ok = await self._send(text, kind=kind)
            except asyncio.CancelledError:
                # 发送被取消时消息未发出，回滚状态等下次检查重试
                rollback()
                raise
            except Exception as e:
                print(f"[notify] 发送异常: {e}", flush=True)
                ok = False
            if ok:
                if commit is not None:
                    try:
                        commit()
                    except Exception as e:
                        # 已送达但落库失败：宁可下次重复通知，也不能丢掉
                        print(f"[notify] 通知状态落库失败: {e}", flush=True)
            else:
                rollback()

    async def _handle(self, d, res) -> list[tuple]:
        """更新状态并返回待发送的消息。

        发送由后台队列执行。状态在入队时即推进，这样 Telegram 不可达时
        冷却与告警标记仍然生效——否则每次检查都会重试并各阻塞一次 15s 超时。
        发送失败或进程退出时由 rollback 恢复状态，下次检查可重新告警。
        被总开关/分类开关关闭时不推进状态，避免"从未发出的告警被当作已通知"。
        """
        now = int(time.time())
        pending: list[tuple] = []
        st = self.fail_state.setdefault(
            d["id"], {"fails": 0, "alerted": False, "last_alert": 0})
        if res["status"] == "ok":
            st["fails"] = 0
            prev = db.current_ip(d["id"])
            if prev != res["ip"]:
                # prev 为空表示首次建立基线，不需要通知；其余变动以 notified=0
                # 落库，进程重启后仍能从这里补发
                db.add_change(d["id"], prev, res["ip"], notify=prev is not None)
                print(f"[change] {d['name']}: {prev} -> {res['ip']}", flush=True)
            self._queue_change(d, st, now, pending)
            # 上一条告警/恢复还在队列里时先不排恢复：等它发送成功再排，
            # 否则失败告警未送达、恢复却先发出去，状态会互相覆盖
            if st["alerted"] and not st.get("alert_pending") \
                    and self._deliverable("recover"):
                # 通知关闭时不能清 alerted：否则恢复事件被吞掉，重新开启后无法补发
                st["alerted"] = False
                st["alert_pending"] = True
                st["alert_seq"] = st.get("alert_seq", 0) + 1
                seq = st["alert_seq"]

                def rollback_recover():
                    # 只有状态仍属于本条消息时才回滚，避免旧消息覆盖新检查
                    if st.get("alert_seq") == seq and st.get("alert_pending"):
                        st["alert_pending"] = False
                        st["alerted"] = True

                def commit_recover():
                    if st.get("alert_seq") == seq:
                        st["alert_pending"] = False

                pending.append((
                    f"解析恢复正常\n域名: {d['name']} ({d['rrtype']})\n当前 IP: {res['ip']}",
                    "recover", rollback_recover, commit_recover))
        else:
            # 变动是已经发生的事实，不应被当前解析失败阻塞；先补发再判断告警
            self._queue_change(d, st, now, pending)
            st["fails"] += 1
            if st["fails"] >= FAIL_THRESHOLD and not st.get("alert_pending") \
                    and self._deliverable("fail"):
                if not st["alerted"] or now - st["last_alert"] >= \
                        self._tg_conf()["cooldown_min"] * 60:
                    prev_alerted, prev_last_alert = st["alerted"], st["last_alert"]
                    st["alerted"] = True
                    st["last_alert"] = now
                    st["alert_pending"] = True
                    st["alert_seq"] = st.get("alert_seq", 0) + 1
                    seq = st["alert_seq"]

                    def rollback_fail():
                        if st.get("alert_seq") == seq and st.get("alert_pending"):
                            st["alert_pending"] = False
                            st["alerted"] = prev_alerted
                            st["last_alert"] = prev_last_alert

                    def commit_fail():
                        if st.get("alert_seq") == seq:
                            st["alert_pending"] = False

                    pending.append((
                        f"解析持续失败\n域名: {d['name']} ({d['rrtype']})\n"
                        f"错误: {res['error'] or res['status']}\n已连续失败 {st['fails']} 次",
                        "fail", rollback_fail, commit_fail))
        return pending

    def _queue_change(self, d, st, now: int, pending: list[tuple]):
        """把 DB 里未通知的变动合并成一条消息入队。

        以 DB 为准而不是内存：进程重启后 notified=0 的记录仍然有效；
        连续变动合并为"最早的旧 IP → 最新的新 IP"，不会把中间 IP 当成旧值。
        """
        pend = db.pending_change(d["id"])
        if not pend:
            return
        conf = self._tg_conf()
        if not (conf["on"] and conf["on_change"]):
            # 用户主动关闭：不积压，否则重新开启后会补发很久以前的变动
            db.mark_changes_notified(d["id"])
            return
        if not (conf["token"] and conf["chat"]):
            # 只是凭据没填完整：保留 notified=0，补全后仍可补发
            return
        if st.get("change_pending"):
            # 上一条变动消息还在队列/发送中，DB 里仍是 notified=0；
            # 此时再次入队会重复发送，等它 commit/rollback 后再处理
            return
        if now - st.get("last_change_notify", 0) < conf["change_cooldown_min"] * 60:
            return
        p_old, p_new, p_ts, last_id = pend
        prev_lcn = st.get("last_change_notify", 0)
        st["last_change_notify"] = now
        st["change_pending"] = True
        st["change_seq"] = st.get("change_seq", 0) + 1
        seq = st["change_seq"]

        def rollback_change():
            # 只在状态仍属于本条消息时回滚，避免覆盖后续检查的新状态
            if st.get("change_seq") == seq:
                st["last_change_notify"] = prev_lcn
                st["change_pending"] = False

        def commit_change():
            if st.get("change_seq") == seq:
                st["change_pending"] = False
            db.mark_changes_notified(d["id"], last_id)

        pending.append((
            f"DDNS 解析变动\n域名: {d['name']} ({d['rrtype']})\n"
            f"时间: {fmt_short(p_ts)}\n旧 IP: {p_old}\n新 IP: {p_new}",
            "change", rollback_change, commit_change))

    @staticmethod
    def _deliverable(kind: str) -> bool:
        """当前配置下这类消息是否处于启用状态。关闭时不应推进告警/冷却状态，
        否则重新开启后会出现"从未发出的告警被当作已通知"。"""
        conf = Monitor._tg_conf()
        if not (conf["on"] and conf["token"] and conf["chat"]):
            return False
        if kind == "change":
            return conf["on_change"]
        if kind in ("fail", "recover"):
            # 恢复通知是失败告警的收尾，失败通知关闭时不应单独发送
            return conf["on_fail"]
        return True

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
        """返回是否已发出。入队后配置被关闭时按未发送处理并回滚状态。"""
        if not self._deliverable(kind):
            return False
        conf = self._tg_conf()
        ok, info = await send_tg(conf["token"], conf["chat"], text)
        if not ok:
            print(f"[notify] Telegram 发送失败: {info}", flush=True)
            return False
        return True
