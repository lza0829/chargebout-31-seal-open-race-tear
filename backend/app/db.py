from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from app.config import DB_PATH

# 并发口径（全局唯一，架构 A）：
#   锁账世代优先。封印提交持写锁并把世代号推进；与之争用的在途开充
#   等锁到手后必然读到「已封印」，于是整笔回滚：不建会话、不动桩枪占用、
#   不推进会话序号、不落开充回执。因此任一时刻只有两种可重放结局：
#   开充先提交（会话合法存在，封印事件把它带上）或封印先提交（开充整笔失败）。
SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  address TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS rate_plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  peak_price REAL NOT NULL,
  flat_price REAL NOT NULL,
  valley_price REAL NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS piles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  rated_kw REAL NOT NULL DEFAULT 7,
  status TEXT NOT NULL DEFAULT '空闲',
  rate_plan_id INTEGER NOT NULL,
  UNIQUE(site_id, name),
  FOREIGN KEY(site_id) REFERENCES sites(id),
  FOREIGN KEY(rate_plan_id) REFERENCES rate_plans(id)
);
CREATE TABLE IF NOT EXISTS guns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pile_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '可用',
  UNIQUE(pile_id, name),
  FOREIGN KEY(pile_id) REFERENCES piles(id)
);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slip_no TEXT NOT NULL UNIQUE,
  site_id INTEGER NOT NULL,
  pile_id INTEGER NOT NULL,
  gun_id INTEGER NOT NULL,
  rate_plan_id INTEGER NOT NULL,
  business_day TEXT NOT NULL,
  status TEXT NOT NULL,
  kwh REAL NOT NULL DEFAULT 0,
  amount REAL NOT NULL DEFAULT 0,
  price_locked REAL,
  started_at TEXT NOT NULL,
  closed_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  seal_generation INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY(site_id) REFERENCES sites(id),
  FOREIGN KEY(pile_id) REFERENCES piles(id),
  FOREIGN KEY(gun_id) REFERENCES guns(id),
  FOREIGN KEY(rate_plan_id) REFERENCES rate_plans(id)
);
CREATE TABLE IF NOT EXISTS pulses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  delta_kwh REAL NOT NULL,
  power_kw REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE TABLE IF NOT EXISTS session_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE TABLE IF NOT EXISTS day_seals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_day TEXT NOT NULL UNIQUE,
  sealed INTEGER NOT NULL DEFAULT 0,
  sealed_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  generation INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- 锁账世代事件：每次封印/解封提交时在同一事务内追加，seq 给出跨表全序。
CREATE TABLE IF NOT EXISTS seal_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  seq INTEGER NOT NULL DEFAULT 0,
  business_day TEXT NOT NULL,
  generation INTEGER NOT NULL,
  action TEXT NOT NULL,
  open_count INTEGER NOT NULL DEFAULT 0,
  open_session_ids TEXT NOT NULL DEFAULT '',
  sealed_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT ''
);
-- 开充回执：开充提交成功时与新建会话行、枪线占用、会话序号同事务落账。
CREATE TABLE IF NOT EXISTS open_receipts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  seq INTEGER NOT NULL DEFAULT 0,
  receipt_no TEXT NOT NULL UNIQUE,
  session_id INTEGER NOT NULL,
  slip_no TEXT NOT NULL,
  business_day TEXT NOT NULL,
  generation INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_sessions_day ON sessions(business_day);
CREATE INDEX IF NOT EXISTS idx_receipts_day ON open_receipts(business_day);
CREATE INDEX IF NOT EXISTS idx_seal_events_day ON seal_events(business_day);
"""


def connect() -> sqlite3.Connection:
    # isolation_level=None：由 db() 显式 BEGIN IMMEDIATE / COMMIT，
    # 让「封印」与「开充」两个写事务在数据库层严格串行，杜绝读后写竞态。
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def db(immediate: bool = False):
    """单事务工作单元。

    写入口一律 immediate=True，以 BEGIN IMMEDIATE 抢写锁，保证事务一开始
    就能读到最新锁账世代并独占「读锁账状态 -> 写会话/占用/回执」临界区。
    异常一律 ROLLBACK，因此半截写入（含序号推进、占用列、回执、世代事件）
    全部回到争用前快照。只读入口用 DEFERRED，不与写事务抢升级锁。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate(conn: sqlite3.Connection) -> None:
    if "generation" not in _column_names(conn, "day_seals"):
        conn.execute("ALTER TABLE day_seals ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")
    if "seal_generation" not in _column_names(conn, "sessions"):
        conn.execute("ALTER TABLE sessions ADD COLUMN seal_generation INTEGER NOT NULL DEFAULT 0")


def init_db() -> None:
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
    finally:
        conn.close()


def row_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def rows(rs) -> list[dict]:
    return [row_dict(r) for r in rs]
