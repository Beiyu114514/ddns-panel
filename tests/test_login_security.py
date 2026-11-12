import asyncio
import ipaddress
from pathlib import Path
import threading
import time
from time import perf_counter

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import db, main


@pytest.fixture(autouse=True)
def isolated_login_state(monkeypatch):
    monkeypatch.setattr(main, "LOGIN_FAILS", {})
    monkeypatch.setattr(main, "SCRYPT_SEMAPHORE", asyncio.Semaphore(4))


def _must_not_run_scrypt(*args):
    pytest.fail("非法输入不应执行 scrypt")


def _signed_client():
    client = TestClient(main.app, base_url="https://testserver")
    exp = int(time.time()) + 3600
    client.cookies.set(main.COOKIE, f"{exp}.{main._sign(exp)}")
    client.get("/settings")
    return client


def test_long_login_password_is_rejected_before_scrypt(monkeypatch):
    monkeypatch.setattr(main.db, "verify_password", _must_not_run_scrypt)
    client = TestClient(main.app, base_url="https://testserver")
    client.get("/login")
    start = perf_counter()
    response = client.post("/login", data={"password": "x" * 1025})
    elapsed = perf_counter() - start
    assert response.status_code == 400
    assert "密码不能超过 1024 位" in response.text
    assert elapsed < 0.1
    assert main.LOGIN_FAILS == {}
    print(f"超长密码登录：{elapsed * 1000:.2f} ms，未执行 scrypt")


@pytest.mark.parametrize("length, status", [(str(10 ** 9), 413), ("-1", 400), ("invalid", 400)])
def test_login_rejects_invalid_content_length_before_reading_body(monkeypatch, length, status):
    async def no_form(*args, **kwargs):
        pytest.fail("应在解析表单前拒绝 Content-Length")

    monkeypatch.setattr(Request, "form", no_form)
    client = TestClient(main.app)
    response = client.post("/login", headers={"Content-Length": length})
    assert response.status_code == status


def test_chunked_long_password_does_not_run_scrypt(monkeypatch):
    monkeypatch.setattr(main.db, "verify_password", _must_not_run_scrypt)

    async def chunks():
        yield b"password=" + b"x" * 512
        yield b"x" * 513

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                    base_url="https://testserver") as client:
            response = await client.post("/login", content=chunks(), headers={
                "Content-Type": "application/x-www-form-urlencoded",
            })
        assert "content-length" not in response.request.headers
        assert response.status_code == 400
        assert "密码不能超过 1024 位" in response.text

    asyncio.run(run())


def test_global_scrypt_limit_with_eight_distributed_logins(monkeypatch):
    verify = main.db.verify_password
    release = threading.Event()
    counter_lock = threading.Lock()
    active, peak, calls = 0, 0, 0

    def tracked_verify(*args):
        nonlocal active, peak, calls
        with counter_lock:
            active += 1
            calls += 1
            peak = max(peak, active)
        try:
            # 固定四个在途运算，让后续请求必定遇到满额，而非依赖机器速度。
            assert release.wait(3)
            return verify(*args)
        finally:
            with counter_lock:
                active -= 1

    monkeypatch.setattr(main.db, "verify_password", tracked_verify)

    async def post(i):
        transport = httpx.ASGITransport(app=main.app, client=(f"203.0.113.{i + 1}", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            return await client.post("/login", data={"password": "wrong-password"})

    async def run():
        tasks = [asyncio.create_task(post(i)) for i in range(8)]
        try:
            done, pending = await asyncio.wait(tasks, timeout=0.3)
            assert len(done) == len(pending) == 4
            assert all(task.result().status_code == 429 for task in done)
            assert all(task.result().headers["Retry-After"] == "60" for task in done)
            assert len(main.LOGIN_FAILS) == 4
        finally:
            release.set()
            results = await asyncio.gather(*tasks)
        assert sorted(r.status_code for r in results) == [400] * 4 + [429] * 4
        assert calls == peak == 4
        assert active == 0
        # 名额应全部回收，正常登录路径仍能继续执行。
        assert (await post(8)).status_code == 400
        print("8 路分布式并发：4 个 400、4 个 429；实际 scrypt 峰值 4，无 500")

    asyncio.run(run())


def test_cancelled_request_holds_slot_until_thread_finishes(monkeypatch):
    release = threading.Event()
    started = threading.Event()
    monkeypatch.setattr(main, "SCRYPT_SEMAPHORE", asyncio.Semaphore(1))

    def work():
        started.set()
        assert release.wait(3)
        return True

    async def run():
        task = asyncio.create_task(main._run_scrypt(work))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            with pytest.raises(main._ScryptBusy):
                await main._run_scrypt(lambda: True)
        finally:
            release.set()
            # 等实际线程归还名额，避免事件循环退出时取消它而掩盖断言。
            async def wait_for_release():
                while main.SCRYPT_SEMAPHORE.locked():
                    await asyncio.sleep(0.001)
            await asyncio.wait_for(wait_for_release(), 1)
        assert await main._run_scrypt(lambda: True)

    asyncio.run(run())


def test_ip_rate_limit_has_retry_after_and_preserves_concurrent_login():
    password = (Path(main.db.DATA_DIR) / "initial_password.txt").read_text().strip()

    async def run():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            responses = await asyncio.gather(*[
                client.post("/login", data={"password": "wrong-password"}) for _ in range(8)
            ])
            assert all(r.status_code in (400, 429) for r in responses)
            # 全局满额拒绝的请求不计作密码失败，补齐五次实际失败再验证 IP 锁定。
            for _ in range(max(0, 5 - sum(r.status_code == 400 for r in responses))):
                await client.post("/login", data={"password": "wrong-password"})
            locked = await client.post("/login", data={"password": password})
            assert locked.status_code == 429
            assert locked.headers["Retry-After"] == "60"
            main.LOGIN_FAILS.clear()
            valid = await asyncio.gather(*[
                client.post("/login", data={"password": password}) for _ in range(4)
            ])
            assert all(r.status_code == 303 and r.headers["location"] == "/" for r in valid)

    asyncio.run(run())


@pytest.mark.parametrize("password, password2, message", [
    ("1234567", "1234567", "密码至少 8 位"),
    ("x" * 1025, "x" * 1025, "密码不能超过 1024 位"),
    ("12345678", "x" * 1025, "密码不能超过 1024 位"),
    ("12345678", "87654321", "两次输入的密码不一致"),
])
def test_invalid_new_password_skips_scrypt(private_auth_db, monkeypatch,
                                         password, password2, message):
    monkeypatch.setattr(main.db, "hash_password", _must_not_run_scrypt)
    client = _signed_client()
    response = client.post("/settings/password", data={
        "_csrf": client.cookies.get("ddns_csrf"),
        "current_password": (Path(private_auth_db.DATA_DIR) / "initial_password.txt").read_text().strip(),
        "password": password, "password2": password2,
    })
    assert response.status_code == 400
    assert message in response.text


def test_eight_character_new_password_and_legacy_login(private_auth_db):
    private_auth_db.set_setting("password_hash", private_auth_db.hash_password("123456"))
    client = TestClient(main.app, base_url="https://testserver")
    assert client.post("/login", data={"password": "123456"}, follow_redirects=False).status_code == 303
    old_secret = private_auth_db.get_setting("auth_secret")
    client.get("/settings")
    response = client.post("/settings/password", data={
        "_csrf": client.cookies.get("ddns_csrf"),
        "current_password": "123456",
        "password": "12345678", "password2": "12345678",
    }, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert private_auth_db.get_setting("auth_secret") != old_secret
    assert client.get("/settings", follow_redirects=False).status_code == 303
    assert client.post("/login", data={"password": "123456"}, follow_redirects=False).status_code == 400
    assert client.post("/login", data={"password": "12345678"}, follow_redirects=False).status_code == 303


def test_password_maximum_boundary_and_frontend_constraints(private_auth_db):
    client = _signed_client()
    settings = client.get("/settings").text
    assert settings.count('minlength="8" maxlength="1024"') == 2
    login = TestClient(main.app).get("/login").text
    assert 'maxlength="1024"' in login
    assert 'minlength=' not in login
    password = "密" * 1024
    response = client.post("/settings/password", data={
        "_csrf": client.cookies.get("ddns_csrf"),
        "current_password": (Path(private_auth_db.DATA_DIR) / "initial_password.txt").read_text().strip(),
        "password": password, "password2": password,
    }, follow_redirects=False)
    assert response.status_code == 303
    assert client.post("/login", data={"password": password}, follow_redirects=False).status_code == 303


def test_password_change_shares_global_scrypt_limit(private_auth_db, monkeypatch):
    monkeypatch.setattr(main.db, "hash_password", _must_not_run_scrypt)
    monkeypatch.setattr(main, "SCRYPT_SEMAPHORE", asyncio.Semaphore(0))
    old_hash = private_auth_db.get_setting("password_hash")
    client = _signed_client()
    response = client.post("/settings/password", data={
        "_csrf": client.cookies.get("ddns_csrf"),
        "current_password": (Path(private_auth_db.DATA_DIR) / "initial_password.txt").read_text().strip(),
        "password": "12345678", "password2": "12345678",
    })
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    assert private_auth_db.get_setting("password_hash") == old_hash


@pytest.mark.parametrize("hops, xff, expected", [
    (2, "203.0.113.7", "172.17.0.1"),
    (2, "203.0.113.7, 9.9.9.9", "172.17.0.1"),
    (1, "not-an-ip", "172.17.0.1"),
    (2, "garbage, 104.16.0.1", "172.17.0.1"),
    (2, "999.1.2.3, 172.18.0.2", "172.17.0.1"),
    (2, "[2001:db8::1], 172.18.0.2", "172.17.0.1"),
    (1, "203.0.113.7", "203.0.113.7"),
    (2, "203.0.113.7, 104.16.0.1", "203.0.113.7"),
    (1, "2001:db8::7", "2001:db8::7"),
    (2, "2001:db8::7, 172.18.0.2", "2001:db8::7"),
])
def test_xff_client_address_validation(monkeypatch, hops, xff, expected):
    monkeypatch.setattr(main, "TRUSTED_PROXIES", [ipaddress.ip_network("172.16.0.0/12")])
    monkeypatch.setattr(main, "TRUSTED_PROXY_HOPS", hops)
    request = Request({
        "type": "http", "client": ("172.17.0.1", 12345),
        "headers": [(b"x-forwarded-for", xff.encode())],
    })
    assert main._client_ip(request) == expected
