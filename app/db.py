import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None, *, autocommit: bool = False) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        path,
        check_same_thread=False,
        isolation_level=None if autocommit else "",
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if autocommit:
        # 并发续灌时排队等待写锁，而不是立刻报 database is locked
        conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def write_tx(db_path: str | None = None):
    """写事务：BEGIN IMMEDIATE 先拿写锁，两名值班并发提交也不会双写位点。

    写锁等到 busy_timeout 仍拿不到时抛 LockBusy，由调用方翻成可读的短句，
    不把原始 database is locked 漏到页面上。
    """
    conn = connect(db_path, autocommit=True)
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc) or "busy" in str(exc):
                raise LockBusy(str(exc)) from exc
            raise
        yield conn
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


class LockBusy(Exception):
    """并发续灌抢锁超时。"""


SCHEMA = """
CREATE TABLE IF NOT EXISTS backfill_windows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('草稿','已计划','执行中','已完成')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS backfill_slices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER NOT NULL REFERENCES backfill_windows(id),
  slice_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待重放','已落库','失败')),
  row_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE(window_id, slice_date)
);

-- 暂停 / 续跑状态（窗口生命周期状态仍在 backfill_windows.status）
CREATE TABLE IF NOT EXISTS backfill_runtime (
  window_id INTEGER PRIMARY KEY REFERENCES backfill_windows(id),
  state TEXT NOT NULL CHECK (state IN ('运行中','已暂停','已完成')),
  paused_at TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 旁路位点账册（只追加）：选定路线 A。
-- 每次提交位点在同一事务里原子记下现行偏移，再拒旧位点；
-- 不实现路线 B（回滚半截重写行再挂新偏移）。
CREATE TABLE IF NOT EXISTS backfill_checkpoints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER NOT NULL REFERENCES backfill_windows(id),
  version INTEGER NOT NULL,
  offset_no INTEGER NOT NULL,
  current_offset INTEGER NOT NULL,
  next_slice_id INTEGER,
  decision TEXT NOT NULL CHECK (decision IN ('有效','拒签')),
  reason_code TEXT NOT NULL DEFAULT 'OK',
  reason TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT '值班',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 每个窗口每个版本最多一条「有效」位点：并发双写在库层直接失败。
CREATE UNIQUE INDEX IF NOT EXISTS ux_checkpoint_accepted
  ON backfill_checkpoints(window_id, version) WHERE decision = '有效';
CREATE INDEX IF NOT EXISTS ix_checkpoint_window
  ON backfill_checkpoints(window_id, id);

CREATE TABLE IF NOT EXISTS coverage_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER NOT NULL,
  planned INTEGER NOT NULL,
  loaded INTEGER NOT NULL,
  missing INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone():
            return
        cur = conn.execute(
            "INSERT INTO backfill_windows(table_name, start_date, end_date, status) "
            "VALUES ('dw_orders_d','2026-11-10','2026-11-12','草稿')"
        )
        wid = cur.lastrowid
        conn.execute(
            "INSERT INTO backfill_windows(table_name, start_date, end_date, status) "
            "VALUES ('dw_payments_d','2026-11-11','2026-11-11','草稿')"
        )
        # placeholder so list is non-empty; slices created by plan
        _ = wid
        conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
