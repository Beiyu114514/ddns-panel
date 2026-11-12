import importlib.util
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 必须在导入 app 之前固定数据目录，避免测试碰到真实 data/
os.environ["DDNS_DATA_DIR"] = tempfile.mkdtemp(prefix="ddns-test-")

import pytest  # noqa: E402

from app import db  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def initialized_db():
    db.init()
    yield


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    # 不进入 with 上下文：跳过 lifespan，测试里不启动后台解析器/清理任务。
    # 会话 cookie 带 Secure，测试客户端必须走 https 才会回传它。
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def auth_client(client):
    path = os.path.join(db.DATA_DIR, "initial_password.txt")
    with open(path, encoding="utf-8") as f:
        password = f.read().strip()
    resp = client.post("/login", data={"password": password}, follow_redirects=False)
    assert resp.status_code == 303
    client.get("/settings")  # 与浏览器一样，先取表单页建立 CSRF cookie。
    return client


@pytest.fixture
def private_auth_db(tmp_path, monkeypatch):
    from app import main

    # 改密会删除初始密码并轮换会话密钥，独立库避免污染整个测试会话。
    monkeypatch.setenv("DDNS_DATA_DIR", str(tmp_path / "auth-data"))
    spec = importlib.util.spec_from_file_location("isolated_auth_db", db.__file__)
    isolated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(isolated)
    isolated.init()
    monkeypatch.setattr(main, "db", isolated)
    yield isolated
    isolated.conn().close()
