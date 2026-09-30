"""HTTP 服务：REST API + 静态前端 + 现场附件。零第三方依赖。"""
import json
import mimetypes
import re
import uuid
from datetime import datetime
from email.parser import BytesParser
from email.policy import default as email_default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config
from .adapters import adapter, chemical_cn
from .database import connect, init_db
from .scheduler import Scheduler
from .services import FAULT_TEXT, PermissionError_, PlatformService, ServiceError

CHEMICAL_CN_INV = {v: k for k, v in {"chlorine": "氯片", "flocculant": "絮凝剂",
                                     "algaecide": "除藻剂"}.items()}


def _rows(rows):
    return [dict(r) for r in rows]


def make_handler(svc: PlatformService):
    class Handler(BaseHTTPRequestHandler):
        server_version = "FountainPlatform/1.0"

        def log_message(self, fmt, *args):
            pass  # 安静输出

        # ---------- 基础工具 ----------
        def _send_json(self, obj, status: int = 200):
            data = json.dumps(obj, ensure_ascii=False, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_file(self, path):
            ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body_json(self):
            length = int(self.headers.get("Content-Length", 0))
            if not length:
                return {}
            raw = self.rfile.read(length)
            ctype = self.headers.get("Content-Type", "")
            if ctype.startswith("multipart/form-data"):
                return self._parse_multipart(ctype, raw)
            try:
                return json.loads(raw.decode())
            except json.JSONDecodeError:
                return {k: v[0] for k, v in parse_qs(raw.decode()).items()}

        def _parse_multipart(self, ctype: str, raw: bytes):
            msg = BytesParser(policy=email_default).parsebytes(
                f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n".encode() + raw)
            result = {}
            files = []
            for part in msg.iter_parts():
                disp = part.get("Content-Disposition", "")
                name = part.get_param("name", header="content-disposition")
                filename = part.get_filename()
                data = part.get_payload(decode=True) or b""
                if filename:
                    files.append({"field": name, "filename": filename, "data": data})
                else:
                    result[name] = data.decode(errors="replace")
            result["__files__"] = files
            return result

        def _role(self, payload):
            return payload.get("role") or self.headers.get("X-Role") or config.ROLE_KEEPER

        # ---------- 路由 ----------
        def do_GET(self):
            u = urlparse(self.path)
            p, q = u.path, parse_qs(u.query)
            try:
                if p == "/" or p == "/index.html":
                    return self._send_file(config.STATIC_DIR / "index.html")
                if p.startswith("/static/"):
                    return self._static(p[len("/static/"):])
                if p == "/api/overview":
                    return self._send_json(svc.overview())
                if p == "/api/weather":
                    return self._send_json(adapter.weather())
                m = re.fullmatch(r"/api/pools/(\d+)/readings", p)
                if m:
                    hours = int(q.get("hours", ["24"])[0])
                    return self._send_json(self._readings(int(m.group(1)), hours))
                if p == "/api/schedules":
                    return self._send_json(_rows(svc.conn.execute(
                        "SELECT s.*, p.name pool_name FROM run_schedules s "
                        "JOIN pools p ON p.id=s.pool_id ORDER BY s.pool_id,s.start_at")))
                if p == "/api/pumps":
                    return self._send_json(self._pumps())
                if p == "/api/dosing":
                    return self._send_json(_rows(svc.conn.execute(
                        "SELECT d.*, p.name pool_name FROM dosing_records d "
                        "JOIN pools p ON p.id=d.pool_id ORDER BY d.id DESC LIMIT 100")))
                if p == "/api/cleaning":
                    return self._send_json(_rows(svc.conn.execute(
                        "SELECT c.*, p.name pool_name FROM cleaning_tasks c "
                        "JOIN pools p ON p.id=c.pool_id ORDER BY c.id DESC")))
                if p == "/api/faults":
                    return self._send_json(_rows(svc.conn.execute(
                        "SELECT f.*, p.name pool_name FROM faults f "
                        "JOIN pools p ON p.id=f.pool_id ORDER BY f.id DESC")))
                if p == "/api/reopen":
                    return self._send_json(self._reopen_list())
                if p == "/api/logs":
                    return self._send_json(_rows(svc.conn.execute(
                        "SELECT l.*, p.name pool_name FROM run_logs l "
                        "JOIN pools p ON p.id=l.pool_id ORDER BY l.id DESC LIMIT 80")))
                if p == "/api/alerts":
                    return self._send_json(_rows(svc.conn.execute(
                        "SELECT a.*, p.name pool_name FROM alerts a "
                        "LEFT JOIN pools p ON p.id=a.pool_id ORDER BY a.id DESC LIMIT 50")))
                m = re.fullmatch(r"/api/attachments/(\w+)/(\d+)", p)
                if m:
                    return self._attachments(m.group(1), int(m.group(2)))
                if p == "/api/constants":
                    return self._send_json(self._constants())
                self._send_json({"error": "未找到"}, 404)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        def do_POST(self):
            u = urlparse(self.path)
            p = u.path
            payload = self._body_json()
            try:
                if p == "/api/pools/start":
                    pid = int(payload["pool_id"])
                    res = svc.start_pool(pid, payload.get("actor", "养护员"),
                                         manual=bool(payload.get("manual")))
                    return self._send_json(res, 200 if res["ok"] else 409)
                if p == "/api/pools/stop":
                    return self._send_json(svc.stop_pool(
                        int(payload["pool_id"]), payload.get("actor", "养护员"),
                        payload.get("reason", "人工停运")))
                if p == "/api/pools/refill":
                    return self._send_json(svc.refill(
                        int(payload["pool_id"]), payload.get("actor", "养护员")))
                m = re.fullmatch(r"/api/pools/(\d+)/readings", p)
                if m:
                    values = {"level": float(payload["level"]),
                              "turbidity": float(payload["turbidity"]),
                              "chlorine": float(payload["chlorine"])}
                    res = svc.add_reading(
                        int(m.group(1)), values,
                        kind=payload.get("kind", "manual"),
                        actor=payload.get("actor", "养护员"),
                        note=payload.get("note", ""))
                    self._save_attachments(payload, "water_readings", res["id"])
                    return self._send_json(res)
                if p == "/api/dosing":
                    chem = CHEMICAL_CN_INV.get(payload["chemical"], payload["chemical"])
                    res = svc.add_dosing(int(payload["pool_id"]), chem,
                                         float(payload["amount_ml"]),
                                         payload.get("actor", "养护员"))
                    return self._send_json(res, 200 if res["ok"] else 409)
                if p == "/api/schedules":
                    return self._send_json(self._create_schedule(payload))
                m = re.fullmatch(r"/api/schedules/(\d+)/toggle", p)
                if m:
                    svc.conn.execute("UPDATE run_schedules SET enabled=1-enabled WHERE id=?",
                                     (m.group(1),))
                    svc.conn.commit()
                    return self._send_json({"ok": True})
                if p == "/api/cleaning":
                    cid = svc.create_cleaning(int(payload["pool_id"]),
                                              payload["planned_date"],
                                              payload.get("assignee", "养护员"))
                    self._save_attachments(payload, "cleaning_tasks", cid)
                    return self._send_json({"id": cid})
                m = re.fullmatch(r"/api/cleaning/(\d+)/(lock|start|finish)", p)
                if m:
                    cid, act = int(m.group(1)), m.group(2)
                    actor = payload.get("actor", "养护员")
                    if act == "lock":
                        svc.lock_cleaning(cid, actor)
                    elif act == "start":
                        svc.start_cleaning(cid, actor)
                    else:
                        rid = svc.finish_cleaning(cid, actor)
                        self._save_attachments(payload, "cleaning_tasks", cid)
                        return self._send_json({"ok": True, "reopen_request_id": rid})
                    return self._send_json({"ok": True})
                m = re.fullmatch(r"/api/pools/(\d+)/nozzle-trial", p)
                if m:
                    return self._send_json(svc.nozzle_trial(
                        int(m.group(1)), payload.get("actor", "养护员")))
                m = re.fullmatch(r"/api/faults/(\d+)/(repair|finish|resolve)", p)
                if m:
                    fid, act = int(m.group(1)), m.group(2)
                    actor = payload.get("actor", "养护员")
                    if act == "repair":
                        return self._send_json(svc.begin_repair(fid, actor))
                    if act == "finish":
                        rid = svc.finish_repair(fid, actor)
                        return self._send_json({"ok": True, "reopen_request_id": rid})
                    return self._send_json(svc.resolve_fault(
                        fid, actor, payload.get("resolution", "现场处置完成")))
                m = re.fullmatch(r"/api/reopen/(\d+)/decide", p)
                if m:
                    res = svc.decide_reopen(
                        int(m.group(1)), payload["decision"],
                        payload.get("actor", "养护员"), self._role(payload),
                        payload.get("reject_reason", ""))
                    return self._send_json(res)
                m = re.fullmatch(r"/api/pools/(\d+)/reopen-request", p)
                if m:
                    return self._send_json({"id": svc.request_reopen(
                        int(m.group(1)), payload.get("actor", "养护员"))})
                if p == "/api/sim/scenario":
                    adapter.set_scenario(int(payload["pool_id"]), payload["scenario"])
                    return self._send_json({"ok": True})
                if p == "/api/sim/weather":
                    adapter.set_weather(bool(payload.get("bad")))
                    return self._send_json({"ok": True})
                if p == "/api/sim/pump-overheat":
                    adapter.force_pump_overheat(int(payload["pump_id"]))
                    return self._send_json({"ok": True})
                if p == "/api/sim/nozzle-block":
                    adapter.force_nozzle_block(int(payload["group_id"]),
                                               bool(payload.get("blocked", True)))
                    return self._send_json({"ok": True})
                if p == "/api/sim/intrusion":
                    if payload.get("clear"):
                        svc.clear_intrusion(int(payload["zone_id"]),
                                            payload.get("actor", "巡检员"))
                    else:
                        svc.report_intrusion(int(payload["zone_id"]),
                                             payload.get("actor", "巡检员"))
                    return self._send_json({"ok": True})
                self._send_json({"error": "未找到"}, 404)
            except ServiceError as e:
                self._send_json({"error": str(e)}, 400)
            except PermissionError_ as e:
                self._send_json({"error": str(e)}, 403)
            except KeyError as e:
                self._send_json({"error": f"缺少字段：{e}"}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        # ---------- 聚合查询 ----------
        def _readings(self, pool_id, hours):
            rows = svc.conn.execute(
                "SELECT * FROM water_readings WHERE pool_id=? AND ts>=datetime('now',?) "
                "ORDER BY ts", (pool_id, f"-{hours} hours")).fetchall()
            if not rows:
                rows = svc.conn.execute(
                    "SELECT * FROM water_readings WHERE pool_id=? ORDER BY ts DESC LIMIT 30",
                    (pool_id,)).fetchall()[::-1]
            return _rows(rows)

        def _pumps(self):
            pumps = _rows(svc.conn.execute(
                "SELECT pu.*, p.name pool_name FROM pumps pu JOIN pools p ON p.id=pu.pool_id "
                "ORDER BY pu.id"))
            for p in pumps:
                tele = adapter.read_pump(p["id"], p["status"] == "running")
                p["temp_c"] = tele["temp_c"]
            groups = _rows(svc.conn.execute("SELECT * FROM nozzle_groups ORDER BY id"))
            for g in groups:
                g.update(adapter.read_nozzle_group(g["id"]))
            return {"pumps": pumps, "nozzle_groups": groups}

        def _reopen_list(self):
            rows = _rows(svc.conn.execute(
                "SELECT r.*, p.name pool_name FROM reopen_requests r "
                "JOIN pools p ON p.id=r.pool_id ORDER BY r.id DESC"))
            for r in rows:
                if r["retest_reading_id"]:
                    rd = svc.conn.execute(
                        "SELECT * FROM water_readings WHERE id=?",
                        (r["retest_reading_id"],)).fetchone()
                    r["retest"] = dict(rd) if rd else None
                    if rd:
                        r["within_limits"] = svc.reading_in_range(dict(rd))
                else:
                    r["retest"] = None
            return rows

        def _create_schedule(self, payload):
            pid = int(payload["pool_id"])
            start, end = payload["start_at"], payload["end_at"]
            if not re.fullmatch(r"\d{2}:\d{2}", start or "") or start >= end:
                raise ServiceError("时间格式应为 HH:MM 且开始早于结束")
            days = payload.get("days", "1234567")
            days = "".join(sorted({d for d in days if d in "1234567"})) or "1234567"
            # 互斥：同一水池时段不可重叠
            for r in svc.conn.execute(
                    "SELECT * FROM run_schedules WHERE pool_id=? AND enabled=1", (pid,)):
                if not (end <= r["start_at"] or start >= r["end_at"]):
                    raise ServiceError(f"与现有计划 {r['start_at']}-{r['end_at']} 时段重叠")
            cur = svc.conn.execute(
                "INSERT INTO run_schedules(pool_id,start_at,end_at,days,enabled) "
                "VALUES(?,?,?,?,1)", (pid, start, end, days))
            svc.conn.commit()
            return {"id": cur.lastrowid}

        def _constants(self):
            return {
                "thresholds": {
                    "water_level_min": config.WATER_LEVEL_MIN,
                    "water_level_max": config.WATER_LEVEL_MAX,
                    "turbidity_max": config.TURBIDITY_MAX,
                    "chlorine_min": config.CHLORINE_MIN,
                    "chlorine_max": config.CHLORINE_MAX,
                    "pump_temp_max": config.PUMP_TEMP_MAX,
                    "nozzle_pressure_min": config.NOZZLE_PRESSURE_MIN,
                    "observe_minutes": config.DOSING_OBSERVE_MINUTES,
                },
                "colors": config.COLORS,
                "fault_text": FAULT_TEXT,
                "dose_limits": {"氯片": 500, "絮凝剂": 300, "除藻剂": 200},
            }

        def _attachments(self, ref_table, ref_id):
            rows = _rows(svc.conn.execute(
                "SELECT * FROM attachments WHERE ref_table=? AND ref_id=? ORDER BY id",
                (ref_table, ref_id)))
            return rows

        def _save_attachments(self, payload, ref_table, ref_id):
            for f in payload.get("__files__", []):
                if not f["data"]:
                    continue
                suffix = f["filename"].rsplit(".", 1)[-1][:8] if "." in f["filename"] else "bin"
                stored = f"{uuid.uuid4().hex}.{suffix}"
                path = config.ATTACH_DIR / stored
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f["data"])
                svc.conn.execute(
                    "INSERT INTO attachments(ref_table,ref_id,filename,stored_name,uploaded_at)"
                    " VALUES(?,?,?,?,?)",
                    (ref_table, ref_id, f["filename"], stored,
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                svc.conn.commit()

        def _static(self, rel: str):
            base = config.STATIC_DIR.resolve()
            target = (base / rel).resolve()
            if not str(target).startswith(str(base)) or not target.is_file():
                return self._send_json({"error": "未找到"}, 404)
            return self._send_file(target)

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8000, with_scheduler: bool = True):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    init_db(conn)
    svc = PlatformService(conn)
    sched = Scheduler(svc) if with_scheduler else None
    if sched:
        sched.start()
    httpd = ThreadingHTTPServer((host, port), make_handler(svc))
    print(f"喷泉平台已启动：http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if sched:
            sched.stop()
        httpd.server_close()


if __name__ == "__main__":
    serve()
