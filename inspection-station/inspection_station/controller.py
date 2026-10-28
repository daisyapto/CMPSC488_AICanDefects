"""
Line logic: sensor -> inspection -> timed reject, stack light, alarms, logging.

Threads
  * sensor     - edge callbacks (gpiozero) or polling (pinctrl)
  * inspect    - one worker, inspects cans in arrival order (like a real camera)
  * reject     - fires the air at the exact time each failed can reaches the nozzle
  * lights     - drives the stack light from the current state
  * camera     - pings the camera IP periodically
The UI never touches hardware directly; it calls Controller methods and reads
Controller.status(). Everything the UI needs to show arrives via the events queue.
"""
from __future__ import annotations

import csv
import dataclasses
import shutil
import heapq
import ipaddress
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime

from .config import (CAMERA_CHECK_INTERVAL_S, DATA_DIR, DEFECT_LAMP_S, LATE_TOLERANCE_MS,
                     Settings)
from .config import (CAM_MAX_C, CAM_WARN_C, CPU_CRITICAL_C, CPU_HOT_C, CPU_WARM_C,
                     DISK_MIN_MB, STANDBY_FPS, TEMP_HYSTERESIS_C)
from .alarms import AlarmLog
from .review import cleanup_images
from .hardware import CameraLink, Rejector, SensorInput, StackLight
from .conveyor import Conveyor
from .audit import Audit
from .batches import BatchManager
from .health import SystemHealth
from .recipes import RECIPE_FIELDS, RecipeBook
from .inspectors import Outcome, SimulatedInspector, build_inspectors

try:                                    # camera support needs numpy + OpenCV
    from .camera import SOURCES, CameraManager
    CAMERA_IMPORT_ERROR = ""
except Exception as _e:                 # app still runs without them
    CameraManager, SOURCES = None, {}
    CAMERA_IMPORT_ERROR = f"{_e.__class__.__name__}: {_e}"

# reject status of a can
PASS, PENDING, FIRED, MISSED, DISABLED, LOCKED = "-", "pending", "fired", "missed", "disabled", "locked"
REJ_OFF = "off"                          # rejector option off: the can is only marked FAIL
ESCAPED = {MISSED, DISABLED, LOCKED}     # failed can that was NOT blown off the line


@dataclass
class InspectionResult:
    can_id: int
    timestamp: datetime
    passed: bool
    defect: str
    confidence: float
    inspect_ms: float
    inspector: str
    reject: str = PASS
    image: str = ""          # saved image path (camera inspector)
    batch: str = ""          # batch id the can belongs to

    @property
    def escaped(self) -> bool:
        return not self.passed and self.reject in ESCAPED


CSV_HEADER = ["can_id", "timestamp", "result", "defect", "confidence",
              "inspect_ms", "inspector", "reject", "image", "batch"]


def result_row(r: InspectionResult) -> list:
    return [r.can_id, r.timestamp.isoformat(timespec="milliseconds"),
            "PASS" if r.passed else "FAIL", r.defect, f"{r.confidence:.3f}",
            f"{r.inspect_ms:.1f}", r.inspector, r.reject, r.image, r.batch]


# =============================================================================
class DataLogger:
    """Appends every inspection and event to daily files in data/."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._lock = threading.Lock()
        self._failed = False

    def _path(self, prefix: str, ext: str):
        return DATA_DIR / f"{prefix}_{datetime.now():%Y-%m-%d}.{ext}"

    def result(self, r: InspectionResult):
        if not self.enabled:
            return
        with self._lock:
            try:
                DATA_DIR.mkdir(exist_ok=True)
                path = self._path("inspections", "csv")
                if path.exists():                     # file from an older version (other columns)?
                    with path.open() as f:
                        if f.readline().strip() != ",".join(CSV_HEADER):
                            path = path.with_name(path.stem + "_v2.csv")
                new = not path.exists()
                with path.open("a", newline="") as f:
                    w = csv.writer(f)
                    if new:
                        w.writerow(CSV_HEADER)
                    w.writerow(result_row(r))
            except OSError as e:
                self._fail(e)

    def event(self, level: str, msg: str):
        if not self.enabled:
            return
        with self._lock:
            try:
                DATA_DIR.mkdir(exist_ok=True)
                with self._path("events", "log").open("a") as f:
                    f.write(f"{datetime.now():%H:%M:%S.%f}"[:-3] + f"  {level:<5}  {msg}\n")
            except OSError as e:
                self._fail(e)

    def _fail(self, e):
        if not self._failed:          # never crash the line over a disk problem
            print(f"[datalog] cannot write to {DATA_DIR}: {e}")
            self._failed = True


# =============================================================================
class RejectScheduler:
    """Fires the air for each failed can at its own scheduled time."""

    def __init__(self, rejector: Rejector, on_done):
        self.rejector = rejector
        self.on_done = on_done            # on_done(can_id, status, late_ms)
        self._heap: list = []
        self._cv = threading.Condition()
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="reject")
        self._thread.start()

    def schedule(self, fire_at: float, can_id: int, profile: tuple):
        with self._cv:
            heapq.heappush(self._heap, (fire_at, can_id, profile))
            self._cv.notify()

    def cancel_all(self) -> list[int]:
        with self._cv:
            ids = [can_id for _, can_id, _ in self._heap]
            self._heap.clear()
            self._cv.notify()
        return ids

    def stop(self):
        with self._cv:
            self._stop = True
            self._cv.notify()
        self._thread.join(timeout=1)

    def _run(self):
        while True:
            with self._cv:
                while not self._stop and not self._heap:
                    self._cv.wait()
                if self._stop:
                    return
                fire_at, can_id, profile = self._heap[0]
                delay = fire_at - time.monotonic()
                if delay > 0:
                    self._cv.wait(delay)          # wakes early if something new is scheduled
                    continue
                heapq.heappop(self._heap)
            late_ms = -delay * 1000
            if late_ms > LATE_TOLERANCE_MS:
                self.on_done(can_id, MISSED, late_ms)     # can is already past the nozzle
                continue
            ok = self.rejector.fire(*profile)
            self.on_done(can_id, FIRED if ok else LOCKED, late_ms)


# =============================================================================
class Controller:
    def __init__(self, io, settings: Settings, events: queue.Queue):
        self.io = io
        self.settings = settings
        self.events = events
        self.datalog = DataLogger(settings.log_to_disk)
        self.audit = Audit(self.post)
        self.alarms = AlarmLog(self.post)
        self.batches = BatchManager(self.log)
        self.recipes = RecipeBook(settings, self.log)

        self.camera = CameraLink(settings.camera_ip)          # network ping (GigE)
        self.cammgr = CameraManager(settings, self.log) if CameraManager else None
        self.camera_import_error = CAMERA_IMPORT_ERROR
        if self.cammgr:
            self.cammgr.on_ip_change = self._camera_ip_found
            self.recipes.load_reference(self.cammgr.vision)       # good-can reference of the recipe
        self.inspectors = build_inspectors(self.cammgr, settings, CAMERA_IMPORT_ERROR)
        self.inspectors[SimulatedInspector.name].defect_rate = settings.sim_defect_rate / 100
        if settings.inspector not in self.inspectors:
            settings.inspector = SimulatedInspector.name

        self.lights = StackLight(io)
        self.rejector = Rejector(io)
        self.scheduler = RejectScheduler(self.rejector, self._on_reject_done)
        self.sensor = SensorInput(io, self._on_sensor_edge, settings.sensor_backend, self.log)
        self.conveyor = Conveyor(settings, self.log, sim=io.sim)

        self.running = False
        self.estop = False
        self.tune_trigger = None             # capture-delay tuner: last trigger while it was open
        self.warning = False                 # yellow flashing: unusual defect activity (until acknowledged)
        self.warning_reason = ""
        self.manual_override = False
        self.started_at = time.time()

        # heat & power
        self.health = SystemHealth()
        self.cpu_temp = None
        self.cam_temp = None
        self.thermal = "ok"                  # ok | warm | hot | critical
        self.power: dict = {}
        self.disk_free_mb = None
        self.disk_total_mb = None
        self.standby = False
        self.selftest_running = False

        # sensor timing (Diagnostics): how long each can blocks the beam -> belt speed
        self.sensor_count = 0                # triggers since start (also while stopped)
        self._sensor_on_t = None
        self._sensor_last_t = None
        self._sensor_hist: deque = deque(maxlen=30)    # (time, block_ms, gap_ms or None)
        self._reset_at = -1e9
        self._idle_since = time.monotonic()  # line stopped since...

        self._lock = threading.Lock()
        self._can_counter = 0
        self._streak = 0                     # consecutive FAILs
        self._last_trigger = -1e9
        self._last_burst_end = 0.0
        self._ignored_logged = False
        self._defect_until = 0.0             # yellow lamp on after a defective can
        self._recent = deque(maxlen=max(10, settings.warn_window))   # True = defective
        self._latched: dict[str, str] = {}   # red until Reset (e.g. a defect escaped)
        self._fault_keys: dict = {}
        self._last_insp_error = ("", 0.0)
        self._insp_error_at = -1e9
        self._pending: dict[int, InspectionResult] = {}
        self._can_queue: queue.Queue = queue.Queue()
        self._shutdown = threading.Event()
        self._camera_wake = threading.Event()
        self._closed = False

        self._threads = [
            threading.Thread(target=self._inspection_worker, daemon=True, name="inspect"),
            threading.Thread(target=self._light_loop, daemon=True, name="lights"),
            threading.Thread(target=self._camera_loop, daemon=True, name="camera"),
            threading.Thread(target=self._health_loop, daemon=True, name="health"),
        ]
        for t in self._threads:
            t.start()

        self.log("INFO", "Controller started - " + ("SIMULATION mode" if io.sim else "HARDWARE mode (pinctrl)"))
        self.sensor.start()
        self.log("INFO", f"Sensor input: {self.sensor.mode}")
        if self.cammgr is not None:
            self.cammgr.start()
        else:
            self.log("WARN", f"Camera features disabled - {CAMERA_IMPORT_ERROR} "
                             f"(pip install opencv-python numpy pillow)")

    # ------------------------------------------------------------------ events
    def post(self, kind: str, payload=None):
        self.events.put((kind, payload))

    def log(self, level: str, msg: str):
        self.post("log", (level, msg, datetime.now()))
        self.datalog.event(level, msg)
        if level != "INFO" and getattr(self, "batches", None):
            self.batches.event(level, msg)

    def status(self) -> dict:
        return {
            "running": self.running, "estop": self.estop,
            "warning": self.warning, "warning_reason": self.warning_reason,
            "faults": [msg for _, msg in self.faults()],
            "manual": self.manual_override,
            "lights": self.lights.state, "valve": self.rejector.on, "buzzer": self.lights.buzzer.on,
            "rejector": self.settings.reject_enabled, "rejector_connected": self.rejector.connected,
            "locked": self.rejector.locked_out,
            "sensor": self.sensor.active, "sensor_mode": self.sensor.mode,
            "cam_ip": self.camera.ip, "cam_online": self.camera.online,
            "cam_ms": self.camera.latency_ms, "cam_ping": self.camera.can_ping,
            "cam_source": self.settings.camera_backend,
            "cam_ok": bool(self.cammgr and self.cammgr.ok),
            "cam_status": self.cammgr.status if self.cammgr else "not installed",
            "cam_fps": self.cammgr.fps if self.cammgr else 0.0,
            "queue": self._can_queue.qsize(), "sim": self.io.sim,
            "uptime": time.time() - self.started_at,
            "cpu_temp": self.cpu_temp, "cam_temp": self.cam_temp, "thermal": self.thermal,
            "standby": self.standby, "power": self.power, "is_pi": self.health.is_pi,
            "disk_free_mb": self.disk_free_mb, "disk_total_mb": self.disk_total_mb,
            "selftest": self.selftest_running, "alarms_version": self.alarms.version,
            "alarms_active": len([1 for k in self._fault_keys]) + (1 if self.warning else 0),
            "conveyor": self.conveyor.status(),
        }

    # -------------------------------------------------------- operator commands
    def start(self):
        if self.estop:
            self.log("WARN", "E-stop is active - press RESET first")
            return
        if self.running:
            return
        if self.thermal == "critical":
            self.log("WARN", f"Raspberry Pi too hot ({self.cpu_temp:.0f} °C) - let it cool before starting")
            return
        if "overheat" in self._latched:
            self.log("WARN", "The station overheated - check cooling, then press Reset before starting")
            return
        if self.manual_override:
            self.lights.buzzer.set(False, force=True)
        self.manual_override = False
        self.running = True
        self.wake_camera(quiet=True)
        self._ignored_logged = False
        self.log("INFO", f"Line STARTED - inspector: {self.settings.inspector}")
        if self.settings.conveyor_with_line:
            self.conveyor.run(1)

    def stop(self):
        if self.running:
            self.running = False
            self._idle_since = time.monotonic()
            self.log("INFO", "Line STOPPED")
        if self.settings.conveyor_with_line:
            self.conveyor.stop()

    def emergency_stop(self):
        if self.estop:
            return
        self.estop = True
        self.running = False
        self._idle_since = time.monotonic()
        self.manual_override = False
        self.lights.buzzer.set(False, force=True)
        self.rejector.locked_out = True
        self.rejector.safe()
        try:
            self.conveyor.halt()
        except Exception as e:
            self.log("ERROR", f"E-stop: motors did not stop cleanly ({e}) - switch the motor HAT off")
        for can_id in self.scheduler.cancel_all():
            self._on_reject_done(can_id, LOCKED, 0.0)
        self.log("ALARM", "E-STOP - inspection halted, reject valve locked out")

    def reset(self):
        """Clears E-stop, latched faults and warnings, and re-arms the rejector."""
        self.estop = False
        self._latched.clear()
        self._reset_at = time.monotonic()
        if self.warning:
            self.alarms.cleared("defect-warning", "Reset")
        self.warning = False
        self.warning_reason = ""
        self._streak = 0
        self._recent.clear()
        self.rejector.locked_out = False
        self.rejector.safe()
        self.log("INFO", "Reset - system re-armed")

    def acknowledge(self):
        """Operator has seen the defect warning (yellow flashing)."""
        if self.warning:
            self.warning = False
            self.warning_reason = ""
            self._streak = 0
            self._recent.clear()
            self.alarms.cleared("defect-warning", "acknowledged")
            self.log("INFO", "Defect warning acknowledged")

    ack_alarm = acknowledge

    def set_inspector(self, name: str):
        if name in self.inspectors and name != self.settings.inspector:
            self.settings.inspector = name
            self.log("INFO", f"Inspector: {name}")

    def set_defect_rate(self, percent: int):
        self.settings.sim_defect_rate = percent
        self.inspectors[SimulatedInspector.name].defect_rate = percent / 100
        if self.cammgr:
            self.cammgr.apply_settings()          # synthetic camera uses the same rate

    def set_setting(self, key: str, value):
        setattr(self.settings, key, value)
        if key == "log_to_disk":
            self.datalog.enabled = value

    def save_settings(self, announce: bool = False):
        try:
            self.settings.save()
            if announce:
                self.log("INFO", "Settings saved to config.json")
            return True
        except OSError as e:
            self.log("ERROR", f"Could not save settings: {e}")
            return False

    def set_camera_ip(self, ip: str) -> bool:
        ip = ip.strip()
        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            self.log("WARN", f"'{ip}' is not a valid IPv4 address")
            return False
        self.settings.camera_ip = ip
        self.camera.ip = ip
        self.camera.online = None
        self.log("INFO", f"Camera IP set to {ip} - checking connection")
        self._camera_wake.set()
        if self.cammgr and self.settings.camera_backend == "gige":
            self.cammgr.reopen()
        return True

    # ------------------------------------------------------------- recipes
    def select_recipe(self, name: str):
        if name == self.recipes.active:
            return
        if self.running:
            raise ValueError("stop the line before changing the recipe")
        if self.batches.active:
            raise ValueError("end the current batch before changing the recipe")
        self.recipes.activate(name, self.cammgr.vision if self.cammgr else None)
        if self.cammgr:
            self.cammgr.apply_settings()
        self._apply_camera_load()
        self.save_settings()
        self.log("INFO", f"Recipe selected: {name}")

    def save_recipe(self):
        self.recipes.save_current()
        self.log("INFO", f"Recipe '{self.recipes.active}' saved")

    def create_recipe(self, name: str, description: str = ""):
        self.recipes.create(name, description, copy_reference_from=self.recipes.active)
        self.log("INFO", f"Recipe '{name.strip()}' created from the current settings")

    def delete_recipe(self, name: str):
        self.recipes.delete(name)
        self.log("INFO", f"Recipe '{name}' deleted")

    def teach_reference(self) -> bool:
        if self.cammgr is None or not self.cammgr.teach():
            return False
        self.recipes.set_reference(self.cammgr.vision)
        return True

    def clear_reference(self):
        if self.cammgr is not None:
            self.cammgr.clear_reference()
        self.recipes.clear_reference()

    # ------------------------------------------------------------- batches
    def start_batch(self, lot: str, operator: str, note: str = "") -> dict:
        s = self.settings
        snapshot = {f: getattr(s, f) for f in RECIPE_FIELDS}
        snapshot.update(inspector=s.inspector, camera_backend=s.camera_backend,
                        reject_enabled=s.reject_enabled)
        b = self.batches.start(lot, operator, self.recipes.active, snapshot, note)
        self.log("INFO", f"Batch started: lot {b['lot']} ({self.recipes.active}, operator {b['operator']})")
        return b

    def end_batch(self) -> dict:
        b = self.batches.end()
        self.log("INFO", f"Batch ended: lot {b['lot']} - {b['n']} cans, {b['failed']} rejected")
        return b

    def _camera_ip_found(self, ip: str):
        self.camera.ip = ip
        self.camera.online = None
        self._camera_wake.set()
        self.save_settings()

    def check_camera(self):
        self._camera_wake.set()

    def set_manual_override(self, on: bool) -> bool:
        if on and (self.running or self.estop):
            self.log("WARN", "Manual override only while stopped and not in E-stop")
            return False
        self.manual_override = on
        self.lights.all_off()                       # entering or leaving: start from all off (buzzer too)
        self.log("INFO", f"Manual output override {'ON' if on else 'OFF'}")
        return True

    def manual_toggle(self, key: str):
        if not self.manual_override:
            return
        if key == "buzzer":
            self.lights.buzzer.set(not self.lights.buzzer.on)
        else:
            self.lights.set(key, not self.lights.state[key])

    def beep(self, seconds: float = 0.6):
        """Buzzer test: one short beep (relay 4)."""
        def run():
            self.lights.buzzer.set(True)
            time.sleep(seconds)
            self.lights.buzzer.set(False)
        self.log("INFO", "Buzzer test")
        threading.Thread(target=run, daemon=True).start()

    def test_burst(self):
        if not self.rejector.connected:
            self.log("WARN", "Rejector not connected - set REJECT_PIN in config.py when it is wired")
            return
        if self.running or self.estop:
            self.log("WARN", "Test burst only while stopped and not in E-stop")
            return
        s = self.settings
        profile = (s.pulses, s.pulse_on_ms / 1000, s.pulse_off_ms / 1000)

        def run():
            ok = self.rejector.fire(*profile)
            self.log("INFO" if ok else "WARN",
                     f"Test burst fired ({s.pulses} x {s.pulse_on_ms} ms)" if ok
                     else "Test burst blocked - rejector locked out (press RESET)")
        threading.Thread(target=run, daemon=True).start()

    def simulate_can(self):
        self.sensor.inject_pulse()

    # ------------------------------------------------------------- can flow
    def _on_sensor_edge(self, active: bool, t: float, initial: bool):
        self.post("sensor", active)
        if active and not initial and self.cammgr and time.monotonic() < self.cammgr.tune_until \
                and (self.tune_trigger is None or t - self.tune_trigger > 1.5):
            self.tune_trigger = t                    # capture-delay tuner: this can's trigger time
        if not initial:
            self._time_sensor(active, t)
        if initial or not active:
            return
        if (t - self._last_trigger) * 1000 < self.settings.min_can_gap_ms:
            return                                  # bounce / same can
        self._last_trigger = t
        if not self.running or self.estop:
            if not self._ignored_logged:
                self.log("INFO", "Can detected while the line is stopped - ignored")
                self._ignored_logged = True
            return
        with self._lock:
            self._can_counter += 1
            can_id = self._can_counter
        if self.cammgr:
            self.cammgr.on_trigger()              # synthetic / folder sources show the next can
        self._can_queue.put((can_id, t))

    def _time_sensor(self, active: bool, t: float):
        """Beam blocked time per can -> belt speed (Diagnostics > Timing)."""
        if active:
            if self._sensor_last_t is not None and (t - self._sensor_last_t) * 1000 < self.settings.min_can_gap_ms:
                self._sensor_on_t = self._sensor_last_t     # bounce: still the same can
                return
            gap = (t - self._sensor_last_t) * 1000 if self._sensor_last_t is not None else None
            self._sensor_on_t, self._sensor_last_t = t, t
            self._sensor_gap = gap
            self.sensor_count += 1
        elif self._sensor_on_t is not None:
            block = (t - self._sensor_on_t) * 1000
            if 2 <= block <= 5000:                      # ignore glitches and a hand left in the beam
                self._sensor_hist.append((time.time(), block, getattr(self, "_sensor_gap", None)))
            self._sensor_on_t = None

    def belt_stats(self) -> dict:
        """Belt speed from how long cans block the sensor (speed = can diameter / blocked time)."""
        s = self.settings
        hist = [h for h in self._sensor_hist if time.time() - h[0] < 600][-10:]
        out = {"count": self.sensor_count, "n": len(hist), "active": self.sensor.active,
               "last_block_ms": hist[-1][1] if hist else None,
               "last_age_s": time.time() - hist[-1][0] if hist else None}
        gaps = [h[2] for h in hist if h[2] is not None]
        out["min_gap_ms"] = min(gaps) if gaps else None
        if not hist:
            return out
        speeds = [s.can_diameter_mm / (b / 1000) for _, b, _ in hist]
        v = sum(speeds) / len(speeds)
        out.update(speed_mm_s=v, speed_min=min(speeds), speed_max=max(speeds),
                   spread_pct=(max(speeds) - min(speeds)) / v * 100 if v else 0,
                   block_ms=sum(b for _, b, _ in hist) / len(hist))
        r_ms = s.can_diameter_mm / 2 / v * 1000            # leading edge -> can centre at the beam
        if s.camera_distance_mm > 0:
            out["rec_camera_delay_ms"] = round(s.camera_distance_mm / v * 1000 + r_ms)
        if s.nozzle_distance_mm > 0:                       # centre the air burst on the can
            out["rec_reject_delay_ms"] = max(0, round(s.nozzle_distance_mm / v * 1000 + r_ms - s.burst_ms / 2))
        return out

    def self_test(self):
        """Commissioning check: each lamp for 1 s, then one air burst. Line must be stopped."""
        if self.running or self.estop:
            raise ValueError("stop the line (and clear E-stop) before the self-test")
        if self.selftest_running:
            raise ValueError("self-test already running")
        self.selftest_running = True

        def run():
            was = self.manual_override
            try:
                self.manual_override = True
                self.log("INFO", "Self-test: green, yellow, red lamp, buzzer"
                         + (", then one reject burst" if self.rejector.connected else ""))
                for key in ("green", "yellow", "red"):
                    self.lights.all_off()
                    self.lights.set(key, True)
                    time.sleep(1.0)
                self.lights.all_off()
                self.lights.buzzer.set(True)
                time.sleep(0.5)
                self.lights.all_off()
                time.sleep(0.3)
                ok = True
                if self.rejector.connected:
                    s = self.settings
                    ok = self.rejector.fire(s.pulses, s.pulse_on_ms / 1000, s.pulse_off_ms / 1000)
                self.log("INFO" if ok else "WARN", "Self-test finished" if ok
                         else "Self-test: reject burst blocked - rejector locked out (press Reset)")
            finally:
                self.manual_override = was
                self.selftest_running = False
        threading.Thread(target=run, daemon=True, name="selftest").start()

    def _inspection_worker(self):
        while not self._shutdown.is_set():
            try:
                can_id, t_detect = self._can_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._inspect(can_id, t_detect)
            except Exception as e:                   # never let the worker die
                self.log("ERROR", f"Can #{can_id}: internal error {e!r}")

    def _inspect(self, can_id: int, t_detect: float):
        s = self.settings
        name = s.inspector
        t0 = time.monotonic()
        try:
            outcome = self.inspectors[name].inspect(can_id, t_detect)
        except Exception as e:
            outcome = Outcome(False, "Inspection error", 0.0)
            self._log_inspector_error(can_id, e)
        inspect_ms = (time.monotonic() - t0) * 1000

        result = InspectionResult(can_id, datetime.now(), outcome.passed,
                                  outcome.defect or "", outcome.confidence, inspect_ms, name,
                                  image=outcome.image, batch=self.batches.active_id)
        if outcome.passed:
            self._streak = 0
        else:
            self._streak += 1
            self._defect_until = time.monotonic() + DEFECT_LAMP_S
            if not s.reject_enabled or not self.rejector.connected:
                result.reject = REJ_OFF               # pass / fail only
            elif self.estop or self.rejector.locked_out:
                result.reject = LOCKED
            else:
                result.reject = PENDING
        self._check_defect_trend(not outcome.passed)

        if result.reject == PENDING:
            self._pending[can_id] = result
        self.post("result", dataclasses.replace(result))      # UI gets its own copy

        if result.reject == PENDING:
            fire_at = t_detect + s.reject_delay_ms / 1000
            burst = s.burst_ms / 1000
            if fire_at < self._last_burst_end:
                self.log("WARN", f"Can #{can_id}: arrives during the previous air burst - "
                                 f"cans closer than {s.burst_ms} ms")
            self._last_burst_end = max(fire_at, self._last_burst_end) + burst
            self.scheduler.schedule(fire_at, can_id,
                                    (s.pulses, s.pulse_on_ms / 1000, s.pulse_off_ms / 1000))
        else:
            self._record(result)
            if result.escaped:
                self.log("ERROR", f"Can #{can_id}: FAIL but NOT rejected (reject {result.reject})")
                self._latch_escape(can_id, result.reject)

    def _record(self, result: InspectionResult):
        """A can is finished (reject status final): daily CSV + batch totals."""
        self.datalog.result(result)
        self.batches.add_result(result)

    def _check_defect_trend(self, defective: bool):
        """Yellow flashing warning: too many defects in a row, or a high defect rate."""
        self._recent.append(defective)
        if self.warning:
            return
        s = self.settings
        reason = ""
        if self._streak >= s.alarm_after:
            reason = f"{self._streak} defects in a row"
        elif len(self._recent) >= min(20, self._recent.maxlen):
            rate = 100 * sum(self._recent) / len(self._recent)
            if rate >= s.warn_rate_pct:
                reason = f"{rate:.0f}% defects in the last {len(self._recent)} cans"
        if reason:
            self.warning = True
            self.warning_reason = reason
            self.alarms.raised("defect-warning", reason)
            self.log("WARN", f"Unusual defect activity: {reason} - check the upstream process")

    def _latch_escape(self, can_id: int, why: str):
        if not self.estop:
            self._latched["escape"] = f"Defective can #{can_id} was not rejected ({why}) - press Reset"

    def _log_inspector_error(self, can_id: int, e: Exception):
        msg = str(e) or e.__class__.__name__
        self._insp_error_at = time.monotonic()
        last_msg, last_t = self._last_insp_error
        if msg != last_msg or time.monotonic() - last_t > 10:     # don't flood the log
            self.log("ERROR", f"Can #{can_id}: inspector failed ({msg}) - rejecting as fail-safe")
            self._last_insp_error = (msg, time.monotonic())

    def _on_reject_done(self, can_id: int, status: str, late_ms: float):
        result = self._pending.pop(can_id, None)
        if result is not None:
            result.reject = status
            self._record(result)
        self.post("reject", (can_id, status))
        if status == FIRED and self.settings.single_shot and not self.rejector.locked_out:
            self.rejector.locked_out = True
            self.log("WARN", "Single-shot latch: rejector locked out - press RESET to re-arm")
        elif status == MISSED:
            self.log("ERROR", f"Can #{can_id}: reject MISSED ({late_ms:.0f} ms late) - "
                              f"increase reject delay or speed up inspection")
            self._latch_escape(can_id, "air too late")
        elif status == LOCKED:
            self.log("ERROR", f"Can #{can_id}: FAIL but NOT rejected (rejector locked out)")
            self._latch_escape(can_id, "rejector locked")

    # ------------------------------------------------------- background loops
    def faults(self) -> list:
        """
        Everything that turns the red lamp on, as (key, message).
        Live faults clear themselves once fixed; latched ones (E-stop, a defective
        can that escaped) stay until Reset.
        """
        f = []
        s = self.settings
        if self.estop:
            f.append(("estop", "E-stop pressed - press Reset"))
        f += list(self._latched.items())
        if self.sensor.error:
            f.append(("sensor", f"Trigger sensor: {self.sensor.error}"))
        camera_fault = False
        if s.inspector == "Camera":
            if self.cammgr is None:
                f.append(("camera", "Camera software not installed"))
                camera_fault = True
            elif not self.cammgr.ok and not self.cammgr.status.startswith(("Starting", "Opening")):
                f.append(("camera", f"Camera: {self.cammgr.status}"))
                camera_fault = True
        if not camera_fault and time.monotonic() - self._insp_error_at < 5:
            f.append(("inspect", f"Inspection failing: {self._last_insp_error[0]}"))
        if self.thermal in ("hot", "critical") and self.cpu_temp is not None:
            f.append(("cpu-hot", f"Raspberry Pi hot ({self.cpu_temp:.0f} °C) - load reduced, check the fan"))
        if self.power.get("undervoltage_now"):
            f.append(("power", "Under-voltage - use the official 5V 5A (27 W) power supply"))
        if self.cam_temp is not None and self.cam_temp >= CAM_MAX_C:
            f.append(("cam-hot", f"Camera hot ({self.cam_temp:.0f} °C) - check mounting and airflow"))
        if self.rejector.locked_out and not self.estop:
            f.append(("locked", "Reject valve locked out - press Reset"))
        if self.disk_free_mb is not None and self.disk_free_mb < DISK_MIN_MB:
            f.append(("disk", f"Storage almost full ({self.disk_free_mb:.0f} MB free) - images may stop saving"))
        return f

    def _desired_lights(self, faults) -> dict:
        """One lamp at a time, by priority: red > yellow > green."""
        now = time.monotonic()
        if faults:
            return {"green": False, "yellow": False, "red": True}           # solid red: fault
        if self.warning:
            return {"green": False, "yellow": int(now * 2) % 2 == 0, "red": False}   # flashing yellow
        if now < self._defect_until:
            return {"green": False, "yellow": True, "red": False}           # defect just found
        if self.running:
            return {"green": True, "yellow": False, "red": False}           # all good, inspecting
        return {"green": False, "yellow": False, "red": False}              # stopped: all off

    def _light_loop(self):
        while not self._shutdown.is_set():
            faults = self.faults()
            now_faults = dict(faults)
            for k, msg in faults:
                if k not in self._fault_keys:
                    self.log("ERROR", f"FAULT: {msg}")
                    self.alarms.raised(k, msg)
            for k, msg in self._fault_keys.items():
                if k not in now_faults:
                    self.log("INFO", f"Fault cleared: {msg}")
                    self.alarms.cleared(k, "Reset" if time.monotonic() - self._reset_at < 1 else "cleared itself")
            self._fault_keys = now_faults
            if not self.manual_override and not self.selftest_running:
                self.lights.apply(self._desired_lights(faults))
            time.sleep(0.05)

    # ---------------------------------------------------------- heat & power
    def wake_camera(self, quiet: bool = False):
        self._idle_since = time.monotonic()
        if self.standby:
            self.standby = False
            self._apply_camera_load()
            if not quiet:
                self.log("INFO", "Camera woken from standby")

    def _thermal_level(self, t):
        order = ["ok", "warm", "hot", "critical"]
        limits = {"warm": CPU_WARM_C, "hot": CPU_HOT_C, "critical": CPU_CRITICAL_C}
        if t is None:
            return "ok"
        new = "ok"
        for name in ("warm", "hot", "critical"):
            if t >= limits[name]:
                new = name
        cur = self.thermal
        if order.index(new) < order.index(cur) and t > limits[cur] - TEMP_HYSTERESIS_C:
            return cur                                  # hysteresis: cool down properly first
        return new

    def _apply_camera_load(self):
        """Live view / camera frame rate according to standby and temperature."""
        m = self.cammgr
        if m is None:
            return
        s = self.settings
        if self.standby:
            m.live_mode, m.fps_limit = "off", STANDBY_FPS
            return
        m.live_mode = {"ok": "normal", "warm": "reduced", "hot": "raw", "critical": "raw"}[self.thermal]
        fps = s.camera_fps
        if self.thermal == "hot":
            fps = min(fps, 10)
        elif self.thermal == "critical":
            fps = min(fps, 5)
        m.fps_limit = fps

    def _health_loop(self):
        last_summary = time.monotonic()
        last_tick = time.monotonic()
        last_disk = -1e9
        last_cleanup = time.monotonic() - 3600 + 60     # first clean-up a minute after start
        seen_logged = False
        cam_level = "ok"
        while not self._shutdown.is_set():
            h = self.health
            t = h.cpu_temp()
            self.cpu_temp = t
            self.power = SystemHealth.describe(h.read_flags()) if h.is_pi else {}
            if self.power.get("undervoltage_seen") and not seen_logged:
                self.log("WARN", "Under-voltage has occurred since boot - the power supply is too weak "
                                 "(Pi 5 needs 5V 5A / 27 W)")
                seen_logged = True

            level = self._thermal_level(t)
            if level != self.thermal:
                msg = {"ok": ("INFO", f"Raspberry Pi temperature normal again ({t:.0f} °C)"),
                       "warm": ("WARN", f"Raspberry Pi warm ({t:.0f} °C) - live view slowed down"),
                       "hot": ("ERROR", f"Raspberry Pi hot ({t:.0f} °C) - live detection off, camera "
                                        f"frame rate reduced. Check the fan / airflow"),
                       "critical": ("ALARM", f"Raspberry Pi critical ({t:.0f} °C) - stopping inspection")}[level]
                self.log(*msg)
                self.thermal = level
            if level == "critical" and self.running:
                self.running = False
                self._idle_since = time.monotonic()
                self._latched["overheat"] = (f"Overheated ({t:.0f} °C) - inspection stopped. "
                                             f"Let it cool, then Reset")

            s = self.settings
            idle = (not self.running and s.standby_after_min > 0
                    and time.monotonic() - self._idle_since >= s.standby_after_min * 60)
            if idle != self.standby:
                self.standby = idle
                if idle:
                    self.log("INFO", f"Line stopped for {s.standby_after_min} min - camera in standby "
                                     f"({STANDBY_FPS} fps, live view off). Start or Wake to resume")
            self._apply_camera_load()

            if self.cammgr is not None:
                ct = self.cammgr.cam_temp
                self.cam_temp = ct
                new = "ok" if ct is None or ct < CAM_WARN_C else "warm" if ct < CAM_MAX_C else "hot"
                if new != cam_level:
                    if new == "warm":
                        self.log("WARN", f"Camera warm ({ct:.0f} °C)")
                    elif new == "ok" and ct is not None:
                        self.log("INFO", f"Camera temperature normal ({ct:.0f} °C)")
                    cam_level = new

            if time.monotonic() - last_summary > 300 and (t is not None or self.cam_temp is not None):
                last_summary = time.monotonic()
                parts = []
                if t is not None:
                    parts.append(f"Pi {t:.0f} °C")
                if self.cam_temp is not None:
                    parts.append(f"camera {self.cam_temp:.0f} °C")
                self.log("INFO", "Health: " + ", ".join(parts))
            if time.monotonic() - last_disk > 30:
                last_disk = time.monotonic()
                try:
                    du = shutil.disk_usage(DATA_DIR if DATA_DIR.exists() else DATA_DIR.parent)
                    self.disk_free_mb, self.disk_total_mb = du.free / 2**20, du.total / 2**20
                except OSError:
                    pass
            if time.monotonic() - last_cleanup > 3600:       # hourly
                last_cleanup = time.monotonic()
                try:
                    cleanup_images(self.settings.keep_images_days, self.log)
                except Exception as e:
                    self.log("WARN", f"Image clean-up failed: {e}")
            now = time.monotonic()
            self.batches.tick(self.running, now - last_tick, t, self.cam_temp)
            last_tick = now
            self._shutdown.wait(2)

    def _camera_loop(self):
        last = "start"
        while not self._shutdown.is_set():
            if self.settings.camera_backend != "gige":     # ping only matters for the GigE camera
                self.camera.online = self.camera.latency_ms = None
                last = "start"
                self._camera_wake.wait(CAMERA_CHECK_INTERVAL_S)
                self._camera_wake.clear()
                continue
            self.camera.check()
            state = self.camera.online
            if state != last:
                if state is True:
                    self.log("INFO", f"Camera {self.camera.ip} online")
                elif state is False:
                    self.log("WARN", f"Camera {self.camera.ip} not reachable - check IP, cable, Pi static IP")
                last = state
            self._camera_wake.wait(CAMERA_CHECK_INTERVAL_S)
            self._camera_wake.clear()

    # ---------------------------------------------------------------- shutdown
    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self.running = False
        self._shutdown.set()
        self._camera_wake.set()
        self.scheduler.stop()
        self.sensor.stop()
        try:                                 # a motor error must never block the valve / relay shutdown
            self.conveyor.close()
        except Exception as e:
            print(f"Motors: {e}")
        if self.cammgr:
            self.cammgr.stop()
        for t in self._threads:
            t.join(timeout=0.5)
        self.rejector.locked_out = True
        self.rejector.safe()
        self.lights.all_off()
        self.batches.flush()
        self.save_settings()
        print("Shutdown complete: reject valve closed, all relays off, motors stopped, settings saved.")
