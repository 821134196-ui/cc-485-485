"""演示数据：区域、水池、泵、喷头组、计划与基础采样。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from . import adapters, config, services
from .db import connect, init_db

NOZZLE_LINES_RECT = [
    [12, 88, 26, 88], [32, 88, 46, 88], [54, 88, 68, 88], [74, 88, 88, 88],
    [12, 12, 26, 12], [32, 12, 46, 12], [54, 12, 68, 12], [74, 12, 88, 12],
]
NOZZLE_LINES_OVAL = [
    [20, 75, 34, 75], [43, 80, 57, 80], [66, 75, 80, 75],
    [20, 25, 34, 25], [43, 20, 57, 20], [66, 25, 80, 25],
]


def seed(conn, reset: bool = False) -> None:
    init_db(conn)
    if conn.execute("SELECT COUNT(*) c FROM zones").fetchone()["c"]:
        return
    if reset:
        for t in ("events", "reopen_requests", "faults", "clean_tasks", "chemicals",
                  "refill_logs", "samples", "run_sessions", "schedules",
                  "nozzle_groups", "pumps", "pools", "zones", "attachments"):
            conn.execute(f"DELETE FROM {t}")

    cur = conn.cursor()
    z1 = cur.execute("INSERT INTO zones(name, note) VALUES (?,?)",
                     ("中央湖区", "公园主轴，开放时间常开")).lastrowid
    z2 = cur.execute("INSERT INTO zones(name, note) VALUES (?,?)",
                     ("儿童活动区", "紧邻戏水区域，安全优先")).lastrowid

    pools = [
        (z1, "音乐喷泉", "rect", {"x": 8, "y": 10, "w": 42, "h": 72}, 1.20, "rect"),
        (z1, "镜面水池", "oval", {"x": 56, "y": 14, "w": 36, "h": 40}, 1.00, "oval"),
        (z2, "戏水浅池", "rect", {"x": 56, "y": 58, "w": 36, "h": 30}, 0.90, "rect"),
    ]
    pool_ids = []
    for zone_id, name, shape, fp, level, _ in pools:
        pid = cur.execute("""INSERT INTO pools(zone_id, name, shape, footprint,
            design_level) VALUES (?,?,?,?,?)""",
            (zone_id, name, shape, json.dumps(fp), level)).lastrowid
        pool_ids.append(pid)

    pump_specs = [
        (pool_ids[0], "P-101", "主循环泵"), (pool_ids[0], "P-102", "变频辅泵"),
        (pool_ids[1], "P-201", "镜面循环泵"),
        (pool_ids[2], "P-301", "浅水循环泵"),
    ]
    for pid, code, name in pump_specs:
        cur.execute("INSERT INTO pumps(pool_id, code, name) VALUES (?,?,?)",
                    (pid, code, name))

    lines = {"rect": NOZZLE_LINES_RECT, "oval": NOZZLE_LINES_OVAL}
    for pid, _, _, _, _, shape in pools:
        group_lines = lines[shape]
        cur.execute("""INSERT INTO nozzle_groups(pool_id, code, name, position)
            VALUES (?,?,?,?)""",
            (pid, "NG-A", "外圈喷头组", json.dumps(group_lines[:4])))
        cur.execute("""INSERT INTO nozzle_groups(pool_id, code, name, position)
            VALUES (?,?,?,?)""",
            (pid, "NG-B", "内圈喷头组", json.dumps(group_lines[4:])))

    now = datetime.now()
    # 运行时段：无论何时启动演示，都让计划窗口覆盖当前时刻（便于看到自动启停）

    def window(start_delta_min: int, end_delta_min: int) -> tuple[str, str]:
        s = now + timedelta(minutes=start_delta_min)
        e = now + timedelta(minutes=end_delta_min)
        return (max(s, now.replace(hour=0, minute=0)).strftime("%H:%M"),
                min(e, now.replace(hour=23, minute=59)).strftime("%H:%M"))

    s1, e1 = window(-60, 120)    # 音乐喷泉
    s2, e2 = window(-30, 60)     # 镜面水池
    s3, e3 = window(0, 90)       # 戏水浅池
    for pid, st, en in [(pool_ids[0], s1, e1), (pool_ids[1], s2, e2),
                        (pool_ids[2], s3, e3)]:
        cur.execute("""INSERT INTO schedules(pool_id, start_at, end_at, enabled,
            created_at) VALUES (?,?,?,1,?)""",
                    (pid, st, en, services.iso(now)))

    conn.commit()

    # 近 24 小时演示采样（每 2 小时一条）
    for pid, design in [(pool_ids[0], 1.20), (pool_ids[1], 1.00), (pool_ids[2], 0.90)]:
        t = now - timedelta(hours=24)
        i = 0
        while t <= now:
            telem = adapters.read_telemetry(pid, design, t)
            conn.execute("""INSERT INTO samples(pool_id, ts, water_level, turbidity,
                chlorine, source, note) VALUES (?,?,?,?,?,'adapter','演示采样')""",
                (pid, services.iso(t), telem["water_level"], telem["turbidity"],
                 telem["chlorine"]))
            t += timedelta(hours=2)
            i += 1
    conn.commit()
    return pool_ids


def reset_and_seed(db_path=None):
    conn = connect(db_path)
    init_db(conn)
    for t in ("events", "reopen_requests", "faults", "clean_tasks", "chemicals",
              "refill_logs", "samples", "run_sessions", "schedules",
              "nozzle_groups", "pumps", "pools", "zones", "attachments"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    pool_ids = seed(conn)
    return conn, pool_ids
