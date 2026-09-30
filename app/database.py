"""SQLite 建表、连接与演示数据。"""
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS zones (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    outline TEXT NOT NULL,          -- 池体轮廓 SVG 点串
    pos_x REAL NOT NULL,
    pos_y REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS pools (
    id INTEGER PRIMARY KEY,
    zone_id INTEGER NOT NULL REFERENCES zones(id),
    name TEXT NOT NULL,
    depth_m REAL NOT NULL,
    shape TEXT NOT NULL DEFAULT 'pond'   -- pond 异形池 / round 圆形池
);

CREATE TABLE IF NOT EXISTS pumps (
    id INTEGER PRIMARY KEY,
    pool_id INTEGER NOT NULL REFERENCES pools(id),
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    power_kw REAL NOT NULL DEFAULT 5.5,
    status TEXT NOT NULL DEFAULT 'idle',     -- idle/running/fault/repair
    fault_code TEXT
);

CREATE TABLE IF NOT EXISTS nozzle_groups (
    id INTEGER PRIMARY KEY,
    pool_id INTEGER NOT NULL REFERENCES pools(id),
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    nozzle_count INTEGER NOT NULL DEFAULT 8,
    layout TEXT NOT NULL,              -- 池体轮廓上的短线坐标串 "x1,y1;x2,y2"
    blocked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS run_schedules (
    id INTEGER PRIMARY KEY,
    pool_id INTEGER NOT NULL REFERENCES pools(id),
    start_at TEXT NOT NULL,            -- HH:MM
    end_at TEXT NOT NULL,
    days TEXT NOT NULL DEFAULT '1234567',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_date TEXT
);

CREATE TABLE IF NOT EXISTS water_readings (
    id INTEGER PRIMARY KEY,
    pool_id INTEGER NOT NULL REFERENCES pools(id),
    ts TEXT NOT NULL,
    level REAL NOT NULL,               -- 占池深比例 0~1
    turbidity REAL NOT NULL,           -- NTU
    chlorine REAL NOT NULL,            -- mg/L
    kind TEXT NOT NULL DEFAULT 'auto', -- auto 模拟采样 / manual 人工 / retest 复测
    note TEXT
);

CREATE TABLE IF NOT EXISTS dosing_records (
    id INTEGER PRIMARY KEY,
    pool_id INTEGER NOT NULL REFERENCES pools(id),
    ts TEXT NOT NULL,
    chemical TEXT NOT NULL,            -- chlorine 氯片 / flocculant 絮凝剂 / algaecide 除藻剂
    amount_ml REAL NOT NULL,
    operator TEXT NOT NULL,
    observe_until TEXT NOT NULL,       -- 观察期结束时刻
    note TEXT
);

CREATE TABLE IF NOT EXISTS cleaning_tasks (
    id INTEGER PRIMARY KEY,
    pool_id INTEGER NOT NULL REFERENCES pools(id),
    planned_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned',  -- planned/locked/in_progress/done
    assignee TEXT,
    started_at TEXT,
    finished_at TEXT,
    retest_reading_id INTEGER REFERENCES water_readings(id),
    nozzle_trial_ok INTEGER,
    note TEXT
);

CREATE TABLE IF NOT EXISTS faults (
    id INTEGER PRIMARY KEY,
    pool_id INTEGER NOT NULL REFERENCES pools(id),
    pump_id INTEGER REFERENCES pumps(id),
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,                -- level_low/quality/nozzle_block/overheat/weather/misstart/overdose/intrusion/maintenance
    detail TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',     -- open/repairing/resolved/rejected
    resolution TEXT,
    reopen_request_id INTEGER
);

CREATE TABLE IF NOT EXISTS reopen_requests (
    id INTEGER PRIMARY KEY,
    pool_id INTEGER NOT NULL REFERENCES pools(id),
    fault_id INTEGER REFERENCES faults(id),
    cleaning_id INTEGER REFERENCES cleaning_tasks(id),
    ts TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending/approved/rejected
    decided_by TEXT,
    decided_role TEXT,
    decided_at TEXT,
    retest_reading_id INTEGER REFERENCES water_readings(id),
    nozzle_trial_ok INTEGER,
    reject_reason TEXT
);

CREATE TABLE IF NOT EXISTS run_logs (
    id INTEGER PRIMARY KEY,
    pool_id INTEGER NOT NULL REFERENCES pools(id),
    schedule_id INTEGER REFERENCES run_schedules(id),
    ts TEXT NOT NULL,
    action TEXT NOT NULL,              -- start/stop/block/abort
    result TEXT NOT NULL,              -- ok/failed
    reason TEXT,
    actor TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    pool_id INTEGER REFERENCES pools(id),
    ts TEXT NOT NULL,
    category TEXT NOT NULL,
    message TEXT NOT NULL,
    handled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY,
    ref_table TEXT NOT NULL,
    ref_id INTEGER NOT NULL,
    filename TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    uploaded_at TEXT NOT NULL
);
"""


def connect(db_path: Path = None) -> sqlite3.Connection:
    path = db_path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection, seed: bool = True):
    conn.executescript(SCHEMA)
    conn.commit()
    if seed and conn.execute("SELECT COUNT(*) c FROM zones").fetchone()["c"] == 0:
        _seed(conn)


def _seed(conn: sqlite3.Connection):
    now = datetime.now()
    conn.executemany(
        "INSERT INTO zones(id,name,outline,pos_x,pos_y) VALUES(?,?,?,?,?)",
        [
            # 中央广场异形池（双弧轮廓）
            (1, "中央广场喷泉区",
             "20,150 70,70 150,40 230,70 280,150 230,230 150,260 70,230",
             40, 30),
            # 北门圆形池
            (2, "北门迎宾喷泉区",
             "circle",
             390, 60),
            # 东侧静水池
            (3, "东侧静水池",
             "30,200 110,120 210,150 250,240 140,270 40,250",
             390, 330),
        ],
    )
    pools = [
        (1, 1, "中央主水池", 1.2, "pond"),
        (2, 2, "北门圆形水池", 0.9, "round"),
        (3, 3, "东侧静水池", 0.8, "pond"),
    ]
    conn.executemany(
        "INSERT INTO pools(id,zone_id,name,depth_m,shape) VALUES(?,?,?,?,?)", pools)

    conn.executemany(
        "INSERT INTO pumps(id,pool_id,code,name,power_kw,status) VALUES(?,?,?,?,?,?)",
        [
            (1, 1, "P-101", "中央主泵 A", 7.5, "idle"),
            (2, 1, "P-102", "中央主泵 B", 7.5, "idle"),
            (3, 2, "P-201", "北门循环泵", 4.0, "idle"),
            (4, 3, "P-301", "东侧循环泵", 3.0, "idle"),
        ],
    )

    conn.executemany(
        "INSERT INTO nozzle_groups(id,pool_id,code,name,nozzle_count,layout,blocked) VALUES(?,?,?,?,?,?,?)",
        [
            (1, 1, "N-C1", "中心高喷组", 6, "150,60", 0),
            (2, 1, "N-C2", "环形花柱组", 24, "105,110;195,110;105,190;195,190", 0),
            (3, 2, "N-N1", "迎宾伞喷组", 12, "150,80;100,150;200,150", 0),
            (4, 3, "N-E1", "静水涌泉组", 8, "90,150;190,210", 0),
        ],
    )

    conn.executemany(
        "INSERT INTO run_schedules(id,pool_id,start_at,end_at,days,enabled) VALUES(?,?,?,?,?,?)",
        [
            (1, 1, "09:00", "11:30", "1234567", 1),
            (2, 1, "14:00", "17:00", "1234567", 1),
            (3, 2, "08:30", "10:30", "12345", 1),
            (4, 2, "18:00", "20:00", "67", 1),
            (5, 3, "10:00", "11:00", "1234567", 1),
        ],
    )

    # 近 24 小时演示读数
    rows = []
    rid = 1
    for hours_ago in range(24, -1, -3):
        ts = (now - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M")
        rows.append((rid, 1, ts, 0.72 + (hours_ago % 2) * 0.02, 2.4 + (rid % 3) * 0.4, 0.55))
        rid += 1
        rows.append((rid, 2, ts, 0.68, 3.1 + (rid % 2) * 0.3, 0.48))
        rid += 1
        rows.append((rid, 3, ts, 0.75, 1.8, 0.62))
        rid += 1
    conn.executemany(
        "INSERT INTO water_readings(id,pool_id,ts,level,turbidity,chlorine,kind) VALUES(?,?,?,?,?,?, 'auto')",
        rows)
    conn.commit()
