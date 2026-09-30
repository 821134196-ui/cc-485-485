"""进程内调度器：定时启停、周期采样、故障边沿处置与复测/观察期提醒。"""
import threading
import time
from datetime import datetime
from typing import Callable

from . import config
from .adapters import adapter, chemical_cn
from .services import FAULT_TEXT, PlatformService, ts


class Scheduler:
    def __init__(self, service: PlatformService, interval: float = 5.0):
        self.svc = service
        self.conn = service.conn
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self._last_sample_minute = None
        self._schedule_attempted: set[str] = set()
        self._weather_bad = False
        self._reminded_dose: set[int] = set()
        self._reminded_reopen: set[int] = set()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="fountain-scheduler", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    # ---------- 主循环 ----------
    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception as exc:  # 调度异常不杀死线程
                print(f"[scheduler] tick error: {exc}")

    def tick(self):
        dt = self.svc.clock()
        minute_key = dt.strftime("%Y-%m-%d %H:%M")
        new_minute = minute_key != self._last_sample_minute
        if new_minute:
            self._last_sample_minute = minute_key
        self._check_weather()
        pool_ids = [r["id"] for r in self.conn.execute("SELECT id FROM pools").fetchall()]
        for pid in pool_ids:
            if new_minute:
                self._sample(pid, dt)
            self._check_running_equipment(pid)
            self._check_observe_reminder(pid)
        self._check_reopen_reminders()
        self._check_schedules(dt)

    # ---------- 天气停运 / 解除 ----------
    def _check_weather(self):
        bad = not adapter.weather()["allow_run"]
        if bad and not self._weather_bad:
            self._weather_bad = True
            for r in self.conn.execute("SELECT id FROM pools").fetchall():
                pid = r["id"]
                if self.svc.is_running(pid):
                    self.svc.stop_pool(pid, "气象联动", "雷暴大风预警，天气停运", action="abort")
                self.svc._raise_fault(pid, "weather", f"接入气象预警：{adapter.weather()['text']}")
        elif not bad and self._weather_bad:
            self._weather_bad = False
            self.conn.execute(
                "UPDATE faults SET status='resolved', resolution='气象预警解除' "
                "WHERE kind='weather' AND status IN ('open','repairing')")
            self.conn.commit()

    # ---------- 周期采样（每分钟一条） ----------
    def _sample(self, pool_id: int, dt: datetime):
        w = adapter.read_water(pool_id)
        self.conn.execute(
            "INSERT INTO water_readings(pool_id,ts,level,turbidity,chlorine,kind) "
            "VALUES(?,?,?,?,?, 'auto')",
            (pool_id, dt.strftime("%Y-%m-%d %H:%M:%S"),
             w["level"], w["turbidity"], w["chlorine"]))
        self.conn.commit()

        if w["level"] < config.WATER_LEVEL_MIN:
            if self.svc.is_running(pool_id):
                self.svc.stop_pool(pool_id, "水位监测", "水位不足，保护停运", action="abort")
            self.svc._raise_fault(pool_id, "level_low",
                                  f"采样水位 {w['level']:.0%} 低于下限 "
                                  f"{config.WATER_LEVEL_MIN:.0%}，请补水")
        if w["turbidity"] > config.TURBIDITY_MAX or not (
                config.CHLORINE_MIN <= w["chlorine"] <= config.CHLORINE_MAX):
            if self.svc.is_running(pool_id):
                self.svc.stop_pool(pool_id, "水质监测", "浊度/余氯超限，保护停运", action="abort")
            detail = (f"浊度 {w['turbidity']:.1f}NTU（≤{config.TURBIDITY_MAX:g}），"
                      f"余氯 {w['chlorine']:.2f}mg/L"
                      f"（{config.CHLORINE_MIN:g}~{config.CHLORINE_MAX:g}）")
            self.svc._raise_fault(pool_id, "quality", detail)

    # ---------- 运行中设备保护 ----------
    def _check_running_equipment(self, pool_id: int):
        for p in self.conn.execute("SELECT * FROM pumps WHERE pool_id=?", (pool_id,)).fetchall():
            p = dict(p)
            tele = adapter.read_pump(p["id"], p["status"] == "running")
            if tele["temp_c"] > config.PUMP_TEMP_MAX:
                if self.svc.is_running(pool_id):
                    self.svc.stop_pool(pool_id, "泵温监测", f"{p['code']} 泵体过热，保护停运",
                                       action="abort")
                self.conn.execute("UPDATE pumps SET status='fault', fault_code='overheat' WHERE id=?",
                                  (p["id"],))
                self.conn.commit()
                self.svc._raise_fault(pool_id, "overheat",
                                      f"{p['code']} 温度 {tele['temp_c']:.1f}℃ 超过 "
                                      f"{config.PUMP_TEMP_MAX:g}℃",
                                      pump_id=p["id"])
        for g in self.conn.execute("SELECT * FROM nozzle_groups WHERE pool_id=?", (pool_id,)).fetchall():
            g = dict(g)
            tele = adapter.read_nozzle_group(g["id"])
            if tele["blocked"]:
                if self.svc.is_running(pool_id):
                    self.svc.stop_pool(pool_id, "喷头监测", f"{g['code']} 压力不足疑似堵塞，保护停运",
                                       action="abort")
                self.conn.execute("UPDATE nozzle_groups SET blocked=1 WHERE id=?", (g["id"],))
                self.conn.commit()
                self.svc._raise_fault(pool_id, "nozzle_block",
                                      f"{g['code']}（{g['name']}）压力 "
                                      f"{tele['pressure_mpa']:.2f}MPa 低于 "
                                      f"{config.NOZZLE_PRESSURE_MIN:g}MPa")

    # ---------- 观察期结束提醒 ----------
    def _check_observe_reminder(self, pool_id: int):
        for d in self.conn.execute(
                "SELECT * FROM dosing_records WHERE pool_id=? ORDER BY id DESC", (pool_id,)).fetchall():
            d = dict(d)
            if d["id"] in self._reminded_dose:
                continue
            until = datetime.strptime(d["observe_until"], "%Y-%m-%d %H:%M:%S")
            if until <= self.svc.clock():
                self._reminded_dose.add(d["id"])
                self.conn.execute(
                    "INSERT INTO alerts(pool_id,ts,category,message) VALUES(?,?,?,?)",
                    (pool_id, ts(self.svc.clock()), "reminder",
                     f"{chemical_cn(d['chemical'])} 观察期已结束，可复核水质后恢复运行"))
                self.conn.commit()

    # ---------- 复测提醒 ----------
    def _check_reopen_reminders(self):
        for r in self.conn.execute(
                "SELECT * FROM reopen_requests WHERE status='pending'").fetchall():
            r = dict(r)
            if r["id"] in self._reminded_reopen:
                continue
            missing = []
            if not r["retest_reading_id"]:
                missing.append("水质复测")
            if r["nozzle_trial_ok"] != 1:
                missing.append("喷头试运行")
            if missing:
                self._reminded_reopen.add(r["id"])
                self.conn.execute(
                    "INSERT INTO alerts(pool_id,ts,category,message) VALUES(?,?,?,?)",
                    (r["pool_id"], ts(self.svc.clock()), "reminder",
                     f"复开申请 #{r['id']} 待完成：{'、'.join(missing)}"))
                self.conn.commit()

    # ---------- 定时启停 ----------
    def _check_schedules(self, dt: datetime):
        hm = dt.strftime("%H:%M")
        date = dt.strftime("%Y-%m-%d")
        for r in self.conn.execute("SELECT * FROM run_schedules WHERE enabled=1").fetchall():
            r = dict(r)
            if str(dt.isoweekday()) not in r["days"]:
                continue
            key = f"{r['id']}@{date}"
            if hm == r["start_at"] and key not in self._schedule_attempted:
                self._schedule_attempted.add(key)
                self.svc.start_pool(r["pool_id"], "计划调度", schedule_id=r["id"])
                self.conn.execute("UPDATE run_schedules SET last_run_date=? WHERE id=?",
                                  (date, r["id"]))
                self.conn.commit()
            elif hm == r["end_at"] and self.svc.is_running(r["pool_id"]):
                # 相邻计划首尾相接时不中断：仅当当前不在任何计划窗口内才停止
                in_other = self.conn.execute(
                    "SELECT COUNT(*) c FROM run_schedules WHERE pool_id=? AND enabled=1 "
                    "AND instr(days,?)>0 AND start_at<=? AND ?<end_at AND id<>?",
                    (r["pool_id"], str(dt.isoweekday()), hm, hm, r["id"])).fetchone()["c"]
                if not in_other:
                    self.svc.stop_pool(r["pool_id"], "计划调度", f"计划结束（{r['end_at']}）")
