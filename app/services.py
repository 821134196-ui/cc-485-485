"""领域服务：启停校验、计划互斥、观察期、清池/维修、复开审批与故障处置。"""
import threading
from datetime import datetime, timedelta
from typing import Callable, Optional

from . import config
from .adapters import adapter, chemical_cn

FAULT_TEXT = {
    "level_low": "水位不足",
    "quality": "水质超限",
    "nozzle_block": "喷头堵塞",
    "overheat": "泵体过热",
    "weather": "天气停运",
    "misstart": "误启动",
    "overdose": "药剂超量",
    "intrusion": "游客进入封控区",
    "maintenance": "维修停运",
}

REASON_TEXT = {
    "cleaning": "水池清池锁定中，计划暂停",
    "fault_open": "存在未闭环故障",
    "weather": "天气预警（大风/雷暴），禁止启动",
    "intrusion": "封控区内有游客，禁止启动",
    "observing": "药剂观察期未结束",
    "pump": "循环泵不可用（故障/维修）",
    "pump_temp": "泵体温度超限",
    "level_low": "水位不足",
    "level_high": "水位超高",
    "quality": "浊度或余氯超限",
    "nozzle_block": "喷头组压力不足，疑似堵塞",
    "misstart": "非计划时段禁止人工启动",
}

# 单次投加上限（ml），超过即药剂超量
DOSE_LIMITS = {"chlorine": 500, "flocculant": 300, "algaecide": 200}

BLOCKING_FAULT_KINDS = (
    "level_low", "quality", "nozzle_block", "overheat",
    "misstart", "overdose", "intrusion", "maintenance",
)


def now() -> datetime:
    return datetime.now()


def ts(dt: datetime = None) -> str:
    return (dt or now()).strftime("%Y-%m-%d %H:%M:%S")


class ServiceError(Exception):
    """400 类业务错误。"""


class PermissionError_(Exception):
    """403 权限错误。"""


class PlatformService:
    def __init__(self, conn, clock: Callable[[], datetime] = now):
        self.conn = conn
        self.clock = clock
        self._global = threading.RLock()
        self._pool_locks: dict[int, threading.Lock] = {}

    def _pool_lock(self, pool_id: int) -> threading.Lock:
        with self._global:
            if pool_id not in self._pool_locks:
                self._pool_locks[pool_id] = threading.Lock()
            return self._pool_locks[pool_id]

    # ================= 基础查询 =================
    def _pool(self, pool_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM pools WHERE id=?", (pool_id,)).fetchone()
        if not row:
            raise ServiceError("水池不存在")
        return dict(row)

    def is_running(self, pool_id: int) -> bool:
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM pumps WHERE pool_id=? AND status='running'",
            (pool_id,)).fetchone()
        return row["c"] > 0

    def _active_cleaning(self, pool_id: int):
        # locked/in_progress 锁定；done 但复开申请未批准仍锁定
        row = self.conn.execute(
            "SELECT * FROM cleaning_tasks WHERE pool_id=? "
            "AND status IN ('locked','in_progress') ORDER BY id DESC LIMIT 1",
            (pool_id,)).fetchone()
        if row:
            return dict(row)
        row = self.conn.execute(
            "SELECT c.* FROM cleaning_tasks c JOIN reopen_requests r ON r.cleaning_id=c.id "
            "WHERE c.pool_id=? AND c.status='done' AND r.status IN ('pending','rejected') "
            "ORDER BY r.id DESC LIMIT 1", (pool_id,)).fetchone()
        return dict(row) if row else None

    def _open_faults(self, pool_id: int):
        rows = self.conn.execute(
            "SELECT * FROM faults WHERE pool_id=? AND status IN ('open','repairing')",
            (pool_id,)).fetchall()
        return [dict(r) for r in rows]

    def _observing(self, pool_id: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM dosing_records WHERE pool_id=? ORDER BY id DESC LIMIT 1",
            (pool_id,)).fetchone()
        if not row:
            return None
        until = datetime.strptime(row["observe_until"], "%Y-%m-%d %H:%M:%S")
        if until > self.clock():
            d = dict(row)
            d["chemical_cn"] = chemical_cn(d["chemical"])
            return d
        return None

    # ================= 启动前条件校验 =================
    def precheck(self, pool_id: int, in_schedule_window: bool = True) -> dict:
        pool = self._pool(pool_id)
        reasons = []
        water = adapter.read_water(pool_id)
        weather = adapter.weather()
        zone_intrusion = adapter.intrusion(pool["zone_id"])

        if self._active_cleaning(pool_id):
            reasons.append("cleaning")
        for f in self._open_faults(pool_id):
            if f["kind"] == "weather":
                continue  # 天气故障由天气现状决定
            reasons.append("fault_open")
            break
        if not weather["allow_run"]:
            reasons.append("weather")
        if zone_intrusion:
            reasons.append("intrusion")
        dose = self._observing(pool_id)
        if dose:
            reasons.append("observing")

        pumps = [dict(r) for r in self.conn.execute(
            "SELECT * FROM pumps WHERE pool_id=?", (pool_id,)).fetchall()]
        bad_pump = False
        hot_pump = False
        for p in pumps:
            tele = adapter.read_pump(p["id"], p["status"] == "running")
            p["temp_c"] = tele["temp_c"]
            if p["status"] in ("fault", "repair"):
                bad_pump = True
            if tele["temp_c"] > config.PUMP_TEMP_MAX:
                hot_pump = True
        if bad_pump:
            reasons.append("pump")
        if hot_pump:
            reasons.append("pump_temp")

        if water["level"] < config.WATER_LEVEL_MIN:
            reasons.append("level_low")
        elif water["level"] > config.WATER_LEVEL_MAX:
            reasons.append("level_high")
        if water["turbidity"] > config.TURBIDITY_MAX or not (
                config.CHLORINE_MIN <= water["chlorine"] <= config.CHLORINE_MAX):
            reasons.append("quality")

        groups = [dict(r) for r in self.conn.execute(
            "SELECT * FROM nozzle_groups WHERE pool_id=?", (pool_id,)).fetchall()]
        blocked = False
        for g in groups:
            tele = adapter.read_nozzle_group(g["id"])
            g["pressure_mpa"] = tele["pressure_mpa"]
            g["blocked_now"] = tele["blocked"]
            if tele["blocked"]:
                blocked = True
        if blocked:
            reasons.append("nozzle_block")
        if not in_schedule_window:
            reasons.append("misstart")

        return {
            "ok": not reasons,
            "reasons": reasons,
            "reason_texts": [REASON_TEXT[r] for r in reasons],
            "water": water,
            "weather": weather,
            "intrusion": zone_intrusion,
            "pumps": pumps,
            "nozzle_groups": groups,
            "observing": dose,
        }

    # ================= 启停（幂等） =================
    def start_pool(self, pool_id: int, actor: str, schedule_id: Optional[int] = None,
                   manual: bool = False) -> dict:
        """启动水池。两人并发调用仅一次生效；已在运行直接幂等返回。"""
        lock = self._pool_lock(pool_id)
        with lock:
            if self.is_running(pool_id):
                return {"ok": True, "idempotent": True, "message": "已在运行，启动请求合并"}

            in_window = True
            if manual:
                in_window = self._in_any_window(pool_id)

            pre = self.precheck(pool_id, in_schedule_window=in_window)
            if not pre["ok"]:
                self._log(pool_id, schedule_id, "block",
                          "failed" if manual else "blocked",
                          "；".join(pre["reason_texts"]), actor)
                if manual:
                    # 条件不满足仍人工按下启动 = 误启动，登记故障并继续拦截
                    self._raise_fault(pool_id, "misstart",
                                      f"{actor} 在禁止条件下尝试启动："
                                      f"{'；'.join(pre['reason_texts'])}")
                return {"ok": False, "idempotent": False, "precheck": pre}

            self.conn.execute(
                "UPDATE pumps SET status='running', fault_code=NULL WHERE pool_id=? AND status='idle'",
                (pool_id,))
            self.conn.commit()
            self._log(pool_id, schedule_id, "start", "ok",
                      "计划到点启动" if schedule_id else "人工启动", actor)
            return {"ok": True, "idempotent": False, "precheck": pre}

    def stop_pool(self, pool_id: int, actor: str = "系统", reason: str = "计划结束",
                  action: str = "stop") -> dict:
        lock = self._pool_lock(pool_id)
        with lock:
            if not self.is_running(pool_id):
                return {"ok": True, "idempotent": True, "message": "本已停止"}
            self.conn.execute(
                "UPDATE pumps SET status='idle' WHERE pool_id=? AND status='running'",
                (pool_id,))
            self.conn.commit()
            self._log(pool_id, None, action, "ok", reason, actor)
            return {"ok": True, "idempotent": False}

    def _in_any_window(self, pool_id: int) -> bool:
        dt = self.clock()
        hm = dt.strftime("%H:%M")
        rows = self.conn.execute(
            "SELECT * FROM run_schedules WHERE pool_id=? AND enabled=1", (pool_id,)).fetchall()
        for r in rows:
            if str(dt.isoweekday()) in r["days"] and r["start_at"] <= hm < r["end_at"]:
                return True
        return False

    def _log(self, pool_id, schedule_id, action, result, reason, actor):
        self.conn.execute(
            "INSERT INTO run_logs(pool_id,schedule_id,ts,action,result,reason,actor) "
            "VALUES(?,?,?,?,?,?,?)",
            (pool_id, schedule_id, ts(self.clock()), action, result, reason, actor))
        self.conn.commit()

    def _alert(self, pool_id, category, message):
        self.conn.execute(
            "INSERT INTO alerts(pool_id,ts,category,message) VALUES(?,?,?,?)",
            (pool_id, ts(self.clock()), category, message))
        self.conn.commit()

    def _raise_fault(self, pool_id, kind, detail, pump_id=None) -> Optional[int]:
        """同类故障边沿登记：已有未闭环故障则不重复建单。"""
        exists = self.conn.execute(
            "SELECT id FROM faults WHERE pool_id=? AND kind=? AND status IN ('open','repairing')",
            (pool_id, kind)).fetchone()
        if exists:
            return exists["id"]
        cur = self.conn.execute(
            "INSERT INTO faults(pool_id,pump_id,ts,kind,detail,status) VALUES(?,?,?,?,?,?)",
            (pool_id, pump_id, ts(self.clock()), kind, detail, "open"))
        self.conn.commit()
        fid = cur.lastrowid
        self._alert(pool_id, kind, f"【{FAULT_TEXT[kind]}】{detail}")
        return fid

    # ================= 补水 / 读数 / 复测 =================
    def refill(self, pool_id: int, actor: str):
        with self._global:
            self._pool(pool_id)
            adapter.set_scenario(pool_id, "normal")
            w = adapter.read_water(pool_id)
            self.conn.execute(
                "INSERT INTO water_readings(pool_id,ts,level,turbidity,chlorine,kind,note) "
                "VALUES(?,?,?,?,?, 'manual', ?)",
                (pool_id, ts(self.clock()), w["level"], w["turbidity"], w["chlorine"],
                 f"{actor} 执行补水后记录"))
            self.conn.execute(
                "UPDATE faults SET status='resolved', resolution='已补水，水位恢复' "
                "WHERE pool_id=? AND kind='level_low' AND status IN ('open','repairing')",
                (pool_id,))
            self.conn.commit()
            self._alert(pool_id, "info", f"{actor} 完成补水，水位恢复正常")
            return {"ok": True, "water": w}

    def add_reading(self, pool_id: int, values: dict, kind: str = "manual",
                    actor: str = "养护员", note: str = "") -> dict:
        with self._global:
            cur = self.conn.execute(
                "INSERT INTO water_readings(pool_id,ts,level,turbidity,chlorine,kind,note) "
                "VALUES(?,?,?,?,?,?,?)",
                (pool_id, ts(self.clock()), values["level"], values["turbidity"],
                 values["chlorine"], kind, note or f"{actor} 录入"))
            self.conn.commit()
            rid = cur.lastrowid
            if kind == "retest":
                # 挂到待处理的复开申请与清池任务
                self.conn.execute(
                    "UPDATE reopen_requests SET retest_reading_id=? "
                    "WHERE pool_id=? AND status='pending'", (rid, pool_id))
                self.conn.execute(
                    "UPDATE cleaning_tasks SET retest_reading_id=? "
                    "WHERE pool_id=? AND status='done' AND retest_reading_id IS NULL",
                    (rid, pool_id))
                self.conn.commit()
                over = (values["turbidity"] > config.TURBIDITY_MAX or
                        not (config.CHLORINE_MIN <= values["chlorine"] <= config.CHLORINE_MAX))
                self._alert(pool_id, "retest",
                            f"水质复测已录入（{'超限，需主管批准' if over else '合格'}）")
            return {"id": rid}

    def reading_in_range(self, r: dict) -> bool:
        return (config.WATER_LEVEL_MIN <= r["level"] <= config.WATER_LEVEL_MAX
                and r["turbidity"] <= config.TURBIDITY_MAX
                and config.CHLORINE_MIN <= r["chlorine"] <= config.CHLORINE_MAX)

    # ================= 药剂投放与观察期 =================
    def add_dosing(self, pool_id: int, chemical: str, amount_ml: float,
                   actor: str) -> dict:
        if chemical not in DOSE_LIMITS:
            raise ServiceError("药剂类型无效")
        if amount_ml <= 0:
            raise ServiceError("投加量必须大于 0")
        with self._global:
            pool = self._pool(pool_id)
            over = amount_ml > DOSE_LIMITS[chemical]
            minutes = config.DOSING_OBSERVE_MINUTES * (2 if over else 1)
            observe_until = self.clock() + timedelta(minutes=minutes)
            cur = self.conn.execute(
                "INSERT INTO dosing_records(pool_id,ts,chemical,amount_ml,operator,observe_until,note)"
                " VALUES(?,?,?,?,?,?,?)",
                (pool_id, ts(self.clock()), chemical, amount_ml, actor,
                 observe_until.strftime("%Y-%m-%d %H:%M:%S"),
                 "超量投加" if over else "正常投加"))
            self.conn.commit()
            # 投药后进入观察期：若正在运行则停运
            if self.is_running(pool_id):
                self.stop_pool(pool_id, actor, f"投加{chemical_cn(chemical)}，进入观察期")
            if over:
                fid = self._raise_fault(
                    pool_id, "overdose",
                    f"{actor} 投加{chemical_cn(chemical)} {amount_ml:g}ml，"
                    f"超过单次上限 {DOSE_LIMITS[chemical]}ml，观察期延长至 {minutes} 分钟")
                return {"ok": False, "fault_id": fid, "observe_until": observe_until,
                        "message": "药剂超量，已停运并登记故障"}
            self._alert(pool_id, "dosing",
                        f"{chemical_cn(chemical)} {amount_ml:g}ml 投加完成，观察期至 "
                        f"{observe_until.strftime('%H:%M')}")
            return {"ok": True, "observe_until": observe_until}

    # ================= 清池流程（锁定整个水池） =================
    def create_cleaning(self, pool_id: int, planned_date: str, assignee: str) -> int:
        with self._global:
            if self._active_cleaning(pool_id):
                raise ServiceError("该水池已有未完成的清池任务")
            cur = self.conn.execute(
                "INSERT INTO cleaning_tasks(pool_id,planned_date,status,assignee) VALUES(?,?,?,?)",
                (pool_id, planned_date, "planned", assignee))
            self.conn.commit()
            return cur.lastrowid

    def lock_cleaning(self, task_id: int, actor: str):
        with self._global:
            t = self._task(task_id)
            if t["status"] != "planned":
                raise ServiceError("仅计划态任务可锁定水池")
            # 同一水池不能同时清池与运行：立即停运并锁定
            if self.is_running(t["pool_id"]):
                self.stop_pool(t["pool_id"], actor, "清池锁定，计划暂停")
            self.conn.execute(
                "UPDATE cleaning_tasks SET status='locked', assignee=? WHERE id=?",
                (actor, task_id))
            self.conn.commit()
            self._alert(t["pool_id"], "cleaning", f"清池任务 #{task_id} 已锁定水池，运行计划暂停")

    def start_cleaning(self, task_id: int, actor: str):
        with self._global:
            t = self._task(task_id)
            if t["status"] != "locked":
                raise ServiceError("需先锁定水池才能开始清池")
            self.conn.execute(
                "UPDATE cleaning_tasks SET status='in_progress', started_at=? WHERE id=?",
                (ts(self.clock()), task_id))
            self.conn.commit()

    def finish_cleaning(self, task_id: int, actor: str) -> int:
        """清池结束：创建复开申请，必须复测+试运行后由有权角色复开。"""
        with self._global:
            t = self._task(task_id)
            if t["status"] != "in_progress":
                raise ServiceError("仅施工中的任务可完工")
            self.conn.execute(
                "UPDATE cleaning_tasks SET status='done', finished_at=? WHERE id=?",
                (ts(self.clock()), task_id))
            cur = self.conn.execute(
                "INSERT INTO reopen_requests(pool_id,cleaning_id,ts,reason,status) "
                "VALUES(?,?,?,?,'pending')",
                (t["pool_id"], task_id, ts(self.clock()),
                 f"清池任务 #{task_id} 完工，申请复开（待水质复测与喷头试运行）"))
            self.conn.commit()
            rid = cur.lastrowid
            self._alert(t["pool_id"], "reopen", f"清池完工，请完成水质复测与喷头试运行后申请复开")
            return rid

    def _task(self, task_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM cleaning_tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise ServiceError("清池任务不存在")
        return dict(row)

    # ================= 喷头试运行 =================
    def nozzle_trial(self, pool_id: int, actor: str = "养护员") -> dict:
        with self._global:
            groups = [dict(r) for r in self.conn.execute(
                "SELECT * FROM nozzle_groups WHERE pool_id=?", (pool_id,)).fetchall()]
            results = []
            any_blocked = False
            for g in groups:
                tele = adapter.read_nozzle_group(g["id"])
                ok = not tele["blocked"]
                any_blocked = any_blocked or not ok
                self.conn.execute("UPDATE nozzle_groups SET blocked=? WHERE id=?",
                                  (0 if ok else 1, g["id"]))
                results.append({"group": g["code"], "name": g["name"],
                                "pressure_mpa": tele["pressure_mpa"], "ok": ok})
            self.conn.commit()
            ok_flag = 0 if any_blocked else 1
            self.conn.execute(
                "UPDATE reopen_requests SET nozzle_trial_ok=? "
                "WHERE pool_id=? AND status='pending'", (ok_flag, pool_id))
            self.conn.execute(
                "UPDATE cleaning_tasks SET nozzle_trial_ok=? "
                "WHERE pool_id=? AND status='done'", (ok_flag, pool_id))
            self.conn.commit()
            self._log(pool_id, None, "nozzle_trial", "ok" if not any_blocked else "failed",
                      "；".join(f"{r['group']} {'正常' if r['ok'] else '堵塞'}" for r in results),
                      actor)
            if any_blocked:
                self._raise_fault(pool_id, "nozzle_block",
                                  "喷头试运行发现压力不足，存在堵塞组")
            return {"ok": not any_blocked, "groups": results}

    # ================= 故障维修 =================
    def begin_repair(self, fault_id: int, actor: str) -> dict:
        with self._global:
            f = self._fault(fault_id)
            if f["status"] not in ("open",):
                raise ServiceError("仅待处理故障可开始维修")
            self.conn.execute("UPDATE faults SET status='repairing' WHERE id=?", (fault_id,))
            if f["pump_id"]:
                self.conn.execute("UPDATE pumps SET status='repair' WHERE id=?", (f["pump_id"],))
            if self.is_running(f["pool_id"]):
                self.stop_pool(f["pool_id"], actor, f"故障维修：{FAULT_TEXT[f['kind']]}")
            self.conn.commit()
            return {"ok": True}

    def finish_repair(self, fault_id: int, actor: str) -> int:
        """维修结束：进入复开检查（复测+试运行），不直接恢复运行。"""
        with self._global:
            f = self._fault(fault_id)
            if f["status"] != "repairing":
                raise ServiceError("仅维修中的故障可提交完工")
            if f["pump_id"]:
                adapter.clear_faults(f["pump_id"])
                self.conn.execute(
                    "UPDATE pumps SET status='fault', fault_code='await_reopen' WHERE id=?",
                    (f["pump_id"],))
            cur = self.conn.execute(
                "INSERT INTO reopen_requests(pool_id,fault_id,ts,reason,status) "
                "VALUES(?,?,?,?,'pending')",
                (f["pool_id"], fault_id, ts(self.clock()),
                 f"故障 #{fault_id}（{FAULT_TEXT[f['kind']]}）维修完工，申请复开检查"))
            rid = cur.lastrowid
            self.conn.execute("UPDATE faults SET reopen_request_id=? WHERE id=?", (rid, fault_id))
            self.conn.commit()
            self._alert(f["pool_id"], "reopen", "维修完工，待水质复测与喷头试运行")
            return rid

    def resolve_fault(self, fault_id: int, actor: str, resolution: str):
        """养护员现场处置闭环（误启动核查、游客劝离等无需复开检查的场景）。"""
        with self._global:
            f = self._fault(fault_id)
            if f["kind"] in ("weather",):
                raise ServiceError("天气故障由系统在预警解除后自动闭环")
            self.conn.execute(
                "UPDATE faults SET status='resolved', resolution=? WHERE id=?",
                (f"{actor}：{resolution}", fault_id))
            if f["pump_id"]:
                self.conn.execute(
                    "UPDATE pumps SET status='idle', fault_code=NULL WHERE id=? AND status!='running'",
                    (f["pump_id"],))
            self.conn.commit()

    def _fault(self, fault_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM faults WHERE id=?", (fault_id,)).fetchone()
        if not row:
            raise ServiceError("故障单不存在")
        return dict(row)

    def request_reopen(self, pool_id: int, actor: str) -> int:
        """复开退回后，重新补齐复测与试运行，可再次发起申请。"""
        with self._global:
            prev = self.conn.execute(
                "SELECT * FROM reopen_requests WHERE pool_id=? ORDER BY id DESC LIMIT 1",
                (pool_id,)).fetchone()
            if not prev:
                raise ServiceError("没有可重新发起的复开申请")
            prev = dict(prev)
            if prev["status"] != "rejected":
                raise ServiceError("仅被退回的申请可重新发起")
            cur = self.conn.execute(
                "INSERT INTO reopen_requests(pool_id,fault_id,cleaning_id,ts,reason,status) "
                "VALUES(?,?,?,?,?,'pending')",
                (pool_id, prev["fault_id"], prev["cleaning_id"], ts(self.clock()),
                 f"{actor} 重新补齐材料后再次申请复开"))
            self.conn.commit()
            return cur.lastrowid

    # ================= 复开审批 =================
    def _pending_reopen(self, req_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM reopen_requests WHERE id=?", (req_id,)).fetchone()
        if not row:
            raise ServiceError("复开申请不存在")
        d = dict(row)
        if d["status"] != "pending":
            raise ServiceError("该申请已处理")
        return d

    def decide_reopen(self, req_id: int, decision: str, actor: str, role: str,
                      reject_reason: str = "") -> dict:
        """decision=approve/reject。越限复开仅主管可批准。"""
        if role not in (config.ROLE_KEEPER, config.ROLE_SUPERVISOR):
            raise PermissionError_("未知角色")
        with self._global:
            req = self._pending_reopen(req_id)
            if decision == "reject":
                # 复开退回：主管或养护员均可退回（主管退回为终审演示样例）
                self.conn.execute(
                    "UPDATE reopen_requests SET status='rejected', decided_by=?, decided_role=?,"
                    "decided_at=?, reject_reason=? WHERE id=?",
                    (actor, role, ts(self.clock()),
                     reject_reason or "复测/试运行不满足复开条件", req_id))
                if req["fault_id"]:
                    self.conn.execute(
                        "UPDATE faults SET status='open' WHERE id=? AND status='repairing'",
                        (req["fault_id"],))
                self.conn.commit()
                self._alert(req["pool_id"], "reopen",
                            f"复开申请 #{req_id} 被退回：{reject_reason or '条件不满足'}，请重新处理")
                return {"ok": True, "status": "rejected"}

            if decision != "approve":
                raise ServiceError("decision 必须为 approve/reject")

            # 批准前：复测与喷头试运行缺一不可
            if not req["retest_reading_id"]:
                raise ServiceError("缺少水质复测，不能复开")
            if req["nozzle_trial_ok"] != 1:
                raise ServiceError("喷头试运行未通过，不能复开")
            r = self.conn.execute("SELECT * FROM water_readings WHERE id=?",
                                  (req["retest_reading_id"],)).fetchone()
            within = self.reading_in_range(dict(r))
            if not within and role != config.ROLE_SUPERVISOR:
                raise PermissionError_("复测指标仍超限，越限复开须主管批准")

            self.conn.execute(
                "UPDATE reopen_requests SET status='approved', decided_by=?, decided_role=?,"
                "decided_at=? WHERE id=?",
                (actor, role, ts(self.clock()), req_id))
            if req["fault_id"]:
                self.conn.execute(
                    "UPDATE faults SET status='resolved', resolution=? WHERE id=?",
                    (f"复开申请 #{req_id} 已批准"
             f"（{'越限复开，主管批准' if not within else '指标合格'}）", req["fault_id"]))
            if req["cleaning_id"]:
                self.conn.execute(
                    "UPDATE cleaning_tasks SET note='已复开' WHERE id=?",
                    (req["cleaning_id"],))
            # 解锁设备：维修泵回到 idle
            self.conn.execute(
                "UPDATE pumps SET status='idle', fault_code=NULL "
                "WHERE pool_id=? AND status IN ('fault','repair')", (req["pool_id"],))
            self.conn.commit()
            self._alert(req["pool_id"], "reopen",
                        f"复开申请 #{req_id} 已批准"
                        f"（{'越限复开' if not within else '指标合格'}），水池可按计划启动")
            return {"ok": True, "status": "approved", "within_limits": within}

    # ================= 封控区闯入 =================
    def report_intrusion(self, zone_id: int, actor: str = "巡检员"):
        with self._global:
            adapter.set_intrusion(zone_id, True)
            pools = [dict(r) for r in self.conn.execute(
                "SELECT id,name FROM pools WHERE zone_id=?", (zone_id,)).fetchall()]
            for p in pools:
                if self.is_running(p["id"]):
                    self.stop_pool(p["id"], actor, "游客进入封控区，紧急停运", action="abort")
                self._raise_fault(p["id"], "intrusion",
                                  "探测到游客进入封控区，已紧急停运并劝离")
            return {"ok": True, "pools": [p["name"] for p in pools]}

    def clear_intrusion(self, zone_id: int, actor: str):
        with self._global:
            adapter.set_intrusion(zone_id, False)
            self.conn.execute(
                "UPDATE faults SET status='resolved', resolution=? "
                "WHERE kind='intrusion' AND status IN ('open','repairing') AND pool_id IN "
                "(SELECT id FROM pools WHERE zone_id=?)",
                (f"{actor} 现场确认封控区已清空", zone_id))
            self.conn.commit()

    def create_schedule(self, pool_id: int, start_at: str, end_at: str,
                        days: str = "1234567") -> int:
        import re
        if not re.fullmatch(r"\d{2}:\d{2}", start_at or "") or start_at >= end_at:
            raise ServiceError("时间格式应为 HH:MM 且开始早于结束")
        days = "".join(sorted({d for d in days if d in "1234567"})) or "1234567"
        with self._global:
            self._pool(pool_id)
            for r in self.conn.execute(
                    "SELECT * FROM run_schedules WHERE pool_id=? AND enabled=1",
                    (pool_id,)):
                # 计划互斥：同一水池时段不可重叠
                if not (end_at <= r["start_at"] or start_at >= r["end_at"]):
                    raise ServiceError(
                        f"与现有计划 {r['start_at']}-{r['end_at']} 时段重叠")
            cur = self.conn.execute(
                "INSERT INTO run_schedules(pool_id,start_at,end_at,days,enabled) "
                "VALUES(?,?,?,?,1)", (pool_id, start_at, end_at, days))
            self.conn.commit()
            return cur.lastrowid

    def set_schedule_enabled(self, schedule_id: int, enabled: bool):
        with self._global:
            self.conn.execute("UPDATE run_schedules SET enabled=? WHERE id=?",
                              (1 if enabled else 0, schedule_id))
            self.conn.commit()

    # ================= 总览数据 =================
    def overview(self) -> dict:
        zones = [dict(r) for r in self.conn.execute("SELECT * FROM zones ORDER BY id")]
        for z in zones:
            pools = [dict(r) for r in self.conn.execute(
                "SELECT * FROM pools WHERE zone_id=?", (z["id"],)).fetchall()]
            z["pools"] = []
            for p in pools:
                detail = dict(p)
                detail["running"] = self.is_running(p["id"])
                detail["cleaning"] = self._active_cleaning(p["id"])
                detail["faults"] = self._open_faults(p["id"])
                detail["observing"] = self._observing(p["id"])
                last = self.conn.execute(
                    "SELECT * FROM water_readings WHERE pool_id=? ORDER BY id DESC LIMIT 1",
                    (p["id"],)).fetchone()
                detail["last_reading"] = dict(last) if last else None
                detail["pumps"] = [dict(r) for r in self.conn.execute(
                    "SELECT * FROM pumps WHERE pool_id=?", (p["id"],)).fetchall()]
                detail["nozzle_groups"] = [dict(r) for r in self.conn.execute(
                    "SELECT * FROM nozzle_groups WHERE pool_id=?", (p["id"],)).fetchall()]
                z["pools"].append(detail)
        return {"zones": zones, "weather": adapter.weather()}
