"""进程内调度：守护线程定时 tick。

职责：
- 到点先校验水位/水质/设备/天气，满足才启动；
- 到结束时间正常停运；
- 天气突变时停运运行中的喷泉；
- 投药观察期到期后允许计划重试；
- 维修/清池结束后定时复测提醒（写入事件，去重）。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

from . import adapters, config, services
from .db import connect


class Scheduler:
    def __init__(self, db_path=None, tick_seconds: int | None = None):
        self.db_path = db_path
        self.tick_seconds = tick_seconds or config.TICK_SECONDS
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._reminded: set[int] = set()  # (clean/fault) 复测提醒去重

    # ------------------------------------------------------------ 单次 tick
    def tick(self, conn, now: datetime | None = None) -> dict:
        now = now or services.now_dt()
        hhmm = now.strftime("%H:%M")
        date = services.today_str(now)

        # 1) 到结束时间停运 / 天气突变停运
        services.schedule_stop_due(conn, now)
        services.weather_stop_running(conn, now)

        started, blocked = [], []

        # 2) 到点（且处于运行时段内）启动：UNIQUE 保证幂等
        scheds = conn.execute("SELECT * FROM schedules WHERE enabled=1").fetchall()
        for s in scheds:
            today_session = conn.execute(
                "SELECT * FROM run_sessions WHERE schedule_id=? AND run_date=?",
                (s["id"], date)).fetchone()
            in_window = s["start_at"] <= hhmm < s["end_at"]
            # 当天尚无实例且处于运行时段：到点校验启动
            if not today_session and in_window:
                res = services.attempt_start(conn, s["id"], operator="scheduler", now=now)
                (started if res.get("ok") else blocked).append(
                    {"schedule_id": s["id"], "pool_id": s["pool_id"],
                     "reason": res.get("reason", "")})
            # blocked / weather_stop：观察期到期、天气恢复或解除锁定后自动重试
            elif today_session and today_session["status"] in ("blocked", "weather_stop") \
                    and in_window:
                lock = services.pool_lock_reason(conn, s["pool_id"], now)
                obs = services.observation_remaining(conn, s["pool_id"], now)
                wreason = adapters.weather_blocks(adapters.get_weather(now))
                if not lock and obs == 0 and not wreason:
                    res = services.attempt_start(conn, s["id"], operator="scheduler-retry", now=now)
                    (started if res.get("ok") else blocked).append(
                        {"schedule_id": s["id"], "pool_id": s["pool_id"],
                         "reason": res.get("reason", "")})

        # 4) 复测提醒：维修/清池结束后每 15 分钟提醒养护员
        self._retest_reminders(conn, now)
        return {"at": services.iso(now), "started": started, "blocked": blocked}

    def _retest_reminders(self, conn, now: datetime) -> None:
        rows = conn.execute("""
            SELECT 'clean' t, id, pool_id, finished_at ts FROM clean_tasks
            WHERE status='awaiting_retest'
            UNION ALL
            SELECT 'fault' t, id, pool_id, repaired_at ts FROM faults
            WHERE status='awaiting_retest'""").fetchall()
        for r in rows:
            if not r["ts"]:
                continue
            finished = datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S")
            mins = (now - finished).total_seconds() / 60
            if mins >= config.RETEST_REMIND_MINUTES:
                key = (r["t"], r["id"], int(mins // config.RETEST_REMIND_MINUTES))
                if key not in self._reminded:
                    self._reminded.add(key)
                    label = "清池" if r["t"] == "clean" else "维修"
                    services.log_event(
                        conn, r["pool_id"], "retest_remind",
                        f"{label}结束已 {int(mins)} 分钟，请尽快完成水质复测与喷头试运行",
                        "info")
                    conn.commit()

    # ------------------------------------------------------------ 线程生命周期
    def run_forever(self) -> None:
        conn = connect(self.db_path)
        try:
            while not self._stop.wait(self.tick_seconds):
                try:
                    self.tick(conn)
                except Exception as exc:  # 调度异常不杀线程
                    print(f"[scheduler] tick error: {exc}")
        finally:
            conn.close()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self.run_forever, name="fountain-scheduler",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
