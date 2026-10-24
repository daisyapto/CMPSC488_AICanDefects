"""
Web dashboard server (Flask).

    python3 run.py                    # serve on port 8080 and open the browser
    python3 run.py --kiosk            # Pi: fullscreen Chromium on the station screen
    python3 run.py --remote-control   # allow other devices to control the line

Endpoints
  GET  /                    dashboard (static/index.html)
  GET  /api/stream          Server-Sent Events: init, status (4/s), result, reject, log, sensor
  POST /api/cmd/<name>      start, stop, estop, reset, ack, simulate, test_burst, override,
                            lamp, snapshot, teach, clear_ref, pause, check_camera, clear_session
  POST /api/settings        {"key": value, ...} - validated, applied, saved to config.json
  GET  /video/live          MJPEG live camera view (with detection overlay)
  GET  /video/last.jpg      annotated image of the last inspected can
  GET  /api/image?path=     a saved inspection image (annotated copy if it exists)
  GET  /api/export.csv      this session's results
  GET  /display             big station display for a screen next to the line (view-only)
  GET  /api/alarms          active alarms + history, with troubleshooting steps
  GET  /api/history?days=   per-day totals from the daily logs
  GET  /api/diag            diagnostics: sensor timing / belt speed, storage, system

Safety: devices other than the Pi itself are view-only (plus E-stop and Stop)
unless the server is started with --remote-control.
"""
from __future__ import annotations

import csv
import hashlib
import ipaddress
import sys
import hmac
import io
import json
import queue
import secrets
import socket
import threading
import time
from collections import Counter, OrderedDict, deque
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file

from . import __version__
from .edition import EDITION, YOLO_AVAILABLE
from .config import APP_DIR, DATA_DIR, LIGHT_PINS, REJECT_PIN, SENSOR_PIN
from .controller import CSV_HEADER, ESCAPED, FIRED, SOURCES
from .history import History
from .report import render_report
from .review import DEFECT_TYPES, ReviewStore

STATIC_DIR = Path(__file__).resolve().parent / "static"


# =============================================================================
# Session store - shared by every connected browser
# =============================================================================
def result_dict(r) -> dict:
    img = ""
    if r.image:
        try:
            img = Path(r.image).resolve().relative_to(DATA_DIR.resolve()).as_posix()
        except ValueError:
            img = ""
    return {"id": r.can_id, "t": r.timestamp.strftime("%H:%M:%S"), "ts": r.timestamp.timestamp(),
            "pass": r.passed, "defect": r.defect, "conf": round(r.confidence, 3),
            "ms": round(r.inspect_ms, 1), "insp": r.inspector, "reject": r.reject, "img": img,
            "batch": r.batch}


class Session:
    MAX = 5000

    def __init__(self):
        self.lock = threading.Lock()
        self.clear()

    def clear(self):
        with getattr(self, "lock", threading.Lock()):
            self.results: OrderedDict = OrderedDict()
            self.started = time.time()
            self.n = self.passed = self.failed = self.fired = self.escaped = 0
            self.ms = 0.0
            self.defects: Counter = Counter()

    def add(self, d: dict):
        with self.lock:
            self.results[d["id"]] = d
            if len(self.results) > self.MAX:
                self.results.popitem(last=False)
            self.n += 1
            self.ms += d["ms"]
            if d["pass"]:
                self.passed += 1
            else:
                self.failed += 1
                self.defects[d["defect"] or "Unknown"] += 1
                if d["reject"] in ESCAPED:
                    self.escaped += 1

    def reject(self, can_id: int, status: str):
        with self.lock:
            d = self.results.get(can_id)
            if d is None:
                return
            d["reject"] = status
            if status == FIRED:
                self.fired += 1
            elif status in ESCAPED:
                self.escaped += 1

    def stats(self) -> dict:
        now = time.time()
        with self.lock:
            recent = sum(1 for d in reversed(self.results.values()) if now - d["ts"] <= 60) \
                if self.results else 0
            return {"n": self.n, "passed": self.passed, "failed": self.failed,
                    "fired": self.fired, "escaped": self.escaped,
                    "yield": (self.passed / self.n * 100) if self.n else None,
                    "rate": recent, "avg_ms": (self.ms / self.n) if self.n else None,
                    "started": datetime.fromtimestamp(self.started).strftime("%H:%M"),
                    "defects": self.defects.most_common()}

    def recent(self, limit=400) -> list:
        with self.lock:
            return list(self.results.values())[-limit:]

    def all(self) -> list:
        with self.lock:
            return list(self.results.values())


# =============================================================================
# Event hub - fans controller events out to every browser (SSE)
# =============================================================================
class Hub:
    def __init__(self, ctl, session: Session):
        self.ctl, self.session = ctl, session
        self.subs: set = set()
        self.lock = threading.Lock()
        self.logs: deque = deque(maxlen=300)
        self.viewers = 0
        threading.Thread(target=self._pump, daemon=True, name="web-pump").start()
        threading.Thread(target=self._status_loop, daemon=True, name="web-status").start()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=600)
        with self.lock:
            self.subs.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subs.discard(q)

    def publish(self, msg: dict):
        data = json.dumps(msg, default=str)
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(data)
            except queue.Full:            # a stalled browser must never block the line
                pass

    def _pump(self):
        while True:
            kind, payload = self.ctl.events.get()
            try:
                if kind == "result":
                    d = result_dict(payload)
                    self.session.add(d)
                    self.publish({"type": "result", "result": d, "stats": self.session.stats()})
                elif kind == "reject":
                    can_id, status = payload
                    self.session.reject(can_id, status)
                    self.publish({"type": "reject", "id": can_id, "status": status,
                                  "stats": self.session.stats()})
                elif kind == "log":
                    level, msg, ts = payload
                    entry = {"level": level, "msg": msg, "t": ts.strftime("%H:%M:%S")}
                    self.logs.append(entry)
                    self.publish({"type": "log", "entry": entry})
                elif kind == "sensor":
                    self.publish({"type": "sensor", "active": bool(payload)})
                elif kind == "audit":
                    self.publish({"type": "audit", "entry": payload})
                elif kind == "alarms":
                    self.publish({"type": "alarms", "version": payload})
            except Exception as e:                       # keep pumping no matter what
                print(f"[web] event error: {e!r}")

    def status(self) -> dict:
        ctl = self.ctl
        st = ctl.status()
        m = ctl.cammgr
        cam = {"available": m is not None}
        if m is not None:
            v = m.vision
            cam.update({
                "ok": m.ok, "status": m.status, "info": m.info, "fps": round(m.fps, 1),
                "resolution": m.resolution, "view": m.live_view, "paused": m.live_paused,
                "reference": (datetime.fromtimestamp(v.reference_time).strftime("%H:%M:%S")
                              if v.reference is not None else None),
                "engine": ctl.settings.vision_engine,
                "engine_error": {"team": v.team.error, "yolo": v.yolo.error}.get(ctl.settings.vision_engine, ""),
                "yolo_info": v.yolo.info, "yolo_ms": v.yolo.ms,
                "last_seq": m.last_seq,
                "brightness": m.measure() if m.ok else None,
                "auto_exposure": getattr(m, "_ae_running", False),
                "last": self._last_summary(m),
            })
        settings = {k: v for k, v in asdict(ctl.settings).items() if not k.startswith("engineer_pin")}
        st.update({"settings": settings, "camera": cam, "stats": self.session.stats(),
                   "recipe": ctl.recipes.summary(), "batch": ctl.batches.status(),
                   "edition": EDITION, "yolo_available": YOLO_AVAILABLE,
                   "time": datetime.now().strftime("%H:%M:%S"), "date": datetime.now().strftime("%a %b %d")})
        return st

    @staticmethod
    def _last_summary(m):
        last = m.last
        if not last:
            return None
        r = last["result"]
        path = ""
        if last["path"]:
            try:
                path = Path(last["path"]).resolve().relative_to(DATA_DIR.resolve()).as_posix()
            except ValueError:
                pass
        return {"id": last["can_id"], "pass": r.passed, "summary": r.summary(),
                "conf": round(r.confidence, 3), "ms": round(r.ms, 1), "method": r.method,
                "defects": [{"label": d.label, "area": d.area} for d in r.defects[:6]],
                "measures": r.measures,
                "img": path, "t": last["time"].strftime("%H:%M:%S")}

    def _status_loop(self):
        while True:
            time.sleep(0.25)
            with self.lock:
                if not self.subs:
                    continue
            try:
                self.publish({"type": "status", "status": self.status()})
            except Exception as e:
                print(f"[web] status error: {e!r}")


# =============================================================================
# Settings validation
# =============================================================================
def _int_range(lo, hi):
    def check(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("must be a number")
        return int(min(hi, max(lo, round(v))))
    return check


def _bool(v):
    if not isinstance(v, bool):
        raise ValueError("must be true/false")
    return v


def _choice(*options):
    def check(v):
        if v not in options:
            raise ValueError(f"must be one of {', '.join(options)}")
        return v
    return check


SETTING_RULES = {
    "reject_enabled": _bool, "single_shot": _bool, "save_annotated": _bool,
    "reject_delay_ms": _int_range(0, 5000), "pulses": _int_range(1, 10),
    "pulse_on_ms": _int_range(5, 500), "pulse_off_ms": _int_range(5, 500),
    "alarm_after": _int_range(1, 50), "warn_rate_pct": _int_range(5, 100),
    "camera_delay_ms": _int_range(0, 1000), "exposure_us": _int_range(20, 200000),
    "gain_db": _int_range(0, 48), "vision_sensitivity": _int_range(1, 100),
    "vision_min_area": _int_range(5, 5000), "sim_defect_rate": _int_range(0, 100),
    "save_images": _choice("off", "fails", "all"),
    "webcam_index": _int_range(0, 9),
    "vision_engine": _choice("builtin", "team", *(["yolo"] if YOLO_AVAILABLE else [])),
    "active_cooler": _bool,
    "yolo_conf": _int_range(5, 95), "yolo_imgsz": _int_range(160, 1280), "yolo_crop": _bool,
    "camera_fps": _int_range(5, 30), "standby_after_min": _int_range(0, 120),
    "can_diameter_mm": _int_range(20, 200), "camera_distance_mm": _int_range(0, 5000),
    "nozzle_distance_mm": _int_range(0, 5000), "keep_images_days": _int_range(0, 365),
}


def apply_setting(ctl, key, value):
    """Validate and apply one setting. Raises ValueError with a readable message."""
    m = ctl.cammgr
    if key == "inspector":
        if value not in ctl.inspectors:
            raise ValueError("unknown inspector")
        ctl.set_inspector(value)
    elif key == "camera_ip":
        if not isinstance(value, str) or not ctl.set_camera_ip(value):
            raise ValueError("not a valid IPv4 address")
    elif key == "camera_backend":
        if value not in SOURCES or m is None:
            raise ValueError("unknown camera source")
        m.switch_source(value)
    elif key == "image_folder":
        if not isinstance(value, str) or not Path(value).expanduser().is_dir():
            raise ValueError("folder not found on the station")
        ctl.settings.image_folder = str(Path(value).expanduser())
        if m is not None and ctl.settings.camera_backend == "folder":
            m.reopen()
    elif key == "yolo_model":
        if not isinstance(value, str) or not value.strip():
            raise ValueError("enter the model file path")
        p = Path(value.strip()).expanduser()
        if not (p if p.is_absolute() else APP_DIR / p).exists():
            raise ValueError(f"not found on the station: {p}")
        ctl.settings.yolo_model = value.strip()
        if m is not None:
            m.preload_model()
    elif key == "inspect_area":
        if value is not None:
            if not (isinstance(value, list) and len(value) == 4 and
                    all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)):
                raise ValueError("expected [x0, y0, x1, y1] between 0 and 1")
            x0, y0, x1, y1 = (min(1.0, max(0.0, float(v))) for v in value)
            x0, x1, y0, y1 = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
            if x1 - x0 < 0.05 or y1 - y0 < 0.05:
                raise ValueError("the area is too small")
            value = None if (x0 <= 0.001 and y0 <= 0.001 and x1 >= 0.999 and y1 >= 0.999) else \
                [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)]
        ctl.settings.inspect_area = value
    elif key == "live_view":
        if m is None or value not in ("detect", "raw", "mask", "edges", "focus"):
            raise ValueError("unknown view")
        m.live_view = value
        return                                    # not a saved setting
    elif key in SETTING_RULES:
        value = SETTING_RULES[key](value)
        if key == "sim_defect_rate":
            ctl.set_defect_rate(value)
        else:
            ctl.set_setting(key, value)
        if key in ("exposure_us", "gain_db") and m is not None:
            m.apply_settings()
        if key in ("camera_fps", "standby_after_min"):
            ctl._apply_camera_load()
        if key in ("vision_engine", "yolo_imgsz") and m is not None:
            m.preload_model()
        if key == "webcam_index" and m is not None:
            if ctl.settings.camera_backend == "webcam":
                m.reopen()
            else:
                m.switch_source("webcam")
    else:
        raise ValueError("unknown setting")
    ctl.save_settings()


# =============================================================================
# Access control: viewer < operator < engineer
# =============================================================================
DEFAULT_PIN = "1234"
IDLE_LOGOUT_S = 15 * 60
ROLE_RANK = {"viewer": 0, "operator": 1, "engineer": 2}
CMD_ROLE = {                      # minimum role per command
    "stop": "viewer", "estop": "viewer",
    "start": "operator", "reset": "operator", "ack": "operator", "simulate": "operator",
    "wake": "operator", "pause": "operator", "check_camera": "operator",
    "snapshot": "engineer", "teach": "engineer", "clear_ref": "engineer", "test_burst": "engineer",
    "override": "engineer", "lamp": "engineer", "clear_session": "engineer",
    "selftest": "engineer", "cleanup": "engineer", "auto_exposure": "engineer",
}
OPERATOR_SETTINGS = {"live_view"}
NOT_AUDITED = {"simulate", "pause", "check_camera", "lamp"}


class Auth:
    """Engineer PIN login. The PIN is stored hashed (PBKDF2) in config.json."""

    def __init__(self, ctl):
        self.ctl = ctl
        self.sessions: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.fails = 0
        self.locked_until = 0.0

    @property
    def default_pin(self) -> bool:
        return not self.ctl.settings.engineer_pin_hash

    @staticmethod
    def _hash(pin: str, salt: str) -> str:
        return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 120_000).hex()

    def _check(self, pin: str) -> bool:
        s = self.ctl.settings
        if not s.engineer_pin_hash:
            return hmac.compare_digest(pin, DEFAULT_PIN)
        return hmac.compare_digest(self._hash(pin, s.engineer_pin_salt), s.engineer_pin_hash)

    def login(self, name: str, pin: str) -> str:
        name = name.strip()
        if not 1 <= len(name) <= 30:
            raise ValueError("Enter your name")
        with self.lock:
            wait = self.locked_until - time.monotonic()
            if wait > 0:
                raise ValueError(f"Too many wrong PINs - try again in {wait:.0f} s")
            if not self._check(str(pin)):
                self.fails += 1
                if self.fails >= 5:
                    self.fails, self.locked_until = 0, time.monotonic() + 60
                raise ValueError("Wrong PIN")
            self.fails = 0
            token = secrets.token_urlsafe(24)
            self.sessions[token] = {"name": name, "last": time.monotonic()}
            return token

    def user(self, token, touch=True):
        with self.lock:
            s = self.sessions.get(token or "")
            if not s:
                return None
            if time.monotonic() - s["last"] > IDLE_LOGOUT_S:
                del self.sessions[token]
                return None
            if touch:
                s["last"] = time.monotonic()
            return s

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token or "", None)

    def set_pin(self, pin: str):
        if not (pin.isdigit() and 4 <= len(pin) <= 8):
            raise ValueError("PIN must be 4-8 digits")
        if pin == DEFAULT_PIN:
            raise ValueError("choose a PIN other than the default")
        salt = secrets.token_hex(16)
        self.ctl.settings.engineer_pin_salt = salt
        self.ctl.settings.engineer_pin_hash = self._hash(pin, salt)
        self.ctl.save_settings()


# =============================================================================
# Flask app
# =============================================================================
def create_app(ctl, remote_control: bool = False) -> Flask:
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    session = Session()
    hub = Hub(ctl, session)
    auth = Auth(ctl)
    review = ReviewStore()
    history = History()
    app.config["hub"] = hub

    def is_local() -> bool:
        return request.remote_addr in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def can_control() -> bool:
        return remote_control or is_local()

    def identity(touch=True):
        """(role, name) of the caller."""
        if not can_control():
            return "viewer", "viewer"
        s = auth.user(request.cookies.get("eng"), touch)
        if s:
            return "engineer", s["name"]
        b = ctl.batches.active
        return "operator", (b["operator"] if b else "operator")

    def source():
        return "station" if is_local() else request.remote_addr

    def need(min_role):
        """None if allowed, else an error response."""
        role, _ = identity()
        if ROLE_RANK[role] >= ROLE_RANK[min_role]:
            return None
        if role == "viewer":
            return jsonify(ok=False, error="View-only device - use the station screen, "
                                           "or start the server with --remote-control"), 403
        return jsonify(ok=False, error="Engineer login required", login=True), 403

    def audit(action, detail=""):
        role, name = identity(touch=False)
        ctl.audit.record(name, role, action, detail, source())

    def ok(**kw):
        return jsonify(ok=True, **kw)

    def fail(msg, code=409):
        return jsonify(ok=False, error=msg), code

    @app.after_request
    def no_cache(resp):
        if request.path.startswith("/api/") or request.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/")
    def index():
        return send_file(STATIC_DIR / "index.html")

    @app.get("/display")
    def display():
        return send_file(STATIC_DIR / "display.html")

    # ---------------------------------------------------------------- stream
    @app.get("/api/stream")
    def stream():
        q = hub.subscribe()
        role, name = identity()
        init = {"type": "init", "version": __version__, "can_control": can_control(),
                "role": role, "name": name, "default_pin": auth.default_pin,
                "status": hub.status(), "results": session.recent(), "logs": list(hub.logs),
                "audit": list(ctl.audit.recent)[-200:], "defect_types": DEFECT_TYPES,
                "inspectors": [{"name": n, "description": i.description} for n, i in ctl.inspectors.items()],
                "sources": [{"key": k, "label": v} for k, v in SOURCES.items()],
                "pins": {"sensor": SENSOR_PIN, "reject": REJECT_PIN, "lights": LIGHT_PINS}}

        def gen():
            try:
                yield f"data: {json.dumps(init, default=str)}\n\n"
                while True:
                    try:
                        data = q.get(timeout=10)
                        yield f"data: {data}\n\n"
                    except queue.Empty:
                        yield ": keep-alive\n\n"
            finally:
                hub.unsubscribe(q)

        return Response(gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------ auth
    @app.get("/api/me")
    def me():
        role, name = identity(touch=False)
        s = auth.user(request.cookies.get("eng"), touch=False) if role == "engineer" else None
        left = int(IDLE_LOGOUT_S - (time.monotonic() - s["last"])) if s else None
        return ok(role=role, name=name, idle_left=left, default_pin=auth.default_pin,
                  can_control=can_control())

    @app.post("/api/login")
    def login():
        if not can_control():
            return need("operator")
        body = request.get_json(silent=True) or {}
        try:
            token = auth.login(str(body.get("name", "")), str(body.get("pin", "")))
        except ValueError as e:
            ctl.audit.record(str(body.get("name", ""))[:30] or "-", "operator", "login failed", str(e), source())
            return fail(str(e), 401)
        resp = ok(role="engineer", name=body["name"].strip(), default_pin=auth.default_pin)
        resp.set_cookie("eng", token, httponly=True, samesite="Strict")
        audit_name = body["name"].strip()
        ctl.audit.record(audit_name, "engineer", "login", "", source())
        return resp

    @app.post("/api/logout")
    def logout():
        role, name = identity(touch=False)
        if role == "engineer":
            ctl.audit.record(name, role, "logout", "", source())
        auth.logout(request.cookies.get("eng"))
        resp = ok()
        resp.delete_cookie("eng")
        return resp

    @app.post("/api/pin")
    def change_pin():
        if (e := need("engineer")):
            return e
        try:
            auth.set_pin(str((request.get_json(silent=True) or {}).get("pin", "")))
        except ValueError as e:
            return fail(str(e), 400)
        audit("engineer PIN changed")
        return ok(message="Engineer PIN changed")

    # -------------------------------------------------------------- commands
    @app.post("/api/cmd/<name>")
    def command(name):
        if name not in CMD_ROLE:
            abort(404)
        if (e := need(CMD_ROLE[name])):
            return e
        body = request.get_json(silent=True) or {}
        m = ctl.cammgr
        msg, detail = "", ""
        if name == "start":
            if ctl.estop:
                return fail("E-stop is active - press Reset first")
            if "overheat" in ctl._latched:
                return fail("The station overheated - check cooling, then Reset")
            ctl.start()
        elif name == "stop":
            ctl.stop()
        elif name == "estop":
            ctl.emergency_stop()
        elif name == "reset":
            ctl.reset()
        elif name == "ack":
            detail = ctl.warning_reason
            ctl.acknowledge()
        elif name == "simulate":
            if not ctl.running:
                return fail("Start the line first")
            ctl.simulate_can()
        elif name == "test_burst":
            ctl.test_burst()
        elif name == "override":
            if not ctl.set_manual_override(bool(body.get("on"))):
                return fail("Manual override only while stopped")
            detail = "on" if body.get("on") else "off"
        elif name == "lamp":
            if body.get("key") not in LIGHT_PINS:
                abort(400)
            ctl.manual_toggle(body["key"])
        elif name == "check_camera":
            ctl.check_camera()
        elif name == "wake":
            ctl.wake_camera()
        elif name in ("snapshot", "teach", "clear_ref", "pause"):
            if m is None:
                return fail("Camera not available")
            if name == "snapshot":
                path = m.snapshot()
                if path is None:
                    return fail("No camera frame")
                msg, detail = f"Snapshot saved: {path.name}", path.name
                ctl.log("INFO", f"Snapshot saved: {path}")
            elif name == "teach":
                if not ctl.teach_reference():
                    return fail("No camera frame to teach from")
                msg, detail = f"Good-can reference taught for '{ctl.recipes.active}'", ctl.recipes.active
            elif name == "clear_ref":
                ctl.clear_reference()
                detail = ctl.recipes.active
            else:
                m.live_paused = not m.live_paused
        elif name == "clear_session":
            session.clear()
            hub.publish({"type": "cleared"})
        elif name == "auto_exposure":
            if m is None:
                return fail("Camera not available")

            def done(text, good):
                ctl.save_settings()
                speed = ctl.belt_stats().get("speed_mm_s")
                if speed:
                    blur = speed * ctl.settings.exposure_us / 1e6
                    text += f". At the measured belt speed a can moves {blur:.1f} mm during one exposure"
                ctl.log("INFO" if good else "WARN", f"Auto exposure: {text}")
            try:
                m.auto_exposure(done)
            except ValueError as e:
                return fail(str(e))
            msg = "Adjusting exposure… (result in the event log)"
        elif name == "selftest":
            try:
                ctl.self_test()
            except ValueError as e:
                return fail(str(e))
            msg = "Self-test running: green, yellow, red, then one air burst"
        elif name == "cleanup":
            from .review import cleanup_images
            n = cleanup_images(ctl.settings.keep_images_days, ctl.log)
            msg = detail = f"{n} old image files deleted" if ctl.settings.keep_images_days else \
                "Keep images is 0 (keep all) - nothing deleted"
        if name not in NOT_AUDITED:
            audit(name.replace("_", " "), detail)
        return ok(message=msg)

    @app.post("/api/settings")
    def settings():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not body:
            return fail("expected a JSON object", 400)
        for key in body:
            if (e := need("operator" if key in OPERATOR_SETTINGS else "engineer")):
                return e
        for key, value in body.items():
            old = getattr(ctl.settings, key, None)
            try:
                apply_setting(ctl, key, value)
            except ValueError as e:
                return fail(f"{key}: {e}", 400)
            new = getattr(ctl.settings, key, value)
            if key not in OPERATOR_SETTINGS and old != new:
                audit("setting changed", f"{key}: {old} -> {new}")
        return ok()

    # --------------------------------------------------------------- recipes
    @app.post("/api/recipes/<action>")
    def recipes(action):
        body = request.get_json(silent=True) or {}
        if (e := need("operator" if action == "select" else "engineer")):
            return e
        try:
            if action == "select":
                ctl.select_recipe(str(body.get("name", "")))
                audit("recipe selected", body.get("name", ""))
            elif action == "save":
                ctl.save_recipe()
                audit("recipe saved", ctl.recipes.active)
                return ok(message=f"Saved to recipe '{ctl.recipes.active}'")
            elif action == "create":
                ctl.create_recipe(str(body.get("name", "")), str(body.get("description", "")))
                audit("recipe created", body.get("name", ""))
                return ok(message=f"Recipe '{body.get('name', '').strip()}' created")
            elif action == "delete":
                ctl.delete_recipe(str(body.get("name", "")))
                audit("recipe deleted", body.get("name", ""))
            else:
                abort(404)
        except ValueError as e:
            return fail(str(e))
        return ok()

    # --------------------------------------------------------------- batches
    @app.post("/api/batch/<action>")
    def batch(action):
        if (e := need("operator")):
            return e
        body = request.get_json(silent=True) or {}
        try:
            if action == "start":
                b = ctl.start_batch(str(body.get("lot", "")), str(body.get("operator", "")),
                                    str(body.get("note", "")))
                audit("batch started", f"lot {b['lot']}, recipe {b['recipe']}")
                return ok(id=b["id"])
            if action == "end":
                role, who = identity(touch=False)          # before the batch (and its operator) closes
                b = ctl.end_batch()
                ctl.audit.record(who, role, "batch ended", f"lot {b['lot']}: {b['n']} cans", source())
                return ok(id=b["id"], message=f"Batch {b['lot']} ended - report ready")
        except ValueError as e:
            return fail(str(e))
        abort(404)

    @app.get("/api/batches")
    def batches():
        return ok(batches=ctl.batches.list())

    def batch_review_stats(bid):
        p = ctl.batches.results_csv(bid)
        if not p:
            return None
        root = DATA_DIR.resolve()
        paths = []
        with p.open() as f:
            for row in csv.DictReader(f):
                if row.get("image"):
                    try:
                        paths.append(Path(row["image"]).resolve().relative_to(root).as_posix())
                    except ValueError:
                        pass
        return review.stats_for_paths(paths)

    @app.get("/report/<bid>")
    def report(bid):
        b = ctl.batches.get(bid)
        if b is None:
            abort(404)
        html = render_report(b, batch_review_stats(bid), embed=request.args.get("embed") == "1")
        return Response(html, mimetype="text/html")

    @app.get("/api/batch/<bid>.csv")
    def batch_csv(bid):
        p = ctl.batches.results_csv(bid)
        if not p:
            abort(404)
        return send_file(p, mimetype="text/csv", as_attachment=True, download_name=f"batch_{bid}.csv")

    # ------------------------------------------------- GigE camera finder
    @app.post("/api/gige/find")
    def gige_find():
        if (e := need("operator")):
            return e
        from .gige import discover
        try:
            r = discover()
        except OSError as e:
            return fail(f"network search failed: {e}", 500)
        for c in r["cameras"]:
            c["current"] = c["ip"] == ctl.settings.camera_ip
        ctl.log("INFO", "Camera search: " + (", ".join(f"{c['model']} at {c['ip']}" for c in r["cameras"])
                                            or "no GigE camera answered"))
        return ok(**r, os=sys.platform)

    @app.post("/api/gige/forceip")
    def gige_forceip():
        if (e := need("engineer")):
            return e
        from .gige import force_ip
        body = request.get_json(silent=True) or {}
        try:
            ip, mask, mac = str(body["ip"]), str(body.get("mask", "255.255.0.0")), str(body["mac"])
            ipaddress.IPv4Address(ip), ipaddress.IPv4Address(mask)
        except (KeyError, ValueError):
            return fail("need mac, ip and mask", 400)
        if not force_ip(mac, ip, mask):
            return fail("the camera did not confirm the new IP - search again to check")
        audit("camera IP forced", f"{mac} -> {ip}/{mask} (temporary, until power-off)")
        ctl.log("INFO", f"Camera {mac} given temporary IP {ip} (lost when the camera is power-cycled)")
        return ok(message=f"Camera now at {ip} (until it is power-cycled)")

    @app.post("/api/webcams")
    def webcams():
        if (e := need("engineer")):
            return e
        if ctl.cammgr is None:
            return fail("camera support not installed", 400)
        return ok(cameras=ctl.cammgr.find_webcams(), current=ctl.settings.webcam_index,
                  active=ctl.settings.camera_backend == "webcam")

    # ---------------------------------------------------------------- review
    @app.get("/api/review")
    def review_list():
        a = request.args
        return ok(**review.query(a.get("status", "todo"), a.get("result", "fail"), a.get("date", ""),
                                 int(a.get("offset", 0) or 0), min(200, int(a.get("limit", 60) or 60))))

    @app.post("/api/review/label")
    def review_label():
        if (e := need("engineer")):
            return e
        body = request.get_json(silent=True) or {}
        _, name = identity()
        try:
            rec = review.set(str(body.get("path", "")), body.get("verdict"),
                             str(body.get("defect_type", "") or ""), name)
        except ValueError as e:
            return fail(str(e), 400)
        return ok(label=rec)

    @app.get("/api/thumb")
    def thumb():
        p = review.thumbnail(request.args.get("path", ""))
        if p is None:
            abort(404)
        return send_file(p, mimetype="image/jpeg", max_age=3600)

    @app.get("/api/review/export")
    def review_export():
        if (e := need("engineer")):
            return e
        try:
            z = review.export_dataset()
        except ValueError as e:
            return fail(str(e))
        audit("dataset exported", z.name)
        return send_file(z, mimetype="application/zip", as_attachment=True, download_name=z.name)

    # ------------------------------------------------- alarms, history, diag
    @app.get("/api/alarms")
    def alarms():
        return ok(**ctl.alarms.snapshot())

    @app.get("/api/history")
    def history_summary():
        try:
            days = min(366, max(1, int(request.args.get("days", 30))))
        except ValueError:
            days = 30
        return ok(**history.summary(days))

    @app.get("/api/day/<d>.csv")
    def day_csv(d):
        files = history.day_files(d)
        if not files:
            abort(404)
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=CSV_HEADER, extrasaction="ignore")
        w.writeheader()
        for p in files:
            with p.open(newline="") as f:
                for row in csv.DictReader(f):
                    w.writerow(row)
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=inspections_{d}.csv"})

    @app.get("/api/diag")
    def diag():
        s = ctl.settings
        st = ctl.status()
        m = ctl.cammgr
        return ok(belt=ctl.belt_stats(), sensor_mode=st["sensor_mode"], sensor_error=ctl.sensor.error,
                  sim=st["sim"], disk_free_mb=st["disk_free_mb"], disk_total_mb=st["disk_total_mb"],
                  images_mb=_folder_mb(DATA_DIR / "images"), edition=EDITION, version=__version__,
                  model=ctl.health.model or "not a Raspberry Pi", uptime=st["uptime"],
                  cpu_temp=st["cpu_temp"], cam_temp=st["cam_temp"], thermal=st["thermal"],
                  power=st["power"], is_pi=st["is_pi"],
                  camera={"source": SOURCES.get(s.camera_backend, s.camera_backend),
                          "ok": bool(m and m.ok), "status": m.status if m else "not installed",
                          "info": m.info if m else "", "fps": round(m.fps, 1) if m else 0,
                          "resolution": m.resolution if m else None,
                          "ip": s.camera_ip, "online": st["cam_online"], "ms": st["cam_ms"],
                          "ping": st["cam_ping"]},
                  timing={"camera_delay_ms": s.camera_delay_ms, "reject_delay_ms": s.reject_delay_ms,
                          "burst_ms": s.burst_ms, "avg_inspect_ms": session.stats()["avg_ms"],
                          "can_diameter_mm": s.can_diameter_mm,
                          "camera_distance_mm": s.camera_distance_mm,
                          "nozzle_distance_mm": s.nozzle_distance_mm},
                  selftest=ctl.selftest_running, running=ctl.running, estop=ctl.estop,
                  keep_images_days=s.keep_images_days)

    @app.get("/api/audit")
    def audit_list():
        return ok(entries=list(ctl.audit.recent))

    # ----------------------------------------------------------- images/video
    @app.get("/video/live")
    def live():
        m = ctl.cammgr
        if m is None:
            abort(404)
        import cv2

        def gen():
            hub.viewers += 1
            m.live_visible = True
            shown = -1
            try:
                while True:
                    lf = m.live_frame()
                    if lf is not None and lf[2] != shown and m.ok:
                        shown = lf[2]
                        ok_, jpg = cv2.imencode(".jpg", lf[0], [cv2.IMWRITE_JPEG_QUALITY, 80])
                        if ok_:
                            b = jpg.tobytes()
                            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                   + str(len(b)).encode() + b"\r\n\r\n" + b + b"\r\n")
                    time.sleep(1 / 20)
            finally:
                hub.viewers -= 1
                m.live_visible = hub.viewers > 0

        return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame",
                        headers={"Cache-Control": "no-cache"})

    @app.get("/video/last.jpg")
    def last_jpg():
        m = ctl.cammgr
        if m is None or not m.last:
            abort(404)
        import cv2
        ok_, jpg = cv2.imencode(".jpg", m.last["image"], [cv2.IMWRITE_JPEG_QUALITY, 88])
        return Response(jpg.tobytes(), mimetype="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/image")
    def image():
        rel = request.args.get("path", "")
        root = DATA_DIR.resolve()
        p = (root / rel).resolve()
        if root not in p.parents or not p.is_file():
            abort(404)
        annotated = p.parent / "annotated" / p.name
        if request.args.get("raw") != "1" and annotated.is_file():
            p = annotated
        return send_file(p)

    @app.get("/api/export.csv")
    def export():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(CSV_HEADER)
        for d in session.all():
            w.writerow([d["id"], datetime.fromtimestamp(d["ts"]).isoformat(timespec="milliseconds"),
                        "PASS" if d["pass"] else "FAIL", d["defect"], d["conf"], d["ms"],
                        d["insp"], d["reject"], d["img"], d.get("batch", "")])
        name = f"session_{datetime.fromtimestamp(session.started):%Y%m%d_%H%M}.csv"
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={name}"})

    return app


# =============================================================================
_folder_cache: dict = {}


def _folder_mb(path: Path):
    """Size of a folder in MB (cached for a minute - walking thousands of images is slow)."""
    hit = _folder_cache.get(str(path))
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1]
    total = 0
    if path.is_dir():
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
    mb = total / 2**20
    _folder_cache[str(path)] = (time.monotonic(), mb)
    return mb


def lan_address():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))          # no traffic is sent
            return s.getsockname()[0]
    except OSError:
        return None


def serve(ctl, host="0.0.0.0", port=8080, open_browser=True, kiosk=False, remote_control=False,
          screen="dashboard"):
    import shutil
    import subprocess
    import webbrowser
    from werkzeug.serving import make_server

    import logging
    logging.getLogger("werkzeug").setLevel(logging.WARNING)     # no per-request log lines
    app = create_app(ctl, remote_control)
    server = make_server(host, port, app, threaded=True)
    url = f"http://localhost:{port}"
    print(f"\n  Dashboard:  {url}")
    print(f"  Display:    {url}/display   (big screen next to the line)")
    ip = lan_address()
    if ip and host in ("0.0.0.0", "::"):
        mode = "full control" if remote_control else "view-only + E-stop/Stop"
        print(f"  Network:    http://{ip}:{port}   ({mode})   ·   http://{ip}:{port}/display")
    if screen == "display":
        url += "/display"
    print("  Stop with Ctrl+C\n")

    def launch():
        time.sleep(1.0)
        if kiosk:
            exe = shutil.which("chromium-browser") or shutil.which("chromium")
            if exe:
                subprocess.Popen([exe, "--kiosk", "--noerrdialogs", "--disable-infobars",
                                  "--disable-session-crashed-bubble", "--check-for-update-interval=31536000",
                                  url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            print("  Chromium not found - opening the default browser instead")
        webbrowser.open(url)

    if kiosk or open_browser:
        threading.Thread(target=launch, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        server.shutdown()
