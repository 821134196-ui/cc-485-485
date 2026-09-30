"""平台领域逻辑测试：互斥、条件校验、观察期、幂等启动、审批权限等。"""
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app import config
from app.adapters import adapter
from app.database import connect, init_db
from app.scheduler import Scheduler
from app.services import (
    PermissionError_,
    PlatformService,
    ServiceError,
)


class Clock:
    def __init__(self, dt: datetime):
        self.dt = dt

    def __call__(self) -> datetime:
        return self.dt

    def advance(self, **kw):
        self.dt += timedelta(**kw)


class PlatformTestCase(unittest.TestCase):
    def setUp(self):
        adapter.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.conn = connect(self.db)
        init_db(self.conn)
        # 固定在周三 12:00：不在任何计划时段内
        self.clock = Clock(datetime(2026, 9, 30, 12, 0, 0))
        self.svc = PlatformService(self.conn, clock=self.clock)
        self.sched = Scheduler(self.svc, interval=999)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    # ---------- 1. 计划互斥 ----------
    def test_schedule_overlap_rejected(self):
        # 与 09:00-11:30 重叠
        with self.assertRaises(ServiceError):
            self.svc.create_schedule(1, "10:00", "10:30")
        # 首尾相接允许
        sid = self.svc.create_schedule(1, "11:30", "12:30")
        self.assertGreater(sid, 0)
        # 再与新区间重叠
        with self.assertRaises(ServiceError):
            self.svc.create_schedule(1, "12:00", "13:00")

    def test_cleaning_and_plan_mutually_exclusive(self):
        ok = self.svc.start_pool(1, "计划调度", schedule_id=1)
        self.assertTrue(ok["ok"])
        cid = self.svc.create_cleaning(1, "2026-09-30", "李工")
        self.svc.lock_cleaning(cid, "李工")          # 锁定立即停运
        self.assertFalse(self.svc.is_running(1))
        # 同一水池清池期间到点启动被拦截
        res = self.svc.start_pool(1, "计划调度", schedule_id=1)
        self.assertFalse(res["ok"])
        self.assertIn("cleaning", res["precheck"]["reasons"])

    # ---------- 2. 启动条件校验 ----------
    def test_precheck_blocks_level_low(self):
        adapter.set_scenario(1, "level_low")
        res = self.svc.start_pool(1, "计划调度", schedule_id=1)
        self.assertFalse(res["ok"])
        self.assertIn("level_low", res["precheck"]["reasons"])

    def test_precheck_blocks_quality_over(self):
        adapter.set_scenario(1, "quality_over")
        res = self.svc.start_pool(1, "计划调度", schedule_id=1)
        self.assertFalse(res["ok"])
        self.assertIn("quality", res["precheck"]["reasons"])

    def test_precheck_blocks_weather_and_intrusion(self):
        adapter.set_weather(True)
        res = self.svc.start_pool(2, "计划调度", schedule_id=3)
        self.assertFalse(res["ok"])
        self.assertIn("weather", res["precheck"]["reasons"])
        adapter.set_weather(False)

        self.svc.report_intrusion(2)
        res = self.svc.start_pool(2, "计划调度", schedule_id=3)
        self.assertIn("intrusion", res["precheck"]["reasons"])
        self.svc.clear_intrusion(2, "巡检员")
        res = self.svc.start_pool(2, "计划调度", schedule_id=3)
        self.assertTrue(res["ok"])

    def test_precheck_blocks_nozzle_and_overheat(self):
        adapter.force_nozzle_block(1, True)
        res = self.svc.start_pool(1, "计划调度", schedule_id=1)
        self.assertIn("nozzle_block", res["precheck"]["reasons"])
        adapter.force_nozzle_block(1, False)

        # 泵体过热：先正常启动，再注入过热，调度保护停运并建工单
        ok = self.svc.start_pool(1, "计划调度", schedule_id=1)
        self.assertTrue(ok["ok"])
        adapter.force_pump_overheat(1)
        self.sched.tick()
        self.assertFalse(self.svc.is_running(1))
        fault = self.conn.execute(
            "SELECT * FROM faults WHERE kind='overheat' AND status='open'").fetchone()
        self.assertIsNotNone(fault)

    def test_manual_start_outside_window_is_misstart(self):
        res = self.svc.start_pool(2, "游客旁工作人员", manual=True)  # 周三 12:00
        self.assertFalse(res["ok"])
        self.assertIn("misstart", res["precheck"]["reasons"])
        fault = self.conn.execute(
            "SELECT * FROM faults WHERE kind='misstart' AND status='open'").fetchone()
        self.assertIsNotNone(fault)

    def test_scheduler_auto_samples_and_raises_faults(self):
        adapter.set_scenario(1, "level_low")
        self.sched.tick()
        fault = self.conn.execute(
            "SELECT * FROM faults WHERE kind='level_low' AND status='open'").fetchone()
        self.assertIsNotNone(fault)
        reading = self.conn.execute(
            "SELECT * FROM water_readings WHERE pool_id=1 AND kind='auto' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        self.assertLess(reading["level"], config.WATER_LEVEL_MIN)

    # ---------- 3. 观察期阻断 ----------
    def test_dosing_observe_blocks_start(self):
        self.assertTrue(self.svc.start_pool(1, "计划调度", schedule_id=1)["ok"])
        res = self.svc.add_dosing(1, "chlorine", 300, "李工")
        self.assertTrue(res["ok"])
        self.assertFalse(self.svc.is_running(1))        # 投药即停运
        blocked = self.svc.start_pool(1, "计划调度", schedule_id=1)
        self.assertFalse(blocked["ok"])
        self.assertIn("observing", blocked["precheck"]["reasons"])

        self.clock.advance(minutes=config.DOSING_OBSERVE_MINUTES + 1)
        ok = self.svc.start_pool(1, "计划调度", schedule_id=1)
        self.assertTrue(ok["ok"])

    def test_overdose_extends_observe_and_opens_fault(self):
        res = self.svc.add_dosing(1, "chlorine", 800, "李工")   # 上限 500
        self.assertFalse(res["ok"])
        fault = self.conn.execute(
            "SELECT * FROM faults WHERE kind='overdose' AND status='open'").fetchone()
        self.assertIsNotNone(fault)
        # 超量观察期翻倍：31 分钟仍阻断
        self.clock.advance(minutes=config.DOSING_OBSERVE_MINUTES + 1)
        self.assertFalse(self.svc.start_pool(1, "计划调度", schedule_id=1)["ok"])
        self.clock.advance(minutes=config.DOSING_OBSERVE_MINUTES)
        # 故障未闭环仍不可启动
        pre = self.svc.start_pool(1, "计划调度", schedule_id=1)["precheck"]
        self.assertIn("fault_open", pre["reasons"])

    # ---------- 4. 启动幂等（两人并发仅一次生效） ----------
    def test_concurrent_start_only_once(self):
        barrier = threading.Barrier(2)
        results = []

        def worker(actor):
            barrier.wait()
            results.append(self.svc.start_pool(2, actor, schedule_id=3))

        t1 = threading.Thread(target=worker, args=("甲",))
        t2 = threading.Thread(target=worker, args=("乙",))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(sum(1 for r in results if r["ok"]), 2)
        self.assertEqual(sum(1 for r in results if r.get("idempotent")), 1)
        starts = self.conn.execute(
            "SELECT COUNT(*) c FROM run_logs WHERE pool_id=2 AND action='start' "
            "AND result='ok'").fetchone()["c"]
        self.assertEqual(starts, 1)

    def test_double_start_and_stop_idempotent(self):
        first = self.svc.start_pool(3, "计划调度", schedule_id=5)
        second = self.svc.start_pool(3, "计划调度", schedule_id=5)
        self.assertFalse(first.get("idempotent"))
        self.assertTrue(second["idempotent"])
        self.assertTrue(self.svc.stop_pool(3)["ok"])
        self.assertTrue(self.svc.stop_pool(3)["idempotent"])

    # ---------- 5. 复开审批权限 ----------
    def _cleaning_reopen(self, turbidity=2.6):
        cid = self.svc.create_cleaning(3, "2026-09-30", "李工")
        self.svc.lock_cleaning(cid, "李工")
        self.svc.start_cleaning(cid, "李工")
        rid = self.svc.finish_cleaning(cid, "李工")
        self.svc.add_reading(3, {"level": 0.75, "turbidity": turbidity, "chlorine": 0.55},
                             kind="retest", actor="李工")
        return cid, rid

    def test_reopen_requires_retest_and_trial(self):
        _, rid = self._cleaning_reopen()
        with self.assertRaises(ServiceError):  # 缺复测与试运行
            self.svc.decide_reopen(rid, "approve", "李工", config.ROLE_KEEPER)
        self.svc.nozzle_trial(3, "李工")
        # 复测合格 + 试运行通过：养护员可复开
        res = self.svc.decide_reopen(rid, "approve", "李工", config.ROLE_KEEPER)
        self.assertEqual(res["status"], "approved")
        self.assertTrue(self.svc.start_pool(3, "计划调度", schedule_id=5)["ok"])

    def test_over_limit_reopen_needs_supervisor(self):
        _, rid = self._cleaning_reopen(turbidity=8.2)   # 浊度越限
        self.svc.nozzle_trial(3, "李工")
        with self.assertRaises(PermissionError_):
            self.svc.decide_reopen(rid, "approve", "李工", config.ROLE_KEEPER)
        # 申请仍挂起
        pending = self.conn.execute(
            "SELECT status FROM reopen_requests WHERE id=?", (rid,)).fetchone()["status"]
        self.assertEqual(pending, "pending")
        res = self.svc.decide_reopen(rid, "approve", "王敏", config.ROLE_SUPERVISOR)
        self.assertEqual(res["status"], "approved")
        self.assertFalse(res["within_limits"])

    def test_reject_then_rerequest_flow(self):
        _, rid = self._cleaning_reopen(turbidity=7.0)
        self.svc.nozzle_trial(3, "李工")
        self.svc.decide_reopen(rid, "reject", "王敏", config.ROLE_SUPERVISOR,
                               "复测超限，重新处理")
        # 退回后水池仍锁定
        self.assertFalse(self.svc.start_pool(3, "计划调度", schedule_id=5)["ok"])
        new_rid = self.svc.request_reopen(3, "李工")
        self.assertNotEqual(new_rid, rid)
        # 新申请同样缺复测 → 拒绝批准
        with self.assertRaises(ServiceError):
            self.svc.decide_reopen(new_rid, "approve", "李工", config.ROLE_KEEPER)

    def test_repair_reopen_flow(self):
        # 注入过热 → 调度建故障 → 维修 → 复开检查 → 批准解锁
        self.assertTrue(self.svc.start_pool(1, "计划调度", schedule_id=1)["ok"])
        adapter.force_pump_overheat(1)
        self.sched.tick()
        fid = self.conn.execute(
            "SELECT id FROM faults WHERE kind='overheat' AND status='open'").fetchone()["id"]
        self.svc.begin_repair(fid, "李工")
        rid = self.svc.finish_repair(fid, "李工")
        # 未完成复测/试运行前，泵不可用
        res = self.svc.start_pool(1, "计划调度", schedule_id=1)
        self.assertFalse(res["ok"])
        self.svc.add_reading(1, {"level": 0.75, "turbidity": 2.2, "chlorine": 0.5},
                             kind="retest", actor="李工")
        self.svc.nozzle_trial(1, "李工")
        self.svc.decide_reopen(rid, "approve", "李工", config.ROLE_KEEPER)
        self.assertTrue(self.svc.start_pool(1, "计划调度", schedule_id=1)["ok"])


if __name__ == "__main__":
    unittest.main()
