"""平台领域规则测试：

- 计划互斥（清池 vs 运行计划）
- 启动前条件校验（水位/水质/泵/喷头/天气/锁定）
- 投药观察期阻断与到期解除
- 启动幂等（含两人并发仅一次生效）
- 复开审批权限（养护员驳回 / 主管退回 / 复测后批准）
另含：药剂超量、误启动、天气停运、游客封控、调度器 tick。
"""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path

from fountain import adapters, config, scheduler, services
from fountain.db import connect, init_db
from fountain.seed import seed


class PlatformTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_file = tempfile.mkstemp(suffix=".db", prefix="fountain-test-")
        os.close(fd)
        self.conn = connect(self.db_file)
        init_db(self.conn)
        self.pool_ids = seed(self.conn)
        adapters.clear_scenarios()
        adapters.set_weather("clear", 24.0, 2)
        self.p_music, self.p_mirror, self.p_kids = self.pool_ids

    def tearDown(self):
        self.conn.close()
        adapters.clear_scenarios()
        adapters.set_weather("clear", 24.0, 2)
        for p in (self.db_file, self.db_file + "-wal", self.db_file + "-shm"):
            Path(p).unlink(missing_ok=True)

    def sched_id(self, pool_id: int) -> int:
        return self.conn.execute(
            "SELECT id FROM schedules WHERE pool_id=? ORDER BY id LIMIT 1",
            (pool_id,)).fetchone()["id"]


class TestConditionGuard(PlatformTestBase):
    """到点启动前必须逐项校验。"""

    def _block_reason(self, pool_id, **scenario):
        if scenario:
            adapters.set_scenario(pool_id, **scenario)
        r = services.attempt_start(self.conn, self.sched_id(pool_id),
                                   operator="t", manual=False)
        return r

    def test_normal_start_passes(self):
        r = self._block_reason(self.p_music)
        self.assertTrue(r["ok"], r.get("reason"))
        self.assertEqual(r["status"], "running")

    def test_low_water_blocks(self):
        r = self._block_reason(self.p_kids, water_level=config.WATER_LEVEL_MIN - 0.2)
        self.assertFalse(r["ok"])
        self.assertIn("水位", r["reason"])

    def test_turbidity_blocks(self):
        r = self._block_reason(self.p_mirror, turbidity=config.TURBIDITY_MAX + 2)
        self.assertFalse(r["ok"])
        self.assertIn("浊度", r["reason"])

    def test_chlorine_out_of_range_blocks(self):
        r = self._block_reason(self.p_kids, chlorine=config.CHLORINE_MAX + 0.5)
        self.assertFalse(r["ok"])
        self.assertIn("余氯", r["reason"])

    def test_pump_overheat_blocks(self):
        r = self._block_reason(self.p_music, pump_temp=config.PUMP_TEMP_MAX + 8)
        self.assertFalse(r["ok"])
        self.assertIn("泵", r["reason"])
        # 过热泵应被置为 fault
        st = self.conn.execute("SELECT status FROM pumps WHERE pool_id=?",
                               (self.p_music,)).fetchall()
        self.assertTrue(any(p["status"] == "fault" for p in st))

    def test_clogged_nozzle_blocks(self):
        r = self._block_reason(self.p_kids, clogged=True)
        self.assertFalse(r["ok"])
        self.assertIn("喷头", r["reason"])

    def test_weather_blocks(self):
        adapters.set_weather("storm", 21, 7)
        r = self._block_reason(self.p_music)
        self.assertFalse(r["ok"])
        self.assertIn("天气停运", r["reason"])

    def test_wind_level_blocks(self):
        adapters.set_weather("clear", 18, config.WIND_STOP_LEVEL)
        r = self._block_reason(self.p_music)
        self.assertFalse(r["ok"])
        self.assertIn("风力", r["reason"])

    def test_manual_false_start_is_logged(self):
        adapters.set_scenario(self.p_kids, turbidity=9.0)
        r = services.attempt_start(self.conn, self.sched_id(self.p_kids),
                                   operator="养护员X", manual=True)
        self.assertFalse(r["ok"])
        ev = self.conn.execute(
            "SELECT * FROM events WHERE kind='false_start' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(ev)
        self.assertIn("养护员X", ev["message"])


class TestScheduleMutex(PlatformTestBase):
    """同一水池清池与运行计划互斥，清池锁定整池。"""

    def test_running_pool_cannot_start_clean(self):
        services.attempt_start(self.conn, self.sched_id(self.p_music), operator="t")
        r = services.start_clean(self.conn, self.p_music, keeper="K")
        self.assertFalse(r["ok"])
        self.assertIn("互斥", r["reason"])

    def test_clean_lock_blocks_schedule(self):
        c = services.start_clean(self.conn, self.p_mirror, keeper="K")
        self.assertTrue(c["ok"])
        r = services.attempt_start(self.conn, self.sched_id(self.p_mirror), operator="t")
        self.assertFalse(r["ok"])
        self.assertIn("锁定", r["reason"])

    def test_duplicate_clean_rejected(self):
        self.assertTrue(services.start_clean(self.conn, self.p_kids, keeper="K")["ok"])
        r = services.start_clean(self.conn, self.p_kids, keeper="K2")
        self.assertFalse(r["ok"])


class TestObservationPeriod(PlatformTestBase):
    """药剂投放后进入观察期；观察期内阻断，到期后可启动。"""

    def test_observation_blocks_start(self):
        now = services.now_dt()
        r = services.add_chemical(self.conn, self.p_music, "含氯消毒剂", 6.0, "K",
                                  minutes=config.OBSERVATION_MINUTES)
        self.assertTrue(r["ok"])
        res = services.attempt_start(self.conn, self.sched_id(self.p_music),
                                     operator="t", now=now)
        self.assertFalse(res["ok"])
        self.assertIn("观察期", res["reason"])

    def test_start_after_observation_window(self):
        now0 = services.now_dt()
        services.add_chemical(self.conn, self.p_music, "含氯消毒剂", 6.0, "K", minutes=30)
        later = now0 + timedelta(minutes=31)
        res = services.attempt_start(self.conn, self.sched_id(self.p_music),
                                     operator="t", now=later)
        self.assertTrue(res["ok"], res.get("reason"))

    def test_overdose_stops_and_requires_retest(self):
        services.attempt_start(self.conn, self.sched_id(self.p_music), operator="t")
        r = services.add_chemical(self.conn, self.p_music, "含氯消毒剂",
                                  config.CHEMICAL_DOSE_MAX + 1, "K")
        self.assertTrue(r["overdose"])
        # 运行被紧急停止
        sess = self.conn.execute(
            "SELECT * FROM run_sessions WHERE pool_id=? ORDER BY id DESC LIMIT 1",
            (self.p_music,)).fetchone()
        self.assertEqual(sess["status"], "blocked")
        self.assertIn("药剂超量", sess["block_reason"])
        # 超量未复测前不允许启动
        now = services.now_dt() + timedelta(minutes=60)
        res = services.attempt_start(self.conn, self.sched_id(self.p_music),
                                     operator="t", now=now)
        self.assertFalse(res["ok"])
        self.assertIn("超量", res["reason"])
        # 复测达标后解除
        rr = services.retest_water(self.conn, self.p_music, "K", now=now)
        self.assertTrue(rr["ok"])
        res2 = services.attempt_start(self.conn, self.sched_id(self.p_music),
                                      operator="t", now=now)
        self.assertTrue(res2["ok"], res2.get("reason"))


class TestIdempotency(PlatformTestBase):
    """同日同计划两次/并发启动仅一次生效。"""

    def test_second_start_is_idempotent(self):
        r1 = services.attempt_start(self.conn, self.sched_id(self.p_music), operator="A")
        r2 = services.attempt_start(self.conn, self.sched_id(self.p_music), operator="B")
        self.assertTrue(r1["ok"])
        self.assertTrue(r2["ok"])
        self.assertTrue(r2.get("idempotent"))
        self.assertEqual(r1["session_id"], r2["session_id"])
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM run_sessions WHERE schedule_id=?",
            (self.sched_id(self.p_music),)).fetchone()["c"]
        self.assertEqual(n, 1)

    def test_concurrent_starts_create_single_session(self):
        sid = self.sched_id(self.p_mirror)
        results, barrier = [], threading.Barrier(8)

        def worker(i):
            conn = connect(self.db_file)
            try:
                barrier.wait()
                results.append(services.attempt_start(conn, sid, operator=f"U{i}"))
            finally:
                conn.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        ids = {r["session_id"] for r in results}
        self.assertEqual(len(ids), 1, f"产生了多个运行实例: {ids}")
        self.assertTrue(all(r["ok"] for r in results))
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM run_sessions WHERE schedule_id=? AND run_date=?",
            (sid, services.today_str(services.now_dt()))).fetchone()["c"]
        self.assertEqual(n, 1)

    def test_blocked_then_retry_reuses_session(self):
        adapters.set_scenario(self.p_kids, water_level=0.5)
        r1 = services.attempt_start(self.conn, self.sched_id(self.p_kids), operator="t")
        self.assertFalse(r1["ok"])
        adapters.clear_scenarios()
        r2 = services.attempt_start(self.conn, self.sched_id(self.p_kids), operator="t")
        self.assertTrue(r2["ok"])
        self.assertEqual(r1["session_id"], r2["session_id"])
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM run_sessions WHERE schedule_id=?",
            (self.sched_id(self.p_kids),)).fetchone()["c"]
        self.assertEqual(n, 1)


class TestReopenApproval(PlatformTestBase):
    """清池/维修后必须复测 + 试运行；越限复开仅主管批准。"""

    def _clean_to_awaiting(self, pool_id):
        c = services.start_clean(self.conn, pool_id, keeper="养护员K")
        services.finish_clean(self.conn, c["task_id"], keeper="养护员K")
        req = services.request_reopen(self.conn, "clean", c["task_id"], "养护员K")
        return c["task_id"], req["request_id"]

    def test_keeper_cannot_approve(self):
        _, rid = self._clean_to_awaiting(self.p_mirror)
        r = services.approve_reopen(self.conn, rid, "养护员K", config.ROLE_KEEPER)
        self.assertFalse(r["ok"])
        self.assertIn("权限", r["reason"])

    def test_supervisor_returns_when_checks_missing(self):
        _, rid = self._clean_to_awaiting(self.p_mirror)
        r = services.approve_reopen(self.conn, rid, "主管S", config.ROLE_SUPERVISOR)
        self.assertFalse(r["ok"])
        self.assertTrue(r["returned"])
        self.assertIn("复测", r["reason"])
        row = self.conn.execute("SELECT status FROM reopen_requests WHERE id=?",
                                (rid,)).fetchone()
        self.assertEqual(row["status"], "rejected")

    def test_full_flow_approved_by_supervisor(self):
        tid, rid = self._clean_to_awaiting(self.p_mirror)
        self.assertTrue(services.retest_water(self.conn, self.p_mirror, "养护员K")["ok"])
        self.assertTrue(services.nozzle_trial(self.conn, self.p_mirror, "养护员K")["ok"])
        # 旧申请已被退回，重新发起
        req2 = services.request_reopen(self.conn, "clean", tid, "养护员K")
        r = services.approve_reopen(self.conn, req2["request_id"], "主管S",
                                    config.ROLE_SUPERVISOR)
        self.assertTrue(r["ok"])
        task = self.conn.execute("SELECT status FROM clean_tasks WHERE id=?",
                                 (tid,)).fetchone()
        self.assertEqual(task["status"], "done")
        self.assertEqual(services.pool_lock_reason(self.conn, self.p_mirror), "")

    def test_fault_reopen_requires_same_checks(self):
        f = services.report_fault(self.conn, self.p_kids, "nozzle_clog", "喷头堵塞")
        self.assertTrue(f["ok"])
        services.repair_fault(self.conn, f["fault_id"], "养护员K", "疏通完成")
        req = services.request_reopen(self.conn, "fault", f["fault_id"], "养护员K")
        # 缺试运行
        services.retest_water(self.conn, self.p_kids, "养护员K")
        r = services.approve_reopen(self.conn, req["request_id"], "主管S",
                                    config.ROLE_SUPERVISOR)
        self.assertTrue(r["returned"])
        self.assertIn("试运行", r["reason"])
        services.nozzle_trial(self.conn, self.p_kids, "养护员K")
        req2 = services.request_reopen(self.conn, "fault", f["fault_id"], "养护员K")
        self.assertTrue(services.approve_reopen(
            self.conn, req2["request_id"], "主管S", config.ROLE_SUPERVISOR)["ok"])


class TestIncidentsAndScheduler(PlatformTestBase):
    """天气停运、游客封控、调度 tick 行为。"""

    def test_visitor_intrusion_stops_fountain(self):
        services.attempt_start(self.conn, self.sched_id(self.p_kids), operator="t")
        r = services.report_visitor(self.conn, self.p_kids)
        self.assertTrue(r["ok"])
        sess = self.conn.execute(
            "SELECT * FROM run_sessions WHERE pool_id=? ORDER BY id DESC LIMIT 1",
            (self.p_kids,)).fetchone()
        self.assertEqual(sess["status"], "blocked")
        self.assertIn("封控", sess["block_reason"])
        # 未处置前不允许启动；处置后可恢复
        r2 = services.attempt_start(self.conn, self.sched_id(self.p_kids), operator="t")
        self.assertFalse(r2["ok"])
        self.assertIn("封控", r2["reason"])
        ev = self.conn.execute(
            "SELECT id FROM events WHERE kind='visitor_intrusion' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        services.resolve_event(self.conn, ev["id"], "养护员K", "游客已劝离")
        r3 = services.attempt_start(self.conn, self.sched_id(self.p_kids), operator="t")
        self.assertTrue(r3["ok"])

    def test_scheduler_tick_starts_then_weather_stops(self):
        sched = scheduler.Scheduler(self.db_file, tick_seconds=60)
        out = sched.tick(self.conn)
        self.assertEqual(len(out["started"]), 3)
        adapters.set_weather("heavy_rain", 20, 5)
        sched.tick(self.conn)
        rows = self.conn.execute(
            "SELECT DISTINCT status FROM run_sessions").fetchall()
        statuses = {r["status"] for r in rows}
        self.assertIn("weather_stop", statuses)
        reason = self.conn.execute(
            "SELECT stop_reason FROM run_sessions WHERE status='weather_stop' LIMIT 1"
        ).fetchone()["stop_reason"]
        self.assertIn("天气停运", reason)

    def test_scheduler_retries_after_weather_recovers(self):
        sched = scheduler.Scheduler(self.db_file, tick_seconds=60)
        sched.tick(self.conn)
        adapters.set_weather("storm", 20, 8)
        sched.tick(self.conn)
        adapters.set_weather("clear", 24, 2)
        out = sched.tick(self.conn)
        # 恢复后自动重试成功
        self.assertTrue(any(s["pool_id"] in self.pool_ids for s in out["started"]))
        running = {r["pool_id"] for r in self.conn.execute(
            "SELECT pool_id FROM run_sessions WHERE status='running'")}
        self.assertEqual(set(running), set(self.pool_ids))


if __name__ == "__main__":
    unittest.main(verbosity=2)
