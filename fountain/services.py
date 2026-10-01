"""领域服务：启停校验、计划互斥、观察期、清池/维修/复开、事件处置。

所有写操作都在 ``BEGIN IMMEDIATE`` 事务中完成；``run_sessions`` 的
UNIQUE(schedule_id, run_date) 与事务共同保证“两人并发启动仅一次生效”。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from . import adapters, config


# ---------------------------------------------------------------- 基础工具

def now_dt() -> datetime:
    return datetime.now()


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def today_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _row(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()


def _rows(conn, sql, args=()):
    return conn.execute(sql, args).fetchall()


def log_event(conn, pool_id: Optional[int], kind: str, message: str,
              level: str = "warn") -> int:
    cur = conn.execute(
        "INSERT INTO events(pool_id, ts, kind, level, message) VALUES (?,?,?,?,?)",
        (pool_id, iso(now_dt()), kind, level, message))
    return cur.lastrowid


# ---------------------------------------------------------------- 读模型

def get_pool(conn, pool_id: int):
    return _row(conn, "SELECT * FROM pools WHERE id=?", (pool_id,))


def list_pools(conn):
    return _rows(conn, """
        SELECT p.*, z.name AS zone_name FROM pools p
        JOIN zones z ON z.id = p.zone_id ORDER BY z.id, p.id""")


def pool_snapshot(conn, pool_id: int, now: Optional[datetime] = None) -> dict:
    """区域总览/状态判断用：池 + 泵 + 喷头 + 最新读数 + 锁定状态。"""
    now = now or now_dt()
    pool = get_pool(conn, pool_id)
    pumps = _rows(conn, "SELECT * FROM pumps WHERE pool_id=? ORDER BY code", (pool_id,))
    groups = _rows(conn, "SELECT * FROM nozzle_groups WHERE pool_id=? ORDER BY id", (pool_id,))
    latest = _row(conn,
        "SELECT * FROM samples WHERE pool_id=? ORDER BY ts DESC, id DESC LIMIT 1",
        (pool_id,))
    telem = adapters.read_telemetry(pool_id, pool["design_level"], now)
    lock = pool_lock_reason(conn, pool_id, now)
    running = _row(conn, """
        SELECT * FROM run_sessions WHERE pool_id=? AND run_date=?
        AND status='running' LIMIT 1""", (pool_id, today_str(now)))
    return {"pool": pool, "pumps": pumps, "groups": groups,
            "latest": latest, "telemetry": telem,
            "lock": lock, "running": running}


def pool_lock_reason(conn, pool_id: int, now: Optional[datetime] = None) -> str:
    """水池是否被清池/维修锁定（锁定即暂停运行计划）。"""
    now = now or now_dt()
    clean = _row(conn,
        "SELECT * FROM clean_tasks WHERE pool_id=? AND status IN ('active','awaiting_retest') LIMIT 1",
        (pool_id,))
    if clean:
        if clean["status"] == "active":
            return "水池锁定：清池作业中"
        return "水池锁定：清池完成，待复测复开"
    fault = _row(conn,
        "SELECT * FROM faults WHERE pool_id=? AND status IN ('open','repairing','awaiting_retest') LIMIT 1",
        (pool_id,))
    if fault:
        label = {"open": "故障停运待修", "repairing": "维修中",
                 "awaiting_retest": "维修完成，待复测复开"}[fault["status"]]
        return f"水池锁定：{label}"
    return ""


def observation_remaining(conn, pool_id: int, now: Optional[datetime] = None) -> int:
    """投药观察期剩余分钟数；0 表示不在观察期。"""
    now = now or now_dt()
    row = _row(conn, """
        SELECT observe_until FROM chemicals
        WHERE pool_id=? ORDER BY observe_until DESC, id DESC LIMIT 1""", (pool_id,))
    if not row:
        return 0
    until = datetime.strptime(row["observe_until"], "%Y-%m-%d %H:%M:%S")
    return max(0, int((until - now).total_seconds() // 60) +
               (1 if (until - now).total_seconds() % 60 else 0))


def unresolved_overdose(conn, pool_id: int) -> bool:
    return _row(conn, """
        SELECT 1 FROM events WHERE pool_id=? AND kind='chemical_overdose'
        AND resolution='' ORDER BY id DESC LIMIT 1""", (pool_id,)) is not None


def unresolved_event(conn, pool_id: int, kind: str) -> bool:
    """某类事件是否尚未处置（如游客进入封控区）。"""
    return _row(conn, """
        SELECT 1 FROM events WHERE pool_id=? AND kind=? AND resolution=''
        ORDER BY id DESC LIMIT 1""", (pool_id, kind)) is not None


# ---------------------------------------------------------------- 启动前校验

def evaluate_start(conn, pool_id: int, now: Optional[datetime] = None,
                   persist: bool = True) -> dict:
    """到点启动前校验：水位、水质、设备、天气、观察期、锁定。

    返回 {ok, checks:[...], telemetry, reason}。
    """
    now = now or now_dt()
    pool = get_pool(conn, pool_id)
    telem = adapters.read_telemetry(pool_id, pool["design_level"], now)

    if persist:
        # 节流：调度器每 10 秒 tick，避免 blocked 重试把采样表刷满
        last = _row(conn, """SELECT ts FROM samples WHERE pool_id=? AND source='adapter'
            ORDER BY ts DESC, id DESC LIMIT 1""", (pool_id,))
        fresh = False
        if last:
            try:
                fresh = (now - datetime.strptime(
                    last["ts"], "%Y-%m-%d %H:%M:%S")).total_seconds() < 90
            except ValueError:
                fresh = False
        if not fresh:
            conn.execute("""INSERT INTO samples(pool_id, ts, water_level, turbidity,
                             chlorine, source, note)
                            VALUES (?,?,?,?,?,'adapter','启动前校验')""",
                         (pool_id, iso(now), telem["water_level"],
                          telem["turbidity"], telem["chlorine"]))

    checks = []

    def add(name: str, ok: bool, detail: str):
        checks.append({"name": name, "ok": ok, "detail": detail})

    lock = pool_lock_reason(conn, pool_id, now)
    add("水池状态", not lock, lock or "未锁定")

    obs = observation_remaining(conn, pool_id, now)
    add("投药观察期", obs == 0,
        f"剩余 {obs} 分钟" if obs else "观察期已过")

    add("药剂超量处置", not unresolved_overdose(conn, pool_id),
        "存在超量投加，待复测" if unresolved_overdose(conn, pool_id) else "无")

    add("封控安全", not unresolved_event(conn, pool_id, "visitor_intrusion"),
        "封控区入侵未解除，禁止启动" if unresolved_event(conn, pool_id, "visitor_intrusion")
        else "封控正常")

    w = telem["weather"]
    wreason = adapters.weather_blocks(w)
    add("天气", wreason is None,
        wreason or f"{w['code']}，{w['temp_c']}℃，{w['wind_level']}级风")

    lvl_ok = config.WATER_LEVEL_MIN <= telem["water_level"] <= config.WATER_LEVEL_MAX
    add("水位", lvl_ok,
        f"{telem['water_level']:.2f} m（允许 {config.WATER_LEVEL_MIN}~{config.WATER_LEVEL_MAX}）")

    turb_ok = telem["turbidity"] <= config.TURBIDITY_MAX
    add("浊度", turb_ok,
        f"{telem['turbidity']:.1f} NTU（≤ {config.TURBIDITY_MAX}）")

    cl_ok = config.CHLORINE_MIN <= telem["chlorine"] <= config.CHLORINE_MAX
    add("余氯", cl_ok,
        f"{telem['chlorine']:.2f} mg/L（{config.CHLORINE_MIN}~{config.CHLORINE_MAX}）")

    bad_pumps = _rows(conn,
        "SELECT * FROM pumps WHERE pool_id=? AND status IN ('fault','maintenance')",
        (pool_id,))
    add("循环泵", not bad_pumps and telem["pump_temp"] <= config.PUMP_TEMP_MAX,
        "；".join([f"{p['code']} {p['status']}" for p in bad_pumps]) or
        (f"泵温 {telem['pump_temp']:.0f}℃ 过热（≥{config.PUMP_TEMP_MAX}）"
         if telem["pump_temp"] > config.PUMP_TEMP_MAX else "泵组正常"))

    # 喷头组：库里的堵塞标记 + 适配器即时信号
    clogged_groups = [g for g in _rows(conn,
        "SELECT * FROM nozzle_groups WHERE pool_id=?", (pool_id,)) if g["clogged"]]
    add("喷头组", not clogged_groups and not telem["clogged"],
        "喷头堵塞：" + "、".join(g["code"] for g in clogged_groups) if
        (clogged_groups or telem["clogged"]) else "喷头组通畅")

    failed = [c for c in checks if not c["ok"]]
    return {"ok": not failed, "checks": checks, "telemetry": telem,
            "reason": "；".join(f"{c['name']}：{c['detail']}" for c in failed)}


# ---------------------------------------------------------------- 启停

def attempt_start(conn, schedule_id: int, operator: str = "scheduler",
                  manual: bool = False, now: Optional[datetime] = None) -> dict:
    """按计划启动。每日每计划仅一次生效（可从 blocked 重试到 running）。

    manual=True 表示人工现场启动：条件不满足时按“误启动”拦截并留痕。
    """
    now = now or now_dt()
    date = today_str(now)
    with _tx(conn):
        sched = _row(conn, "SELECT * FROM schedules WHERE id=?", (schedule_id,))
        if not sched:
            return {"ok": False, "reason": "计划不存在"}
        if not sched["enabled"]:
            return {"ok": False, "reason": "计划已停用"}

        existing = _row(conn,
            "SELECT * FROM run_sessions WHERE schedule_id=? AND run_date=?",
            (schedule_id, date))

        # 幂等：已在运行 -> 第二次（并发）调用直接返回，仅一次生效
        if existing and existing["status"] == "running":
            return {"ok": True, "idempotent": True, "session_id": existing["id"],
                    "status": "running", "reason": "已在运行，重复启动仅一次生效"}
        # 当天已正常停运，不再重启
        if existing and existing["status"] == "stopped":
            return {"ok": False, "idempotent": True, "session_id": existing["id"],
                    "status": "stopped", "reason": "今日运行已结束"}

        result = evaluate_start(conn, sched["pool_id"], now, persist=True)

        if existing:  # blocked / weather_stop 的重试
            session_id = existing["id"]
        else:
            cur = conn.execute("""
                INSERT INTO run_sessions(schedule_id, pool_id, run_date, status,
                                         block_reason)
                VALUES (?,?,?, 'blocked', '')""",
                (schedule_id, sched["pool_id"], date))
            session_id = cur.lastrowid

        if not result["ok"]:
            conn.execute("UPDATE run_sessions SET status='blocked', block_reason=? WHERE id=?",
                         (result["reason"], session_id))
            if manual:
                log_event(conn, sched["pool_id"], "false_start",
                          f"误启动拦截（{operator}）：{result['reason']}", "warn")
            _log_block_events(conn, sched["pool_id"], result["telemetry"], result["reason"])
            return {"ok": False, "session_id": session_id, "status": "blocked",
                    "reason": result["reason"], "checks": result["checks"]}

        conn.execute("""UPDATE run_sessions
                        SET status='running', block_reason='', started_at=?
                        WHERE id=?""", (iso(now), session_id))
        conn.execute("UPDATE pumps SET status='running' WHERE pool_id=? AND status='idle'",
                     (sched["pool_id"],))
        log_event(conn, sched["pool_id"], "run_start",
                  f"喷泉启动（{ '人工' if manual else '计划' }：{operator}）", "info")
        return {"ok": True, "session_id": session_id, "status": "running",
                "reason": "", "checks": result["checks"]}


def _tx(conn):
    from .db import transaction
    return transaction(conn)


def _log_block_events(conn, pool_id: int, telem: dict, reason: str) -> None:
    """把阻断原因拆解为各自独立的处置事件（当日同类去重）。"""
    def has_today(kind):
        return _row(conn, """SELECT 1 FROM events WHERE pool_id=? AND kind=?
            AND date(ts)=date('now','localtime')""", (pool_id, kind)) is not None

    if not config.WATER_LEVEL_MIN <= telem["water_level"] <= config.WATER_LEVEL_MAX:
        if not has_today("low_water"):
            log_event(conn, pool_id, "low_water",
                      f"水位异常 {telem['water_level']:.2f} m，暂停启动并提示补水", "danger")
    if telem["turbidity"] > config.TURBIDITY_MAX or not (
            config.CHLORINE_MIN <= telem["chlorine"] <= config.CHLORINE_MAX):
        if not has_today("water_exceed"):
            log_event(conn, pool_id, "water_exceed",
                      f"水质超限（浊度 {telem['turbidity']:.1f} NTU，余氯 "
                      f"{telem['chlorine']:.2f} mg/L），暂停启动", "danger")
    if telem["pump_temp"] > config.PUMP_TEMP_MAX and not has_today("pump_overheat"):
        log_event(conn, pool_id, "pump_overheat",
                  f"泵体过热 {telem['pump_temp']:.0f}℃，停运待修", "danger")
        conn.execute("UPDATE pumps SET status='fault', fault_code='overheat' WHERE pool_id=?",
                     (pool_id,))


def stop_session(conn, session_id: int, reason: str = "计划结束",
                 operator: str = "scheduler") -> dict:
    with _tx(conn):
        s = _row(conn, "SELECT * FROM run_sessions WHERE id=?", (session_id,))
        if not s:
            return {"ok": False, "reason": "运行记录不存在"}
        if s["status"] in ("stopped", "blocked"):
            return {"ok": True, "idempotent": True, "reason": "已停运"}
        conn.execute("""UPDATE run_sessions SET status='stopped', stopped_at=?,
                        stop_reason=? WHERE id=?""",
                     (iso(now_dt()), reason, session_id))
        conn.execute("UPDATE pumps SET status='idle' WHERE pool_id=? AND status='running'",
                     (s["pool_id"],))
        log_event(conn, s["pool_id"], "run_stop", f"喷泉停运：{reason}（{operator}）", "info")
        return {"ok": True, "reason": reason}


# ---------------------------------------------------------------- 补水 / 药剂 / 采样

def add_refill(conn, pool_id: int, amount_liters: float, operator: str,
               note: str = "") -> int:
    with _tx(conn):
        cur = conn.execute("""INSERT INTO refill_logs(pool_id, ts, amount_liters,
                              operator, note) VALUES (?,?,?,?,?)""",
                           (pool_id, iso(now_dt()), amount_liters, operator, note))
        log_event(conn, pool_id, "refill", f"补水 {amount_liters:.0f} L（{operator}）", "info")
        return cur.lastrowid


def add_chemical(conn, pool_id: int, agent: str, amount: float,
                 operator: str, minutes: int = config.OBSERVATION_MINUTES,
                 note: str = "") -> dict:
    """药剂投放：进入观察期；超量单独留痕并要求复测后才能启动。"""
    now = now_dt()
    observe_until = now + timedelta(minutes=minutes)
    overdose = amount > config.CHEMICAL_DOSE_MAX
    with _tx(conn):
        conn.execute("""INSERT INTO chemicals(pool_id, ts, agent, amount, unit,
                          operator, observe_until, overdose, note)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                     (pool_id, iso(now), agent, amount, "L", operator,
                      iso(observe_until), int(overdose), note))
        if overdose:
            # 正在运行则立即停喷，进入超量处置
            running = _row(conn, """SELECT id FROM run_sessions WHERE pool_id=?
                AND run_date=? AND status='running'""", (pool_id, today_str(now)))
            if running:
                conn.execute("""UPDATE run_sessions SET status='blocked',
                    stopped_at=?, block_reason='药剂超量紧急停运' WHERE id=?""",
                             (iso(now), running["id"]))
                conn.execute("UPDATE pumps SET status='idle' WHERE pool_id=? AND status='running'",
                             (pool_id,))
            log_event(conn, pool_id, "chemical_overdose",
                      f"药剂超量：{agent} {amount} L > 单次上限 "
                      f"{config.CHEMICAL_DOSE_MAX} L，停止运行等待复测（{operator}）",
                      "danger")
            return {"ok": False, "overdose": True,
                    "reason": "投加量超上限，已进入超量处置流程"}
        log_event(conn, pool_id, "chemical_dose",
                  f"投加 {agent} {amount} L，进入 {minutes} 分钟观察期（{operator}）",
                  "info")
        return {"ok": True, "overdose": False, "observe_until": iso(observe_until)}


def add_manual_sample(conn, pool_id: int, water_level: float, turbidity: float,
                      chlorine: float, operator: str, note: str = "") -> int:
    with _tx(conn):
        cur = conn.execute("""INSERT INTO samples(pool_id, ts, water_level,
            turbidity, chlorine, source, note) VALUES (?,?,?,?,?,'manual',?)""",
            (pool_id, iso(now_dt()), water_level, turbidity, chlorine, note))
        return cur.lastrowid


# ---------------------------------------------------------------- 清池

def start_clean(conn, pool_id: int, keeper: str, note: str = "") -> dict:
    """清池锁定整个水池并暂停计划；与运行计划互斥。"""
    now = now_dt()
    with _tx(conn):
        active = _row(conn,
            "SELECT id FROM clean_tasks WHERE pool_id=? AND status IN ('active','awaiting_retest')",
            (pool_id,))
        if active:
            return {"ok": False, "reason": "该水池已有进行中的清池任务"}
        running = _row(conn, """SELECT id FROM run_sessions WHERE pool_id=?
            AND run_date=? AND status='running'""", (pool_id, today_str(now)))
        if running:
            return {"ok": False, "reason": "运行计划正在执行，互斥：请先停运再清池"}
        cur = conn.execute("""INSERT INTO clean_tasks(pool_id, started_at, keeper,
                              status, note) VALUES (?,?,?, 'active', ?)""",
                           (pool_id, iso(now), keeper, note))
        conn.execute("UPDATE pumps SET status='maintenance' WHERE pool_id=? AND status='idle'",
                     (pool_id,))
        log_event(conn, pool_id, "clean_start", f"清池开始，水池锁定（养护员 {keeper}）", "warn")
        return {"ok": True, "task_id": cur.lastrowid}


def finish_clean(conn, task_id: int, keeper: str) -> dict:
    """清池作业结束：进入待复测状态，等待水质复测 + 喷头试运行。"""
    with _tx(conn):
        t = _row(conn, "SELECT * FROM clean_tasks WHERE id=?", (task_id,))
        if not t or t["status"] != "active":
            return {"ok": False, "reason": "清池任务不在作业中"}
        conn.execute("UPDATE clean_tasks SET status='awaiting_retest', finished_at=? WHERE id=?",
                     (iso(now_dt()), task_id))
        log_event(conn, t["pool_id"], "clean_finish",
                  f"清池作业完成（{keeper}），待水质复测与喷头试运行后复开", "info")
        return {"ok": True}


# ---------------------------------------------------------------- 故障维修

def report_fault(conn, pool_id: int, kind: str, detail: str,
                 pump_id: Optional[int] = None) -> dict:
    """登记故障：喷头堵塞/泵体过热等，停运并锁定水池。"""
    now = now_dt()
    with _tx(conn):
        running = _row(conn, """SELECT id FROM run_sessions WHERE pool_id=?
            AND run_date=? AND status IN ('running','weather_stop')""",
            (pool_id, today_str(now)))
        if running:
            conn.execute("""UPDATE run_sessions SET status='blocked', stopped_at=?,
                block_reason=? WHERE id=?""",
                (iso(now), f"故障停运：{detail}", running["id"]))
        conn.execute("UPDATE pumps SET status='fault' WHERE pool_id=? AND status IN ('idle','running')",
                     (pool_id,))
        if pump_id:
            conn.execute("UPDATE pumps SET status='fault', fault_code=? WHERE id=?",
                         (kind, pump_id))
        if kind == "nozzle_clog":
            conn.execute("UPDATE nozzle_groups SET clogged=1 WHERE pool_id=?", (pool_id,))
        cur = conn.execute("""INSERT INTO faults(pool_id, pump_id, ts, kind, detail,
                              status) VALUES (?,?,?,?,?, 'open')""",
                           (pool_id, pump_id, iso(now), kind, detail))
        log_event(conn, pool_id, "fault", f"故障停运：{detail}", "danger")
        return {"ok": True, "fault_id": cur.lastrowid}


def repair_fault(conn, fault_id: int, keeper: str, note: str = "") -> dict:
    with _tx(conn):
        f = _row(conn, "SELECT * FROM faults WHERE id=?", (fault_id,))
        if not f or f["status"] not in ("open", "repairing"):
            return {"ok": False, "reason": "故障单不可维修"}
        conn.execute("""UPDATE faults SET status='repairing', repair_note=? WHERE id=?""",
                     (note, fault_id))
        if f["kind"] == "nozzle_clog":
            conn.execute("UPDATE nozzle_groups SET clogged=0 WHERE pool_id=?", (f["pool_id"],))
        conn.execute("UPDATE pumps SET fault_code=NULL WHERE pool_id=?", (f["pool_id"],))
        conn.execute("""UPDATE faults SET status='awaiting_retest', repaired_at=?,
            repair_note=? WHERE id=?""", (iso(now_dt()), note or f"养护员 {keeper} 已修复", fault_id))
        log_event(conn, f["pool_id"], "fault_repaired",
                  f"维修完成（{keeper}），待复测复开", "info")
        return {"ok": True}


# ---------------------------------------------------------------- 复测 / 试运行 / 复开

def _reading_ok(telem: dict) -> tuple[bool, str]:
    probs = []
    if not config.WATER_LEVEL_MIN <= telem["water_level"] <= config.WATER_LEVEL_MAX:
        probs.append(f"水位 {telem['water_level']:.2f} m")
    if telem["turbidity"] > config.TURBIDITY_MAX:
        probs.append(f"浊度 {telem['turbidity']:.1f} NTU")
    if not config.CHLORINE_MIN <= telem["chlorine"] <= config.CHLORINE_MAX:
        probs.append(f"余氯 {telem['chlorine']:.2f} mg/L")
    return (not probs, "；".join(probs))


def retest_water(conn, pool_id: int, operator: str,
                 now: Optional[datetime] = None) -> dict:
    """维修/清池结束后的水质复测；读数取适配器（演示场景可注入）。"""
    now = now or now_dt()
    pool = get_pool(conn, pool_id)
    telem = adapters.read_telemetry(pool_id, pool["design_level"], now)
    ok, bad = _reading_ok(telem)
    with _tx(conn):
        conn.execute("""INSERT INTO samples(pool_id, ts, water_level, turbidity,
            chlorine, source, note) VALUES (?,?,?,?,?,'retest',?)""",
            (pool_id, iso(now), telem["water_level"], telem["turbidity"],
             telem["chlorine"], ("复开水质复测" if ok else f"复测不达标：{bad}")))
        if ok:
            conn.execute("""UPDATE clean_tasks SET retest_passed=1
                WHERE pool_id=? AND status='awaiting_retest'""", (pool_id,))
            conn.execute("""UPDATE faults SET retest_passed=1
                WHERE pool_id=? AND status='awaiting_retest'""", (pool_id,))
            conn.execute("""UPDATE events SET resolution='复测达标', handled_by=?,
                handled_at=? WHERE pool_id=? AND kind IN ('water_exceed','chemical_overdose')
                AND resolution=''""", (operator, iso(now), pool_id))
            log_event(conn, pool_id, "retest", f"水质复测达标（{operator}）", "info")
        else:
            log_event(conn, pool_id, "retest",
                      f"水质复测不达标（{operator}）：{bad}", "warn")
        return {"ok": ok, "bad": bad,
                "telemetry": {**{k: v for k, v in telem.items() if k != "ts"},
                              "ts": iso(now)}}


def nozzle_trial(conn, pool_id: int, operator: str) -> dict:
    """喷头试运行：逐组确认通畅、泵温正常。"""
    with _tx(conn):
        groups = _rows(conn, "SELECT * FROM nozzle_groups WHERE pool_id=?", (pool_id,))
        clogged = [g["code"] for g in groups if g["clogged"]]
        telem = adapters.read_telemetry(pool_id, get_pool(conn, pool_id)["design_level"])
        if clogged or telem["clogged"]:
            log_event(conn, pool_id, "nozzle_trial",
                      f"喷头试运行失败：{ '、'.join(clogged) or '存在堵塞' }", "warn")
            return {"ok": False, "reason": "喷头仍堵塞"}
        if telem["pump_temp"] > config.PUMP_TEMP_MAX:
            return {"ok": False, "reason": f"泵温 {telem['pump_temp']:.0f}℃ 过热"}
        conn.execute("""UPDATE clean_tasks SET nozzle_trial_passed=1
            WHERE pool_id=? AND status='awaiting_retest'""", (pool_id,))
        conn.execute("""UPDATE faults SET nozzle_trial_passed=1
            WHERE pool_id=? AND status='awaiting_retest'""", (pool_id,))
        conn.execute("UPDATE pumps SET status='idle', fault_code=NULL WHERE pool_id=?",
                     (pool_id,))
        log_event(conn, pool_id, "nozzle_trial",
                  f"喷头试运行通过，泵组恢复备用（{operator}）", "info")
        return {"ok": True}


def request_reopen(conn, ref_type: str, ref_id: int, keeper: str) -> dict:
    """养护员发起复开申请（清池或维修结束后）。"""
    with _tx(conn):
        if ref_type == "clean":
            ref = _row(conn, "SELECT * FROM clean_tasks WHERE id=?", (ref_id,))
        else:
            ref = _row(conn, "SELECT * FROM faults WHERE id=?", (ref_id,))
        if not ref:
            return {"ok": False, "reason": "关联任务不存在"}
        if ref["status"] != "awaiting_retest":
            return {"ok": False, "reason": "任务尚未进入待复测阶段"}
        dup = _row(conn, """SELECT id FROM reopen_requests WHERE ref_type=? AND ref_id=?
            AND status='pending'""", (ref_type, ref_id))
        if dup:
            return {"ok": False, "reason": "已存在待审批的复开申请", "request_id": dup["id"]}
        cur = conn.execute("""INSERT INTO reopen_requests(pool_id, ref_type, ref_id,
            requested_by, requested_at, status) VALUES (?,?,?,?,?, 'pending')""",
            (ref["pool_id"], ref_type, ref_id, keeper, iso(now_dt())))
        log_event(conn, ref["pool_id"], "reopen_request",
                  f"养护员 {keeper} 申请复开，待主管审批", "info")
        return {"ok": True, "request_id": cur.lastrowid}


def approve_reopen(conn, request_id: int, reviewer: str, role: str) -> dict:
    """主管批准复开。越权拒绝；复测或试运行缺失则退回（留痕）。"""
    if role != config.ROLE_SUPERVISOR:
        return {"ok": False, "reason": "权限不足：越限复开仅主管可批准"}
    with _tx(conn):
        r = _row(conn, "SELECT * FROM reopen_requests WHERE id=?", (request_id,))
        if not r or r["status"] != "pending":
            return {"ok": False, "reason": "申请不存在或已处理"}
        if r["ref_type"] == "clean":
            ref = _row(conn, "SELECT * FROM clean_tasks WHERE id=?", (r["ref_id"],))
        else:
            ref = _row(conn, "SELECT * FROM faults WHERE id=?", (r["ref_id"],))
        missing = []
        if not ref["retest_passed"]:
            missing.append("水质复测")
        if not ref["nozzle_trial_passed"]:
            missing.append("喷头试运行")
        if missing:
            conn.execute("""UPDATE reopen_requests SET status='rejected', reviewer=?,
                reviewed_at=?, reject_reason=? WHERE id=?""",
                (reviewer, iso(now_dt()), "复开退回：尚未完成" + "、".join(missing), request_id))
            log_event(conn, r["pool_id"], "reopen_returned",
                      f"复开退回（主管 {reviewer}）：缺{'、'.join(missing)}", "warn")
            return {"ok": False, "returned": True,
                    "reason": "复开退回：尚未完成" + "、".join(missing)}
        if r["ref_type"] == "clean":
            conn.execute("UPDATE clean_tasks SET status='done' WHERE id=?", (r["ref_id"],))
        else:
            conn.execute("UPDATE faults SET status='resolved' WHERE id=?", (r["ref_id"],))
        conn.execute("UPDATE pumps SET status='idle', fault_code=NULL WHERE pool_id=?",
                     (r["pool_id"],))
        conn.execute("""UPDATE reopen_requests SET status='approved', reviewer=?,
            reviewed_at=? WHERE id=?""", (reviewer, iso(now_dt()), request_id))
        log_event(conn, r["pool_id"], "reopen_approved",
                  f"主管 {reviewer} 批准复开，水池解锁", "info")
        return {"ok": True}


def reject_reopen(conn, request_id: int, reviewer: str, role: str, reason: str) -> dict:
    if role != config.ROLE_SUPERVISOR:
        return {"ok": False, "reason": "权限不足：仅主管可审批"}
    with _tx(conn):
        r = _row(conn, "SELECT * FROM reopen_requests WHERE id=?", (request_id,))
        if not r or r["status"] != "pending":
            return {"ok": False, "reason": "申请不存在或已处理"}
        conn.execute("""UPDATE reopen_requests SET status='rejected', reviewer=?,
            reviewed_at=?, reject_reason=? WHERE id=?""",
            (reviewer, iso(now_dt()), reason, request_id))
        log_event(conn, r["pool_id"], "reopen_returned",
                  f"复开申请被退回（主管 {reviewer}）：{reason}", "warn")
        return {"ok": True}


# ---------------------------------------------------------------- 封控/事件处置

def report_visitor(conn, pool_id: int, detail: str = "游客进入封控区") -> dict:
    """游客进入封控区：立即停喷，养护员现场处置后解除。"""
    now = now_dt()
    with _tx(conn):
        running = _row(conn, """SELECT id FROM run_sessions WHERE pool_id=?
            AND run_date=? AND status='running'""", (pool_id, today_str(now)))
        if running:
            conn.execute("""UPDATE run_sessions SET status='blocked', stopped_at=?,
                block_reason='封控区入侵，紧急停喷' WHERE id=?""",
                         (iso(now), running["id"]))
            conn.execute("UPDATE pumps SET status='idle' WHERE pool_id=? AND status='running'",
                         (pool_id,))
        eid = log_event(conn, pool_id, "visitor_intrusion",
                        f"{detail}，已紧急停喷并封控", "danger")
        return {"ok": True, "event_id": eid}


def resolve_event(conn, event_id: int, keeper: str, resolution: str) -> dict:
    """养护员处置事件（补水后解除、劝离游客等）。"""
    with _tx(conn):
        e = _row(conn, "SELECT * FROM events WHERE id=?", (event_id,))
        if not e:
            return {"ok": False, "reason": "事件不存在"}
        conn.execute("UPDATE events SET handled_by=?, handled_at=?, resolution=? WHERE id=?",
                     (keeper, iso(now_dt()), resolution, event_id))
        return {"ok": True}


# ---------------------------------------------------------------- 天气停运（调度用）

def weather_stop_running(conn, now: Optional[datetime] = None) -> list:
    """对正在运行且天气不允许的水池执行天气停运。"""
    now = now or now_dt()
    stopped = []
    weather = adapters.get_weather(now)
    reason = adapters.weather_blocks(weather)
    if not reason:
        return stopped
    with _tx(conn):
        rows = _rows(conn, "SELECT id, pool_id FROM run_sessions WHERE status='running'")
        for s in rows:
            conn.execute("""UPDATE run_sessions SET status='weather_stop',
                stopped_at=?, stop_reason=? WHERE id=?""",
                         (iso(now), reason, s["id"]))
            conn.execute("UPDATE pumps SET status='idle' WHERE pool_id=? AND status='running'",
                         (s["pool_id"],))
            log_event(conn, s["pool_id"], "weather_stop", reason, "warn")
            stopped.append(s["pool_id"])
    return stopped


def schedule_stop_due(conn, now: Optional[datetime] = None) -> list:
    """到结束时间正常停运。"""
    now = now or now_dt()
    hhmm = now.strftime("%H:%M")
    out = []
    with _tx(conn):
        rows = _rows(conn, """SELECT rs.* FROM run_sessions rs
            JOIN schedules s ON s.id=rs.schedule_id
            WHERE rs.status='running' AND s.end_at<=? AND rs.run_date=?""",
                     (hhmm, today_str(now)))
        for s in rows:
            conn.execute("""UPDATE run_sessions SET status='stopped', stopped_at=?,
                stop_reason='计划结束' WHERE id=?""", (iso(now), s["id"]))
            conn.execute("UPDATE pumps SET status='idle' WHERE pool_id=? AND status='running'",
                         (s["pool_id"],))
            out.append(s["id"])
    return out
