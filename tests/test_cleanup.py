import asyncio
import threading
import time

import httpx
import pytest

from app import main


def test_cleanup_thread_keeps_login_responsive_and_finishes_on_cancel(private_auth_db, monkeypatch):
    module = private_auth_db
    did = module.create_domain("slow-cleanup.example", "A", "1.1.1.1", 300, "test")
    module.set_setting("retention_days", "1")
    c = module.conn()
    c.executemany("INSERT INTO checks (domain_id, ts, status) VALUES (?, ?, 'ok')",
                  [(did, 1_000_000_000 + i) for i in range(20000)])
    c.execute("INSERT INTO check_hourly VALUES (?, ?, 20000, 0)",
              (did, module.local_hour_ts(1_000_000_000)))
    c.execute("CREATE TRIGGER slow_delete BEFORE DELETE ON checks "
              "BEGIN SELECT pause_cleanup(); END")
    c.commit()
    started = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    worker_threads = []
    connections = []
    open_connection = module._open_connection
    cleanup_checks = module.cleanup_checks

    def pause_cleanup():
        if not started.is_set():
            started.set()
            # 失败的实现也会自行退出，避免测试回归时把事件循环永久挂死。
            release.wait(4)
        return 0

    def tracked_connection(**kwargs):
        connection = open_connection(**kwargs)
        assert connection is not c
        assert connection.row_factory is module.sqlite3.Row
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        connection.create_function("pause_cleanup", 0, pause_cleanup)
        connections.append(connection)
        return connection

    def tracked_cleanup(days):
        worker_threads.append(threading.get_ident())
        try:
            cleanup_checks(days)
        finally:
            if connections:
                # 在创建连接的线程验证 close，避免线程检查掩盖未关闭连接的问题。
                with pytest.raises(module.sqlite3.ProgrammingError, match="closed database"):
                    connections[-1].execute("SELECT 1")
            closed.set()

    monkeypatch.setattr(module, "_open_connection", tracked_connection)
    monkeypatch.setattr(module, "cleanup_checks", tracked_cleanup)

    async def scenario():
        begin = time.perf_counter()
        task = asyncio.create_task(main._cleanup_loop())
        try:
            assert await asyncio.to_thread(started.wait, 2)
            assert len(worker_threads) == 1
            assert worker_threads[0] != threading.get_ident()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                         base_url="https://testserver") as client:
                response = await client.get("/login")
            assert response.status_code == 200
            # 在真实 DELETE 的写事务中暂停，宽松阈值仍能识别事件循环被阻塞。
            assert time.perf_counter() - begin < 1.5
            assert not closed.is_set()
            assert c.execute("SELECT COUNT(*) FROM checks").fetchone()[0] == 20000
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done()
        finally:
            release.set()
            if not task.cancelling():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert closed.is_set()
        assert task.cancelled()

    asyncio.run(scenario())
    assert c.execute("SELECT COUNT(*) FROM checks").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM check_hourly").fetchone()[0] == 0
    assert not c.in_transaction


@pytest.mark.parametrize("body_fails", [False, True])
def test_lifespan_awaits_cleanup_cancellation(monkeypatch, body_fails):
    events = []

    class Monitor:
        async def start(self):
            events.append("monitor started")

        async def stop(self):
            events.append("monitor stopped")

    async def cleanup_loop():
        events.append("cleanup started")
        try:
            await asyncio.Future()
        finally:
            # 异步收尾必须在 lifespan 返回前完成。
            await asyncio.sleep(0)
            events.append("cleanup finished")

    monkeypatch.setattr(main.db, "init", lambda: None)
    monkeypatch.setattr(main, "monitor", Monitor())
    monkeypatch.setattr(main, "_cleanup_loop", cleanup_loop)

    async def scenario():
        try:
            async with main.lifespan(main.app):
                await asyncio.sleep(0)
                assert "cleanup started" in events
                if body_fails:
                    raise RuntimeError("body failed")
        except RuntimeError as error:
            assert body_fails and str(error) == "body failed"
        assert events == ["monitor started", "cleanup started", "monitor stopped", "cleanup finished"]

    asyncio.run(scenario())
