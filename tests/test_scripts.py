"""部署环境中的页面门禁必须与生产数据隔离。"""
import os
from pathlib import Path
import subprocess
import sys


def test_check_pages_ignores_production_data_dir(tmp_path):
    production = tmp_path / "production"
    production.mkdir(mode=0o755)
    database = production / "ddns.db"
    database.write_bytes(b"production sentinel: must never be opened as SQLite")
    database.chmod(0o644)
    before = (production.stat().st_mode, database.stat().st_mode,
              database.stat().st_mtime_ns)
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts/check_pages.py")], cwd=root,
        env={**os.environ, "DDNS_DATA_DIR": str(production)},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "5 页" in result.stdout
    assert list(production.iterdir()) == [database]
    assert database.read_bytes() == b"production sentinel: must never be opened as SQLite"
    assert (production.stat().st_mode, database.stat().st_mode,
            database.stat().st_mtime_ns) == before
