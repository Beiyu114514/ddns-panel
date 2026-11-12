import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import stat
import time
import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 允许测试与演示种子使用独立数据目录，避免污染真实数据
DATA_DIR = os.environ.get("DDNS_DATA_DIR") or os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA_DIR, "ddns.db")

_conn = None
logger = logging.getLogger(__name__)


def local_hour_ts(ts: int) -> int:
    """返回本地墙上整点；DST 回拨的重复小时合并到第一次出现的桶。"""
    return int(datetime.datetime.fromtimestamp(int(ts)).replace(
        minute=0, second=0, microsecond=0, fold=0).timestamp())

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
CREATE TABLE IF NOT EXISTS check_hourly (
  domain_id INTEGER NOT NULL,
  hour_ts   INTEGER NOT NULL,
  total     INTEGER NOT NULL,
  fail      INTEGER NOT NULL,
  PRIMARY KEY (domain_id, hour_ts)
);
CREATE INDEX IF NOT EXISTS idx_check_hourly_ts ON check_hourly (hour_ts);
CREATE TABLE IF NOT EXISTS changes (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  ts        INTEGER NOT NULL,
  old_ip    TEXT,
  new_ip    TEXT,
  notified  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_changes_dom_ts ON changes (domain_id, ts);
CREATE INDEX IF NOT EXISTS idx_changes_reset ON changes (domain_id, ts DESC, id DESC)
  WHERE old_ip IS NULL AND new_ip IS NULL;
-- keyset 分页与时间线扫描：按 (domain_id, ts DESC, id DESC) 直接走索引，
-- 不再为 OFFSET 生成临时 B-tree
CREATE INDEX IF NOT EXISTS idx_changes_dom_ts_id ON changes (domain_id, ts DESC, id DESC);
-- 图例时长聚合只读 (ts, new_ip)，覆盖索引避免回表
CREATE INDEX IF NOT EXISTS idx_changes_dom_ts_ip ON changes (domain_id, ts, new_ip);
CREATE INDEX IF NOT EXISTS idx_checks_ts ON checks (ts);
CREATE INDEX IF NOT EXISTS idx_checks_ts_status ON checks (ts, status);
CREATE INDEX IF NOT EXISTS idx_changes_ts ON changes (ts);
CREATE INDEX IF NOT EXISTS idx_changes_pending ON changes (domain_id, ts) WHERE notified = 0;
"""


def _restrict_permissions(path: str, mode: int):
    try:
        os.chmod(path, mode)
    except FileNotFoundError:
        # WAL/SHM 会随连接和检查点创建、删除，不存在时无需处理
        pass
    except (OSError, NotImplementedError) as e:
        # 非 root 部署、Windows 或特殊挂载可能不允许 chmod；保留服务可用性，
        # 但必须让管理员看到告警，以便在宿主机上修正权限
        logger.warning("无法将 %s 权限收紧为 %04o: %s", path, mode, e)


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
        _restrict_permissions(DATA_DIR, 0o700)
        # 新库先以 0600 创建，避免宽松 umask 产生可读窗口；已有库不会被截断。
        # SQLite 的 WAL/SHM 继承主库权限，因此要在打开连接前收紧主库。
        fd = os.open(DB_PATH, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        for suffix in ("", "-wal", "-shm"):
            _restrict_permissions(DB_PATH + suffix, 0o600)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=5000")
        _conn.create_function("local_hour_ts", 1, local_hour_ts, deterministic=True)
        for suffix in ("-wal", "-shm"):
            _restrict_permissions(DB_PATH + suffix, 0o600)
    return _conn


def init():
    c = conn()
    c.executescript(SCHEMA)
    c.commit()
    _migrate(c)
    _migrate_check_hourly(c)
    if not get_setting("auth_secret"):
        set_setting("auth_secret", secrets.token_hex(32))
    if not get_setting("password_hash"):
        pw = secrets.token_urlsafe(6)
        path = os.path.join(DATA_DIR, "initial_password.txt")
        # 先落盘再提交 hash：文件写失败时不能留下"有 hash 但没有密码文件"的状态，
        # 否则重启后不会再生成初始密码，管理员将永久无法登录
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(f"{path} 不是普通文件")
            # mode 只对新建文件生效：已存在的文件要在写入前收紧，
            # 否则写入到 chmod 之间会有一个可读窗口
            os.fchmod(fd, 0o600)
            f = os.fdopen(fd, "w")
            fd = -1  # fdopen 接管 fd 后不能再重复关闭
            with f:
                f.write(pw + "\n")
                f.flush()
                # 先把密码文件刷到磁盘，再提交 hash：断电后不能出现
                # "DB 已有 hash、密码文件为空"的永久锁死状态
                os.fsync(f.fileno())
        except Exception:
            if fd >= 0:
                os.close(fd)
            raise
        set_setting("password_hash", hash_password(pw))
        # 不要把密码写进 stdout：容器日志/journald 会长期保留，删除密码文件也无济于事
        print(f"[setup] 初始密码已写入 {path}，登录后请修改", flush=True)


def _migrate(c: sqlite3.Connection):
    cols = {r["name"] for r in c.execute("PRAGMA table_info(changes)")}
    if "notified" not in cols:
        # 旧库里的历史变动无法判断是否已通知：一律视为已通知，
        # 避免升级后把几个月前的变动当成待发消息补发
        c.execute("ALTER TABLE changes ADD COLUMN notified INTEGER NOT NULL DEFAULT 1")
        c.commit()


def _migrate_check_hourly(c: sqlite3.Connection):
    tz_key = os.environ.get("TZ") or ",".join(time.tzname)
    if get_setting("check_hourly_local_backfilled") == tz_key:
        return
    # 旧汇总按 UTC 整点分桶；切换本地整点后必须整表重建，不能沿用旧桶。
    c.execute("DELETE FROM check_hourly")
    c.execute(
        "INSERT INTO check_hourly (domain_id, hour_ts, total, fail) "
        "SELECT domain_id, local_hour_ts(ts), COUNT(*), "
        "SUM(CASE WHEN status != 'ok' THEN 1 ELSE 0 END) "
        "FROM checks GROUP BY domain_id, local_hour_ts(ts)"
    )
    c.execute(
        "INSERT INTO settings (key, value) VALUES ('check_hourly_local_backfilled', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (tz_key,),
    )
    c.commit()


def get_setting(key: str, default=None):
    row = conn().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value):
    set_settings({key: value})


def set_settings(items: dict):
    # 单事务写多条，避免"改了一半"的中间态（如改密码时 hash 已更新而 secret 未轮换）
    c = conn()
    try:
        c.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(k, str(v)) for k, v in items.items()],
        )
        c.commit()
    except Exception:
        # 不 rollback 的话，失败的半截写入会被后续无关的 commit 顺带提交
        c.rollback()
        raise


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


def update_domain(domain_id: int, reset_marker: bool = False, **kw):
    # 记录类型变更与基线 marker 必须同一事务，否则崩溃会在两者之间留下
    # "已换类型但未重置基线"，下一次检查会把旧类型的 IP 当成基线记成变动
    c = conn()
    try:
        fields = ", ".join(f"{k} = ?" for k in kw)
        c.execute(f"UPDATE domains SET {fields} WHERE id = ?", (*kw.values(), domain_id))
        if reset_marker:
            c.execute(
                "INSERT INTO changes (domain_id, ts, old_ip, new_ip, notified) "
                "VALUES (?, ?, NULL, NULL, 1)",
                (domain_id, int(time.time())),
            )
            # 记录类型切换后，旧类型的未通知变动不能再发出去
            c.execute(
                "UPDATE changes SET notified = 1 WHERE domain_id = ? AND notified = 0",
                (domain_id,),
            )
        c.commit()
    except Exception:
        c.rollback()
        raise


def delete_domain(domain_id: int):
    c = conn()
    c.execute("DELETE FROM domains WHERE id = ?", (domain_id,))
    c.execute("DELETE FROM checks WHERE domain_id = ?", (domain_id,))
    c.execute("DELETE FROM check_hourly WHERE domain_id = ?", (domain_id,))
    c.execute("DELETE FROM changes WHERE domain_id = ?", (domain_id,))
    c.commit()


# ---------- checks ----------

def add_check(domain_id: int, status: str, ip, rtt_ms, error=None, ts: int | None = None):
    c = conn()
    ts = int(time.time()) if ts is None else int(ts)
    c.execute(
        "INSERT INTO checks (domain_id, ts, status, ip, rtt_ms, error) VALUES (?, ?, ?, ?, ?, ?)",
        (domain_id, ts, status, ip, rtt_ms, error),
    )
    c.execute(
        "INSERT INTO check_hourly (domain_id, hour_ts, total, fail) VALUES (?, ?, 1, ?) "
        "ON CONFLICT(domain_id, hour_ts) DO UPDATE SET "
        "total = total + 1, fail = fail + excluded.fail",
        (domain_id, local_hour_ts(ts), int(status != "ok")),
    )
    c.commit()


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


def global_last_bad_check():
    """最近一条非 ok 检查（首页“最近检查”只展示异常，正常不打扰）。

    停用项不再监控，它的旧失败不能驱动首页“近期异常已恢复”提示。
    """
    return conn().execute(
        "SELECT c.*, d.name AS domain_name, d.rrtype FROM checks c "
        "JOIN domains d ON d.id = c.domain_id "
        "WHERE c.status != 'ok' AND d.enabled = 1 "
        "ORDER BY c.ts DESC, c.id DESC LIMIT 1"
    ).fetchone()


def check_stats_since(since: int):
    return conn().execute(
        "SELECT COALESCE(SUM(total), 0) AS total, "
        "COALESCE(SUM(total - fail), 0) AS ok, COALESCE(SUM(fail), 0) AS fail "
        "FROM check_hourly WHERE hour_ts >= ?",
        (local_hour_ts(since),),
    ).fetchone()


def chart_counts(since: int, hours: int):
    """按图表小时桶聚合，避免将窗口内的检查明细全部传到 Python。"""
    c = conn()
    checks = c.execute(
        "SELECT CAST((hour_ts - ?) / 3600 AS INTEGER) AS bucket, total, fail "
        "FROM check_hourly WHERE hour_ts >= ? AND hour_ts < ?",
        (since, since, since + hours * 3600),
    ).fetchall()
    changes = c.execute(
        "SELECT CAST((ts - ?) / 3600 AS INTEGER) AS bucket, COUNT(*) AS total "
        "FROM changes WHERE ts >= ? AND ts < ? AND old_ip IS NOT NULL GROUP BY bucket",
        (since, since, since + hours * 3600),
    ).fetchall()
    return checks, changes


def cleanup_checks(days: int):
    c = conn()
    cutoff = int(time.time()) - days * 86400
    cutoff_hour = local_hour_ts(cutoff)
    cutoff_end = int((datetime.datetime.fromtimestamp(cutoff_hour)
                      + datetime.timedelta(hours=1)).timestamp())
    c.execute("DELETE FROM checks WHERE ts < ?", (cutoff,))
    c.execute("DELETE FROM check_hourly WHERE hour_ts < ?", (cutoff_hour,))
    # 边界小时可能同时含过期与保留记录，按删除后的明细重算以保持 retention 语义。
    c.execute("DELETE FROM check_hourly WHERE hour_ts = ?", (cutoff_hour,))
    c.execute(
        "INSERT INTO check_hourly (domain_id, hour_ts, total, fail) "
        "SELECT domain_id, local_hour_ts(ts), COUNT(*), "
        "SUM(CASE WHEN status != 'ok' THEN 1 ELSE 0 END) FROM checks "
        "WHERE ts >= ? AND ts < ? AND local_hour_ts(ts) = ? "
        "GROUP BY domain_id, local_hour_ts(ts)",
        (cutoff, cutoff_end, cutoff_hour),
    )
    c.commit()


# ---------- changes ----------

def add_change(domain_id: int, old_ip, new_ip, notify: bool = True) -> int:
    # notify=0 表示这条变动还没有成功通知，进程重启后仍可补发；
    # 建立基线（old_ip 为空）和重置 marker 不算需要通知的变动。
    cur = conn().execute(
        "INSERT INTO changes (domain_id, ts, old_ip, new_ip, notified) "
        "VALUES (?, ?, ?, ?, ?)",
        (domain_id, int(time.time()), old_ip, new_ip, 0 if notify else 1),
    )
    conn().commit()
    return cur.lastrowid


def pending_change(domain_id: int):
    """该域名最早一条未通知变动：返回 (旧 IP, 最新 IP, 起始时间, 最后一条记录 id)。

    连续多次变动合并成一条通知（最早的旧 IP → 最新的新 IP），
    避免重启或发送失败后通知里的"旧 IP"变成用户从未见过的中间值。
    """
    row = conn().execute(
        "SELECT (SELECT old_ip FROM changes WHERE domain_id = ? AND notified = 0 "
        "AND new_ip IS NOT NULL ORDER BY ts ASC, id ASC LIMIT 1) AS old_ip, "
        "(SELECT ts FROM changes WHERE domain_id = ? AND notified = 0 "
        "AND new_ip IS NOT NULL ORDER BY ts ASC, id ASC LIMIT 1) AS first_ts, "
        "(SELECT new_ip FROM changes WHERE domain_id = ? AND notified = 0 "
        "AND new_ip IS NOT NULL ORDER BY ts DESC, id DESC LIMIT 1) AS new_ip, "
        "(SELECT id FROM changes WHERE domain_id = ? AND notified = 0 "
        "AND new_ip IS NOT NULL ORDER BY ts DESC, id DESC LIMIT 1) AS last_id",
        (domain_id, domain_id, domain_id, domain_id),
    ).fetchone()
    if row["first_ts"] is None:
        return None
    return row["old_ip"], row["new_ip"], row["first_ts"], row["last_id"]


def mark_changes_notified(domain_id: int, up_to_id: int | None = None):
    if up_to_id is None:
        conn().execute(
            "UPDATE changes SET notified = 1 WHERE domain_id = ? AND notified = 0",
            (domain_id,),
        )
    else:
        conn().execute(
            "UPDATE changes SET notified = 1 "
            "WHERE domain_id = ? AND notified = 0 AND id <= ?",
            (domain_id, up_to_id),
        )
    conn().commit()


def reset_baseline(domain_id: int):
    add_change(domain_id, None, None, notify=False)


def last_reset(domain_id: int):
    return conn().execute(
        "SELECT * FROM changes WHERE domain_id = ? AND old_ip IS NULL AND new_ip IS NULL "
        "ORDER BY ts DESC, id DESC LIMIT 1",
        (domain_id,),
    ).fetchone()


def last_activity_ts(domain_id: int):
    # 含 marker 行：基线重置后"已稳定"应从重置时刻重新起算，而不是回落到域名创建时间
    row = conn().execute(
        "SELECT MAX(ts) AS ts FROM changes WHERE domain_id = ?", (domain_id,)
    ).fetchone()
    return row["ts"] if row and row["ts"] is not None else None


def current_ip(domain_id: int):
    # 基线重置的 marker 行 (NULL, NULL) 也必须参与"取最新一行"，否则重置前的旧 IP
    # 会被继续当作当前基线；marker 行 new_ip 为 NULL 即表示"无基线"
    row = conn().execute(
        "SELECT new_ip FROM changes WHERE domain_id = ? ORDER BY ts DESC, id DESC LIMIT 1",
        (domain_id,),
    ).fetchone()
    return row["new_ip"] if row else None


def last_change_ts(domain_id: int):
    row = conn().execute(
        "SELECT ts, new_ip FROM changes WHERE domain_id = ? ORDER BY ts DESC, id DESC LIMIT 1",
        (domain_id,),
    ).fetchone()
    return row["ts"] if row and row["new_ip"] is not None else None


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


def count_changes_window(domain_id: int, since: int, after_id: int | None = None) -> int:
    """窗口内真实 IP 变动数（不含首次记录）。after_id 非空时按 (ts,id) 复合边界排除重置前记录。"""
    if after_id is None:
        row = conn().execute(
            "SELECT COUNT(*) AS n FROM changes WHERE domain_id = ? AND ts >= ? "
            "AND old_ip IS NOT NULL",
            (domain_id, since),
        ).fetchone()
    else:
        row = conn().execute(
            "SELECT COUNT(*) AS n FROM changes WHERE domain_id = ? "
            "AND (ts > ? OR (ts = ? AND id > ?)) AND old_ip IS NOT NULL",
            (domain_id, since, since, after_id),
        ).fetchone()
    return row["n"]


def count_timeline_rows(domain_id: int, since: int, after_id: int | None = None) -> int:
    """窗口内可绘制行数（含首次记录，不含 marker），用于决定时间线是否采样。"""
    if after_id is None:
        row = conn().execute(
            "SELECT COUNT(*) AS n FROM changes WHERE domain_id = ? AND ts >= ? "
            "AND new_ip IS NOT NULL",
            (domain_id, since),
        ).fetchone()
    else:
        row = conn().execute(
            "SELECT COUNT(*) AS n FROM changes WHERE domain_id = ? "
            "AND (ts > ? OR (ts = ? AND id > ?)) AND new_ip IS NOT NULL",
            (domain_id, since, since, after_id),
        ).fetchone()
    return row["n"]


def changes_window(domain_id: int, since: int, after_id: int | None = None,
                   bucket: int | None = None):
    """按时间窗口取变动；bucket 非空时每个时间桶只取最后一条，限制绘制段数。"""
    if after_id is None:
        where = "domain_id = ? AND ts >= ?"
        params = [domain_id, since]
    else:
        where = "domain_id = ? AND (ts > ? OR (ts = ? AND id > ?))"
        params = [domain_id, since, since, after_id]
    if bucket:
        # 窗口内变动过密时按时间桶取样，避免把全部记录都变成时间线节点。
        # 取每桶最后一条（ts,id 最大）而不是第一条，保证相邻桶的 IP 是递增后的状态。
        sql = (
            "SELECT id, ts, new_ip FROM ("
            "SELECT id, ts, new_ip, ROW_NUMBER() OVER ("
            "PARTITION BY CAST((ts - ?) / ? AS INTEGER) "
            "ORDER BY ts DESC, id DESC) AS rn "
            f"FROM changes WHERE {where} AND new_ip IS NOT NULL"
            ") WHERE rn = 1 ORDER BY ts ASC, id ASC"
        )
        params = [since, bucket, *params]
    else:
        sql = (
            f"SELECT id, ts, new_ip FROM changes WHERE {where} "
            "AND new_ip IS NOT NULL ORDER BY ts ASC, id ASC"
        )
    return conn().execute(sql, params).fetchall()


def changes_count_since(since: int) -> int:
    return conn().execute(
        "SELECT COUNT(*) AS n FROM changes WHERE ts >= ? AND old_ip IS NOT NULL", (since,)
    ).fetchone()["n"]


def count_domains() -> dict:
    row = conn().execute(
        "SELECT COUNT(*) AS total, SUM(enabled) AS enabled FROM domains"
    ).fetchone()
    return {"total": row["total"] or 0, "enabled": row["enabled"] or 0}


def overview_domains(since7: int, since30: int):
    """一次读取首页所需域名状态字段，避免每个域名重复发起五条查询。"""
    return conn().execute(
        "SELECT d.*, last.id AS last_id, last.ts AS last_ts, last.status AS last_status, "
        "last.ip AS last_check_ip, last.error AS last_error, "
        "(SELECT new_ip FROM changes WHERE domain_id = d.id ORDER BY ts DESC, id DESC LIMIT 1) AS cur, "
        "(SELECT MAX(ts) FROM changes WHERE domain_id = d.id) AS activity, "
        "(SELECT COUNT(*) FROM changes WHERE domain_id = d.id AND ts >= ? AND old_ip IS NOT NULL) AS c7, "
        "(SELECT COUNT(*) FROM changes WHERE domain_id = d.id AND ts >= ? AND old_ip IS NOT NULL) AS c30 "
        "FROM domains d LEFT JOIN checks last ON last.id = ("
        "SELECT id FROM checks WHERE domain_id = d.id ORDER BY ts DESC, id DESC LIMIT 1) "
        "ORDER BY d.name",
        (since7, since30),
    ).fetchall()


def last_change_before(domain_id: int, ts: int):
    # 不过滤 NULL：若 marker 行是 ts 前最新一行，调用方据此判定"无基线"
    return conn().execute(
        "SELECT * FROM changes WHERE domain_id = ? AND ts < ? "
        "ORDER BY ts DESC, id DESC LIMIT 1",
        (domain_id, ts),
    ).fetchone()


def change_stats(domain_id: int, since: int | None = None) -> dict:
    """统计契约（R2）：rows 含全部行，changes 只算真实变动。

    - rows: 全部行数（含首次记录与基线重置 marker）；
    - changes: old_ip IS NOT NULL 的变动次数；
    - first_records: 首次记录（old_ip 为空、new_ip 非空）；
    - markers: 基线重置（两者皆空）。
    """
    where = "domain_id = ?"
    params: list = [domain_id]
    if since is not None:
        where += " AND ts >= ?"
        params.append(since)
    row = conn().execute(
        "SELECT COUNT(*) AS rows, "
        "SUM(CASE WHEN old_ip IS NOT NULL THEN 1 ELSE 0 END) AS changes, "
        "SUM(CASE WHEN old_ip IS NULL AND new_ip IS NOT NULL THEN 1 ELSE 0 END) AS first_records, "
        "SUM(CASE WHEN old_ip IS NULL AND new_ip IS NULL THEN 1 ELSE 0 END) AS markers "
        f"FROM changes WHERE {where}",
        params,
    ).fetchone()
    return {
        "rows": row["rows"] or 0,
        "changes": row["changes"] or 0,
        "first_records": row["first_records"] or 0,
        "markers": row["markers"] or 0,
    }


def count_change_rows(domain_id: int) -> int:
    # 分页总数包含 marker 行，和表格里实际展示的行数一致
    return conn().execute(
        "SELECT COUNT(*) AS n FROM changes WHERE domain_id = ?", (domain_id,)
    ).fetchone()["n"]


def changes_list(domain_id: int, limit: int = 20,
                 before_ts: int | None = None, before_id: int | None = None,
                 after_ts: int | None = None, after_id: int | None = None):
    """keyset 分页（R3）：以 (ts,id) 为游标，翻页期间插入新行不重复、不漏行。

    before_* 取更旧的一页（下一页），after_* 取更新的一页（上一页）。
    两者都为空时返回第一页。
    """
    c = conn()
    if before_ts is not None:
        return c.execute(
            "SELECT * FROM changes WHERE domain_id = ? "
            "AND (ts < ? OR (ts = ? AND id < ?)) ORDER BY ts DESC, id DESC LIMIT ?",
            (domain_id, before_ts, before_ts, before_id, limit),
        ).fetchall()
    if after_ts is not None:
        # 先按升序取紧邻的 limit 行（更新方向），再翻转成统一的倒序展示
        rows = c.execute(
            "SELECT * FROM changes WHERE domain_id = ? "
            "AND (ts > ? OR (ts = ? AND id > ?)) ORDER BY ts ASC, id ASC LIMIT ?",
            (domain_id, after_ts, after_ts, after_id, limit),
        ).fetchall()
        return list(reversed(rows))
    return c.execute(
        "SELECT * FROM changes WHERE domain_id = ? ORDER BY ts DESC, id DESC LIMIT ?",
        (domain_id, limit),
    ).fetchall()


def _has_side(domain_id: int, ts: int, row_id: int, older: bool) -> bool:
    op = "<" if older else ">"
    row = conn().execute(
        "SELECT 1 FROM changes WHERE domain_id = ? "
        f"AND (ts {op} ? OR (ts = ? AND id {op} ?)) LIMIT 1",
        (domain_id, ts, ts, row_id),
    ).fetchone()
    return row is not None


def has_older(domain_id: int, ts: int, row_id: int) -> bool:
    return _has_side(domain_id, ts, row_id, older=True)


def has_newer(domain_id: int, ts: int, row_id: int) -> bool:
    return _has_side(domain_id, ts, row_id, older=False)


def change_durations(domain_id: int, since: int, after_id: int | None,
                     now: int) -> list:
    """图例时长（R1）：用未采样的完整序列按 new_ip 汇总。

    采样只影响时间线节点；此查询始终覆盖窗口内全部变动记录。
    """
    if after_id is None:
        boundary = "ts < ?"
        win = "ts >= ?"
        params = [domain_id, since, domain_id, since, now, now]
    else:
        boundary = "(ts < ? OR (ts = ? AND id <= ?))"
        win = "(ts > ? OR (ts = ? AND id > ?))"
        params = [domain_id, since, since, after_id, domain_id, since, since, after_id,
                  now, now]
    rows = conn().execute(
        "WITH base AS (SELECT id, ts, new_ip FROM changes WHERE domain_id = ? "
        "AND new_ip IS NOT NULL AND " + boundary + " ORDER BY ts DESC, id DESC LIMIT 1), "
        "window_rows AS (SELECT id, ts, new_ip, 1 AS in_window FROM changes WHERE domain_id = ? "
        "AND new_ip IS NOT NULL AND " + win + "), "
        "seq AS (SELECT id, ts, new_ip, in_window, LEAD(ts) OVER (ORDER BY ts, id) AS next_ts "
        "FROM (SELECT id, ts, new_ip, 0 AS in_window FROM base UNION ALL SELECT * FROM window_rows)) "
        "SELECT new_ip, SUM(in_window) AS n, "
        "SUM(CASE WHEN next_ts IS NULL OR next_ts > ? THEN ? - ts ELSE next_ts - ts END) AS dur "
        "FROM seq WHERE in_window = 1 GROUP BY new_ip ORDER BY dur DESC, new_ip",
        params,
    ).fetchall()
    return [{"ip": r["new_ip"], "rows": r["n"], "dur": max(0, r["dur"] or 0)}
            for r in rows if r["new_ip"]]


def first_change_ts(domain_id: int, since: int, after_id: int | None):
    """窗口内第一条真实记录的 ts（用于基线段终点）；没有则返回 None。"""
    if after_id is None:
        row = conn().execute(
            "SELECT MIN(ts) AS ts FROM changes WHERE domain_id = ? AND ts >= ? "
            "AND new_ip IS NOT NULL",
            (domain_id, since),
        ).fetchone()
    else:
        row = conn().execute(
            "SELECT MIN(ts) AS ts FROM changes WHERE domain_id = ? "
            "AND (ts > ? OR (ts = ? AND id > ?)) AND new_ip IS NOT NULL",
            (domain_id, since, since, after_id),
        ).fetchone()
    return row["ts"] if row and row["ts"] is not None else None
