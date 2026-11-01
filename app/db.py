import hashlib
import hmac
import os
import secrets
import sqlite3
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA_DIR, "ddns.db")

_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domains (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  name         TEXT NOT NULL,
  rrtype       TEXT NOT NULL DEFAULT 'A',
  resolver     TEXT NOT NULL DEFAULT '223.5.5.5',
  interval_sec INTEGER NOT NULL DEFAULT 300,
  enabled      INTEGER NOT NULL DEFAULT 1,
  note         TEXT NOT NULL DEFAULT '',
  created_at   INTEGER NOT NULL,
  UNIQUE (name, rrtype)
);
CREATE TABLE IF NOT EXISTS checks (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  ts        INTEGER NOT NULL,
  status    TEXT NOT NULL,
  ip        TEXT,
  rtt_ms    REAL,
  error     TEXT
);
CREATE INDEX IF NOT EXISTS idx_checks_dom_ts ON checks (domain_id, ts);
CREATE TABLE IF NOT EXISTS changes (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  ts        INTEGER NOT NULL,
  old_ip    TEXT,
  new_ip    TEXT
);
CREATE INDEX IF NOT EXISTS idx_changes_dom_ts ON changes (domain_id, ts);
"""


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=5000")
    return _conn


def init():
    c = conn()
    c.executescript(SCHEMA)
    c.commit()
    if not get_setting("auth_secret"):
        set_setting("auth_secret", secrets.token_hex(32))
    if not get_setting("password_hash"):
        pw = secrets.token_urlsafe(6)
        set_setting("password_hash", hash_password(pw))
        path = os.path.join(DATA_DIR, "initial_password.txt")
        with open(path, "w") as f:
            f.write(pw + "\n")
        os.chmod(path, 0o600)
        print(f"[setup] 初始密码: {pw} （已写入 {path}，登录后请修改）", flush=True)


def get_setting(key: str, default=None):
    row = conn().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value):
    conn().execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn().commit()


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return f"{salt.hex()}${h.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$", 1)
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32)
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


# ---------- domains ----------

def list_domains(enabled_only: bool = False):
    sql = "SELECT * FROM domains"
    if enabled_only:
        sql += " WHERE enabled = 1"
    return conn().execute(sql + " ORDER BY id").fetchall()


def get_domain(domain_id: int):
    return conn().execute("SELECT * FROM domains WHERE id = ?", (domain_id,)).fetchone()


def get_domain_by_name(name: str, rrtype: str):
    return conn().execute(
        "SELECT * FROM domains WHERE name = ? AND rrtype = ?", (name, rrtype)
    ).fetchone()


def create_domain(name: str, rrtype: str, resolver: str, interval_sec: int, note: str) -> int:
    cur = conn().execute(
        "INSERT INTO domains (name, rrtype, resolver, interval_sec, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, rrtype, resolver, interval_sec, note, int(time.time())),
    )
    conn().commit()
    return cur.lastrowid


def update_domain(domain_id: int, **kw):
    fields = ", ".join(f"{k} = ?" for k in kw)
    conn().execute(f"UPDATE domains SET {fields} WHERE id = ?", (*kw.values(), domain_id))
    conn().commit()


def delete_domain(domain_id: int):
    c = conn()
    c.execute("DELETE FROM domains WHERE id = ?", (domain_id,))
    c.execute("DELETE FROM checks WHERE domain_id = ?", (domain_id,))
    c.execute("DELETE FROM changes WHERE domain_id = ?", (domain_id,))
    c.commit()


# ---------- checks ----------

def add_check(domain_id: int, status: str, ip, rtt_ms, error=None):
    conn().execute(
        "INSERT INTO checks (domain_id, ts, status, ip, rtt_ms, error) VALUES (?, ?, ?, ?, ?, ?)",
        (domain_id, int(time.time()), status, ip, rtt_ms, error),
    )
    conn().commit()


def last_check(domain_id: int):
    return conn().execute(
        "SELECT * FROM checks WHERE domain_id = ? ORDER BY ts DESC, id DESC LIMIT 1",
        (domain_id,),
    ).fetchone()


def recent_checks(domain_id: int, limit: int = 20):
    return conn().execute(
        "SELECT * FROM checks WHERE domain_id = ? ORDER BY ts DESC, id DESC LIMIT ?",
        (domain_id, limit),
    ).fetchall()


def cleanup_checks(days: int):
    conn().execute("DELETE FROM checks WHERE ts < ?", (int(time.time()) - days * 86400,))
    conn().commit()


# ---------- changes ----------

def add_change(domain_id: int, old_ip, new_ip):
    conn().execute(
        "INSERT INTO changes (domain_id, ts, old_ip, new_ip) VALUES (?, ?, ?, ?)",
        (domain_id, int(time.time()), old_ip, new_ip),
    )
    conn().commit()


def reset_baseline(domain_id: int):
    add_change(domain_id, None, None)


def current_ip(domain_id: int):
    row = conn().execute(
        "SELECT new_ip FROM changes WHERE domain_id = ? AND new_ip IS NOT NULL "
        "ORDER BY ts DESC, id DESC LIMIT 1",
        (domain_id,),
    ).fetchone()
    return row["new_ip"] if row else None


def last_change_ts(domain_id: int):
    row = conn().execute(
        "SELECT ts FROM changes WHERE domain_id = ? AND new_ip IS NOT NULL "
        "ORDER BY ts DESC, id DESC LIMIT 1",
        (domain_id,),
    ).fetchone()
    return row["ts"] if row else None


def change_count(domain_id: int, since: int) -> int:
    return conn().execute(
        "SELECT COUNT(*) AS n FROM changes WHERE domain_id = ? AND ts >= ? AND old_ip IS NOT NULL",
        (domain_id, since),
    ).fetchone()["n"]


def count_changes(domain_id: int) -> int:
    return conn().execute(
        "SELECT COUNT(*) AS n FROM changes WHERE domain_id = ? AND old_ip IS NOT NULL",
        (domain_id,),
    ).fetchone()["n"]


def changes_since(domain_id: int, since: int):
    return conn().execute(
        "SELECT * FROM changes WHERE domain_id = ? AND ts >= ? AND new_ip IS NOT NULL "
        "ORDER BY ts ASC, id ASC",
        (domain_id, since),
    ).fetchall()


def last_change_before(domain_id: int, ts: int):
    return conn().execute(
        "SELECT * FROM changes WHERE domain_id = ? AND ts < ? AND new_ip IS NOT NULL "
        "ORDER BY ts DESC, id DESC LIMIT 1",
        (domain_id, ts),
    ).fetchone()


def changes_list(domain_id: int, limit: int = 200):
    return conn().execute(
        "SELECT * FROM changes WHERE domain_id = ? ORDER BY ts DESC, id DESC LIMIT ?",
        (domain_id, limit),
    ).fetchall()
