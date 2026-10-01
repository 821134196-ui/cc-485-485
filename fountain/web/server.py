"""Web 平台：stdlib http.server，静态页 + JSON API + 现场附件。"""
from __future__ import annotations

import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import adapters, config, services
from ..db import connect
from ..seed import seed

WEB_DIR = Path(__file__).resolve().parent

_POOL_CACHE = {}


def _state(conn) -> dict:
    def rows(sql, args=()):
        return [dict(r) for r in conn.execute(sql, args).fetchall()]

    now = services.now_dt()
    zones = rows("SELECT * FROM zones ORDER BY id")
    pools = rows("SELECT * FROM pools ORDER BY id")
    for p in pools:
        p["footprint"] = json.loads(p["footprint"])

    pumps = rows("SELECT * FROM pumps ORDER BY pool_id, code")
    groups = rows("SELECT * FROM nozzle_groups ORDER BY pool_id, id")
    for g in groups:
        g["position"] = json.loads(g["position"])
    schedules = rows("SELECT * FROM schedules ORDER BY id")
    date = services.today_str(now)
    sessions = rows(
        "SELECT * FROM run_sessions WHERE run_date>=? ORDER BY id DESC LIMIT 40",
        (date,))
    samples_latest = {}
    for p in pools:
        r = conn.execute(
            "SELECT * FROM samples WHERE pool_id=? ORDER BY ts DESC, id DESC LIMIT 1",
            (p["id"],)).fetchone()
        samples_latest[p["id"]] = dict(r) if r else None

    pool_states = []
    for p in pools:
        snap = services.pool_snapshot(conn, p["id"], now)
        obs = services.observation_remaining(conn, p["id"], now)
        pool_states.append({
            "pool_id": p["id"],
            "lock": snap["lock"],
            "observation_minutes": obs,
            "telemetry": {k: (v if not hasattr(v, "strftime") else services.iso(v))
                          for k, v in snap["telemetry"].items() if k != "weather"},
            "running": bool(snap["running"]),
        })

    events = rows("SELECT * FROM events ORDER BY id DESC LIMIT 30")
    cleans = rows("SELECT * FROM clean_tasks ORDER BY id DESC LIMIT 20")
    faults = rows("SELECT * FROM faults ORDER BY id DESC LIMIT 20")
    requests_ = rows("SELECT * FROM reopen_requests ORDER BY id DESC LIMIT 20")
    chemicals = rows("SELECT * FROM chemicals ORDER BY id DESC LIMIT 30")
    refills = rows("SELECT * FROM refill_logs ORDER BY id DESC LIMIT 20")
    attachments = rows("SELECT * FROM attachments ORDER BY id DESC")
    weather = adapters.get_weather(now)
    weather_block = adapters.weather_blocks(weather)
    return {
        "now": services.iso(now), "date": date,
        "zones": zones, "pools": pools, "pumps": pumps, "groups": groups,
        "schedules": schedules, "sessions": sessions,
        "latest_samples": samples_latest, "pool_states": pool_states,
        "events": events, "cleans": cleans, "faults": faults,
        "reopen_requests": requests_, "chemicals": chemicals, "refills": refills,
        "attachments": attachments,
        "weather": weather, "weather_block": weather_block,
        "thresholds": {
            "level_min": config.WATER_LEVEL_MIN, "level_max": config.WATER_LEVEL_MAX,
            "turbidity_max": config.TURBIDITY_MAX,
            "chlorine_min": config.CHLORINE_MIN, "chlorine_max": config.CHLORINE_MAX,
            "pump_temp_max": config.PUMP_TEMP_MAX,
            "dose_max": config.CHEMICAL_DOSE_MAX,
            "observation_minutes": config.OBSERVATION_MINUTES,
        },
    }


# ---------------------------------------------------------------- 动作分发

def _do_action(conn, data: dict) -> dict:
    a = data.get("action")
    role = data.get("role", config.ROLE_KEEPER)
    who = (data.get("operator") or "").strip() or (
        "主管" if role == config.ROLE_SUPERVISOR else "养护员")

    if a == "start":
        return services.attempt_start(conn, int(data["schedule_id"]),
                                      operator=who, manual=True)
    if a == "refill":
        services.add_refill(conn, int(data["pool_id"]), float(data["amount"]), who,
                            data.get("note", ""))
        return {"ok": True}
    if a == "chemical":
        return services.add_chemical(conn, int(data["pool_id"]), data["agent"],
                                     float(data["amount"]), who,
                                     int(data.get("minutes", config.OBSERVATION_MINUTES)),
                                     data.get("note", ""))
    if a == "sample":
        services.add_manual_sample(conn, int(data["pool_id"]),
                                   float(data["water_level"]), float(data["turbidity"]),
                                   float(data["chlorine"]), who, data.get("note", ""))
        return {"ok": True}
    if a == "clean_start":
        return services.start_clean(conn, int(data["pool_id"]), who, data.get("note", ""))
    if a == "clean_finish":
        return services.finish_clean(conn, int(data["id"]), who)
    if a == "fault_report":
        return services.report_fault(conn, int(data["pool_id"]), data["kind"],
                                     data["detail"], data.get("pump_id"))
    if a == "fault_repair":
        return services.repair_fault(conn, int(data["id"]), who, data.get("note", ""))
    if a == "retest":
        return services.retest_water(conn, int(data["pool_id"]), who)
    if a == "nozzle_trial":
        return services.nozzle_trial(conn, int(data["pool_id"]), who)
    if a == "reopen_request":
        return services.request_reopen(conn, data["ref_type"], int(data["ref_id"]), who)
    if a == "approve":
        return services.approve_reopen(conn, int(data["id"]), who, role)
    if a == "reject":
        return services.reject_reopen(conn, int(data["id"]), who, role,
                                      data.get("reason", "条件不满足"))
    if a == "visitor":
        return services.report_visitor(conn, int(data["pool_id"]),
                                       data.get("detail", "游客进入封控区"))
    if a == "resolve_event":
        return services.resolve_event(conn, int(data["id"]), who, data.get("resolution", "已处置"))
    if a == "schedule_toggle":
        cur = conn.execute("SELECT enabled FROM schedules WHERE id=?",
                           (int(data["id"]),)).fetchone()
        conn.execute("UPDATE schedules SET enabled=? WHERE id=?",
                     (0 if cur["enabled"] else 1, int(data["id"])))
        conn.commit()
        return {"ok": True, "enabled": not cur["enabled"]}
    if a == "sim_scenario":
        # 演示用：注入适配器场景（低水位/超限/过热/堵塞）
        pid = int(data["pool_id"])
        kind = data["kind"]
        if kind == "clear":
            adapters.clear_scenarios()
        elif kind == "low_water":
            adapters.set_scenario(pid, water_level=0.62)
        elif kind == "turbidity":
            adapters.set_scenario(pid, turbidity=7.8)
        elif kind == "chlorine_low":
            adapters.set_scenario(pid, chlorine=0.12)
        elif kind == "overheat":
            adapters.set_scenario(pid, pump_temp=82.0)
        elif kind == "clog":
            adapters.set_scenario(pid, clogged=True)
        return {"ok": True}
    if a == "sim_weather":
        adapters.set_weather(data.get("code", "clear"),
                             float(data.get("temp_c", 24)),
                             int(data.get("wind_level", 2)))
        if data.get("code", "clear") != "clear" or int(data.get("wind_level", 2)) >= config.WIND_STOP_LEVEL:
            services.weather_stop_running(conn)
        return {"ok": True}
    return {"ok": False, "reason": f"未知动作 {a}"}


# ---------------------------------------------------------------- multipart

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._一-龥-]")


def _parse_multipart(body: bytes, boundary: bytes) -> tuple[dict, list[dict]]:
    """返回 (普通字段 dict, 文件 [{field, filename, data}])。"""
    fields, files = {}, []
    for part in body.split(b"--" + boundary):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        head, _, content = part.partition(b"\r\n\r\n")
        headers = head.decode("utf-8", "replace")
        m_name = re.search(r'name="([^"]*)"', headers)
        m_file = re.search(r'filename="([^"]*)"', headers)
        if not m_name:
            continue
        name = m_name.group(1)
        if m_file:
            files.append({"field": name, "filename": m_file.group(1),
                          "data": content})
        else:
            fields[name] = content.decode("utf-8", "replace")
    return fields, files


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "FountainPlatform/1.0"

    def log_message(self, fmt, *args):
        pass

    def _db(self):
        return connect(self.server.db_path)

    def _send_json(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_static(self, rel: str, ctype: str):
        path = (WEB_DIR / rel).resolve()
        if not str(path).startswith(str(WEB_DIR)) or not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                conn = self._db()
                try:
                    if path == "/api/state":
                        return self._send_json(_state(conn))
                    m = re.fullmatch(r"/api/pools/(\d+)/samples", path)
                    if m:
                        hours = int(q.get("hours", ["48"])[0])
                        rows = [dict(r) for r in conn.execute("""
                            SELECT * FROM samples WHERE pool_id=?
                            AND ts >= datetime('now', ?) ORDER BY ts, id""",
                            (m.group(1), f"-{hours} hours")).fetchall()]
                        return self._send_json({"samples": rows})
                    m = re.fullmatch(r"/api/sessions/(\d+)", path)
                    if m:
                        days = int(q.get("days", ["14"])[0])
                        rows = [dict(r) for r in conn.execute("""
                            SELECT * FROM run_sessions WHERE pool_id=?
                            AND run_date >= date('now', ?) ORDER BY run_date, id""",
                            (m.group(1), f"-{days} days")).fetchall()]
                        return self._send_json({"sessions": rows})
                    self.send_error(404)
                finally:
                    conn.close()
            if path.startswith("/attachments/"):
                return self._send_attachment(path.split("/", 2)[2])
            static = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "application/javascript; charset=utf-8"),
                      "/styles.css": ("styles.css", "text/css; charset=utf-8")}
            if path in static:
                rel, ctype = static[path]
                return self._send_static(rel, ctype)
            self.send_error(404)
        except Exception as exc:
            self._send_json({"ok": False, "reason": str(exc)}, 500)

    def _send_attachment(self, stored: str):
        safe = Path(stored).name
        target = config.ATTACH_DIR / safe
        if not target.is_file():
            self.send_error(404)
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            ctype = self.headers.get("Content-Type", "")
            conn = self._db()
            try:
                if parsed.path == "/api/actions":
                    data = json.loads(body.decode("utf-8"))
                    res = _do_action(conn, data)
                    return self._send_json(res, 200 if res.get("ok", True) else 409)
                if parsed.path == "/api/attachments" and ctype.startswith("multipart/"):
                    boundary = ctype.split("boundary=", 1)[1].encode()
                    fields, files = _parse_multipart(body, boundary)
                    saved = []
                    for f in files:
                        if not f["data"]:
                            continue
                        stored = uuid.uuid4().hex + "_" + _SAFE_NAME.sub("_", f["filename"])
                        (config.ATTACH_DIR / stored).write_bytes(f["data"])
                        cur = conn.execute("""INSERT INTO attachments(ref_type, ref_id,
                            filename, stored_name, uploaded_by, ts, note)
                            VALUES (?,?,?,?,?,?,?)""",
                            (fields.get("ref_type", "pool"),
                             int(fields.get("ref_id", 0) or 0),
                             f["filename"], stored,
                             fields.get("uploaded_by", "养护员"),
                             services.iso(services.now_dt()),
                             fields.get("note", "")))
                        saved.append({"id": cur.lastrowid, "filename": f["filename"]})
                    conn.commit()
                    return self._send_json({"ok": True, "saved": saved})
                self.send_error(404)
            finally:
                conn.close()
        except Exception as exc:
            self._send_json({"ok": False, "reason": str(exc)}, 500)


def make_server(host: str, port: int, db_path=None) -> ThreadingHTTPServer:
    config.ensure_dirs()
    conn = connect(db_path)
    seed(conn)
    conn.close()
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.db_path = db_path
    httpd.allow_reuse_address = True
    return httpd
