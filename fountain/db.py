"""SQLite 连接与建表。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS zones (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS pools (
    id INTEGER PRIMARY KEY,
    zone_id INTEGER NOT NULL REFERENCES zones(id),
    name TEXT NOT NULL,
    shape TEXT NOT NULL DEFAULT 'rect',          -- rect | oval
    footprint TEXT NOT NULL,                      -- JSON: x,y,w,h（总览布局，百分比）
    design_level REAL NOT NULL DEFAULT 1.20       -- 设计水位
);

CREATE TABLE IF NOT EXISTS pumps (
    id INTEGER PRIMARY KEY,
    pool_id INTEGER NOT NULL REFERENCES pools(id),
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'idle',          -- idle|running|fault|maintenance
    fault_code TEXT DEFAULT NULL,
    temperature REAL NOT NULL DEFAULT 38.0
);

CREATE TABLE IF NOT EXISTS nozzle_groups (
    id INTEGER PRIMARY KEY,
    pool_id INTEGER NOT NULL REFERENCES pools(id),
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    position TEXT NOT NULL,                       -- JSON: 池体轮廓上的短线 [{x1,y1,x2,y2}]
    clogged INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY,
    pool_id INTEGER NOT NULL REFERENCES pools(id),
    start_at TEXT NOT NULL,                       -- ISO 本地时间 HH:MM 每日
    end_at TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_fired_date TEXT DEFAULT NULL,           -- YYYY-MM-DD 幂等键
    created_at TEXT NOT NULL
);

-- 运行实例：每个计划每天至多一条（UNIQUE 保证两人并发启动仅一次生效）
CREATE TABLE IF NOT EXISTS run_sessions (
    id INTEGER PRIMARY KEY,
    schedule_id INTEGER NOT NULL REFERENCES schedules(id),
    pool_id INTEGER NOT NULL REFERENCES pools(id),
    run_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'blocked',       -- blocked|running|stopped|weather_stop
    block_reason TEXT DEFAULT '',
    started_at TEXT DEFAULT NULL,
    stopped_at TEXT DEFAULT NULL,
    stop_reason TEXT DEFAULT '',
    override_approved INTEGER NOT NULL DEFAULT 0,
    approver TEXT DEFAULT NULL,
    UNIQUE(schedule_id, run_date)
);

-- 水位与水质采样
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY,
    pool_id INTEGER NOT NULL REFERENCES pools(id),
    ts TEXT NOT NULL,
    water_level REAL NOT NULL,
    turbidity REAL NOT NULL,
    chlorine REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'adapter',       -- adapter|manual|retest
    note TEXT DEFAULT ''
);

-- 补水记录
CREATE TABLE IF NOT EXISTS refill_logs (
    id INTEGER PRIMARY KEY,
    pool_id INTEGER NOT NULL REFERENCES pools(id),
    ts TEXT NOT NULL,
    amount_liters REAL NOT NULL,
    operator TEXT NOT NULL,
    note TEXT DEFAULT ''
);

-- 药剂台账
CREATE TABLE IF NOT EXISTS chemicals (
    id INTEGER PRIMARY KEY,
    pool_id INTEGER NOT NULL REFERENCES pools(id),
    ts TEXT NOT NULL,
    agent TEXT NOT NULL,                          -- 消毒剂/絮凝剂/除藻剂
    amount REAL NOT NULL,
    unit TEXT NOT NULL DEFAULT 'L',
    operator TEXT NOT NULL,
    observe_until TEXT NOT NULL,                  -- 观察期截止 ISO
    overdose INTEGER NOT NULL DEFAULT 0,
    note TEXT DEFAULT ''
);

-- 清池任务：锁定整个水池并暂停计划
CREATE TABLE IF NOT EXISTS clean_tasks (
    id INTEGER PRIMARY KEY,
    pool_id INTEGER NOT NULL REFERENCES pools(id),
    started_at TEXT NOT NULL,
    finished_at TEXT DEFAULT NULL,
    keeper TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',        -- active|awaiting_retest|done|cancelled
    retest_passed INTEGER NOT NULL DEFAULT 0,
    nozzle_trial_passed INTEGER NOT NULL DEFAULT 0,
    note TEXT DEFAULT ''
);

-- 故障维修
CREATE TABLE IF NOT EXISTS faults (
    id INTEGER PRIMARY KEY,
    pool_id INTEGER NOT NULL REFERENCES pools(id),
    pump_id INTEGER REFERENCES pumps(id),
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,                           -- nozzle_clog|pump_overheat|other
    detail TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',          -- open|repairing|awaiting_retest|resolved
    repaired_at TEXT DEFAULT NULL,
    repair_note TEXT DEFAULT '',
    retest_passed INTEGER NOT NULL DEFAULT 0,
    nozzle_trial_passed INTEGER NOT NULL DEFAULT 0
);

-- 复开审批（越限复开必须主管批准；退回也留痕）
CREATE TABLE IF NOT EXISTS reopen_requests (
    id INTEGER PRIMARY KEY,
    pool_id INTEGER NOT NULL REFERENCES pools(id),
    ref_type TEXT NOT NULL,                       -- clean|fault
    ref_id INTEGER NOT NULL,
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',       -- pending|approved|rejected
    reviewer TEXT DEFAULT NULL,
    reviewed_at TEXT DEFAULT NULL,
    reason TEXT DEFAULT '',
    reject_reason TEXT DEFAULT ''
);

-- 封控/停运事件（水位不足、水质超限、误启动、游客进入等）
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    pool_id INTEGER REFERENCES pools(id),
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'warn',           -- info|warn|danger
    message TEXT NOT NULL,
    handled_by TEXT DEFAULT NULL,
    handled_at TEXT DEFAULT NULL,
    resolution TEXT DEFAULT ''
);

-- 现场附件（文件保存在本地目录）
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY,
    ref_type TEXT NOT NULL,                       -- clean|fault|sample|pool
    ref_id INTEGER NOT NULL DEFAULT 0,
    filename TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    uploaded_by TEXT NOT NULL,
    ts TEXT NOT NULL,
    note TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_samples_pool_ts ON samples(pool_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """事务串行化：所有领域写操作走 IMMEDIATE，保证互斥与幂等。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
