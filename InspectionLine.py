#!/usr/bin/env python3
"""
Yards Can Inspection - Integrated Line Controller
=================================================
Combines the three test scripts into one application:

  * StackLight.py -> stack light outputs (4-channel active-low relay board)
  * GPIOTest.py   -> sensor monitoring through `pinctrl get`
  * AirTest.py    -> pneumatic reject burst with fail-safe de-energize

...plus an inspection step (pluggable: simulated now, Ethernet camera later)
and a Tkinter dashboard for control, live status, results, stats and logs.

Wiring (physical header pins):
    2  5V  -> sensor          4  5V  -> relay board     6  GND -> relay board
    9  GND -> sensor         11  GPIO17 -> relay 1 (green)
   13  GPIO27 -> relay 2 (yellow)                      15  GPIO22 -> relay 3 (red)
   16  GPIO23 -> relay 4 (reject solenoid)            18  GPIO24 <- sensor signal
    The sensor outputs 5V, so its signal goes through a resistor voltage
    divider down to 3.3V before GPIO24 (the Pi's GPIO is not 5V tolerant).
    The camera is connected to the Pi over Ethernet (see CAMERA_IP).

Flow in AUTO (running) mode:
    sensor edge -> new can -> inspect -> FAIL? -> wait reject delay -> air burst
                                         PASS? -> continue
    stack light: green = running, yellow = stopped, red flash = reject,
                 blinking red = alarm (N rejects in a row), solid red = E-stop

Runs on a Raspberry Pi 5 (uses the `pinctrl` CLI). If `pinctrl` is not found
(e.g. on a laptop) it starts in SIMULATION mode, so the UI can be developed
and demoed without hardware - use the "Simulate can" button to feed cans.

NOTE: The Pi does not control the conveyor. STOP / E-STOP only stop this
station's inspection + reject outputs.
"""

import csv
import ipaddress
import queue
import random
import shutil
import subprocess
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# =============================================================================
# HARDWARE CONFIGURATION - edit to match your wiring
# =============================================================================
SENSOR_PIN = 24            # physical pin 18 - trigger sensor (5V signal via resistor divider -> 3.3V)
SENSOR_ACTIVE_LOW = True   # AirTest.py treats "lo" as "can present"
SENSOR_PULL = "pn"         # no internal pull - the divider sets the level

# Reject solenoid is switched by channel 4 of the same active-low relay board
# as the stack light, so it is active-low too: 'dl' = AIR ON, 'dh' = AIR OFF.
REJECT_PIN = 23            # physical pin 16 -> relay 4
REJECT_ACTIVE_LOW = True

# Stack light relays 1-3. Active-low like StackLight.py: 'dl' = ON, 'dh' = OFF.
# (No buzzer - its relay channel is now used by the rejector.)
LIGHT_OUTPUTS = [
    # key,     label,     GPIO, color
    ("green",  "GREEN",   17,   "#2ecc71"),   # physical pin 11, relay 1
    ("yellow", "YELLOW",  27,   "#f39c12"),   # physical pin 13, relay 2
    ("red",    "RED",     22,   "#e74c3c"),   # physical pin 15, relay 3
]
LIGHTS_ACTIVE_LOW = True

# Camera over Ethernet. The Pi's Ethernet port must have a static IP on the
# same subnet as the camera (e.g. camera 192.168.0.10, Pi 192.168.0.100/24).
CAMERA_IP = "192.168.0.10"     # <-- set to your camera's actual IP
CAMERA_CHECK_INTERVAL_S = 5    # how often to check the camera is reachable

POLL_INTERVAL_S = 0.005    # sensor polling period (same as AirTest.py)
MIN_CAN_GAP_S = 0.05       # debounce: ignore re-triggers closer than this
RED_FLASH_S = 0.6          # how long the red light flashes per reject

DEFAULT_SETTINGS = {
    "reject_enabled": True,
    "single_shot": False,      # AirTest.py-style latch: lock out after one fire
    "reject_delay_ms": 150,    # sensor -> air jet travel time
    "pulses": 3,               # AirTest.py burst profile
    "on_ms": 40,
    "off_ms": 30,
    "alarm_after": 3,          # consecutive rejects before blinking-red alarm
}

DEFECT_TYPES = ["Dent", "Scratch", "Flange damage", "Contamination",
                "Crease / wrinkle", "Coating void"]


def check_pin_conflicts():
    pins = [SENSOR_PIN, REJECT_PIN] + [p for _, _, p, _ in LIGHT_OUTPUTS]
    dup = sorted({p for p in pins if pins.count(p) > 1})
    if dup:
        raise SystemExit(f"GPIO conflict: pin(s) {dup} assigned more than once. "
                         f"Fix the HARDWARE CONFIGURATION block.")


# =============================================================================
# GPIO LAYER (pinctrl wrapper, with simulation fallback)
# =============================================================================
class GPIO:
    def __init__(self):
        self.sim = shutil.which("pinctrl") is None

    def _set(self, *args):
        if self.sim:
            return
        subprocess.run(["pinctrl", "set", *map(str, args)], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def setup_input(self, pin, pull="pn"):
        self._set(pin, "ip", pull)

    def write(self, pin, high):
        self._set(pin, "op", "dh" if high else "dl")

    def read(self, pin):
        """Returns True (hi), False (lo) or None (unknown)."""
        if self.sim:
            return SENSOR_ACTIVE_LOW  # idle level = "no can"
        out = subprocess.check_output(["pinctrl", "get", str(pin)], text=True)
        if "| hi" in out:
            return True
        if "| lo" in out:
            return False
        return None


class StackLight:
    def __init__(self, gpio):
        self.gpio = gpio
        self.pins = {k: p for k, _, p, _ in LIGHT_OUTPUTS}
        self.state = {k: False for k in self.pins}
        self._lock = threading.Lock()
        self.all_off()

    def set(self, key, on, force=False):
        with self._lock:
            if self.state[key] == on and not force:
                return
            level = (not on) if LIGHTS_ACTIVE_LOW else on
            self.gpio.write(self.pins[key], level)
            self.state[key] = on

    def all_off(self):
        for k in self.pins:
            self.set(k, False, force=True)


class Rejector:
    """Pneumatic reject solenoid on relay channel 4. Always ends de-energized."""

    def __init__(self, gpio, pin, active_low=REJECT_ACTIVE_LOW):
        self.gpio = gpio
        self.pin = pin
        self.active_low = active_low
        self.locked_out = False
        self.on = False
        self._lock = threading.Lock()
        self.safe()

    def _set(self, on):
        level = (not on) if self.active_low else on
        self.gpio.write(self.pin, level)
        self.on = on

    def safe(self):
        self._set(False)   # active-low: drives the pin HIGH = relay open = no air

    def fire(self, pulses, on_s, off_s):
        if self.locked_out:
            return False
        with self._lock:
            try:
                for _ in range(pulses):
                    if self.locked_out:          # E-stop mid-burst
                        break
                    self._set(True)
                    time.sleep(on_s)
                    self._set(False)
                    time.sleep(off_s)
            finally:
                self.safe()
        return True


class CameraLink:
    """Checks that the Ethernet camera answers at its IP address (ping)."""

    def __init__(self, ip):
        self.ip = ip
        self.online = None        # None = not checked yet
        self.latency_ms = None
        self._ping = shutil.which("ping")

    def check(self):
        if not self._ping:
            self.online, self.latency_ms = None, None
            return
        t0 = time.monotonic()
        try:
            r = subprocess.run([self._ping, "-c", "1", "-W", "1", self.ip],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            ok = r.returncode == 0
        except subprocess.TimeoutExpired:
            ok = False
        self.online = ok
        self.latency_ms = (time.monotonic() - t0) * 1000 if ok else None


# =============================================================================
# INSPECTORS - swap SimulatedInspector for the real camera when ready
# =============================================================================
class Inspector:
    name = "base"

    def inspect(self):
        """Return (passed: bool, defect: str, confidence: float 0-1)."""
        raise NotImplementedError


class SimulatedInspector(Inspector):
    name = "Simulated"

    def __init__(self):
        self.defect_rate = 0.10

    def inspect(self):
        time.sleep(random.uniform(0.01, 0.04))          # fake capture + inference
        if random.random() < self.defect_rate:
            return False, random.choice(DEFECT_TYPES), random.uniform(0.75, 0.99)
        return True, "-", random.uniform(0.90, 0.999)


class AlwaysPassInspector(Inspector):
    name = "Always pass"

    def inspect(self):
        return True, "-", 1.0


class AlwaysRejectInspector(Inspector):
    name = "Always reject (air test)"

    def inspect(self):
        return False, "Forced reject", 1.0


class CameraInspector(Inspector):
    """Hook for the Ethernet camera (KEYENCE / Teledyne) + defect model."""
    name = "Camera (Ethernet)"

    def __init__(self, link):
        self.link = link

    def inspect(self):
        if self.link.online is False:
            raise RuntimeError(f"camera at {self.link.ip} not reachable - check IP / cable")
        # TODO:
        #   1. trigger the camera at self.link.ip and grab a frame
        #   2. run the defect model on the frame
        #   3. return (passed, defect_label, confidence)
        raise NotImplementedError("camera capture + model not implemented yet")


@dataclass
class InspectionResult:
    can_id: int
    timestamp: datetime
    passed: bool
    defect: str
    confidence: float
    inspect_ms: float
    rejected: bool       # air actually fired


# =============================================================================
# CONTROLLER - all hardware logic, runs on background threads
# =============================================================================
class Controller:
    def __init__(self, gpio, events):
        self.gpio = gpio
        self.events = events
        self.settings = dict(DEFAULT_SETTINGS)
        self.camera = CameraLink(CAMERA_IP)
        self._camera_wake = threading.Event()
        self.inspectors = {i.name: i for i in (SimulatedInspector(), AlwaysPassInspector(),
                                               AlwaysRejectInspector(), CameraInspector(self.camera))}
        self.inspector_name = SimulatedInspector.name

        self.lights = StackLight(gpio)
        self.rejector = Rejector(gpio, REJECT_PIN)
        gpio.setup_input(SENSOR_PIN, SENSOR_PULL)

        self.running = False
        self.estop = False
        self.alarm = False
        self.manual_override = False
        self.sensor_active = False

        self._lock = threading.Lock()
        self._can_counter = 0
        self._consecutive_rejects = 0
        self._red_flash_until = 0.0
        self._shutdown = threading.Event()
        self._closed = False

        self._threads = [
            threading.Thread(target=self._sensor_loop, daemon=True, name="sensor"),
            threading.Thread(target=self._light_loop, daemon=True, name="lights"),
            threading.Thread(target=self._camera_loop, daemon=True, name="camera"),
        ]
        for t in self._threads:
            t.start()

        self.log("INFO", "Controller started in "
                 + ("SIMULATION mode (pinctrl not found)" if gpio.sim else "HARDWARE mode"))

    # ---- messaging to UI -----------------------------------------------------
    def post(self, kind, payload=None):
        self.events.put((kind, payload))

    def log(self, level, msg):
        self.post("log", (level, msg))

    # ---- operator commands ---------------------------------------------------
    def start(self):
        if self.estop:
            self.log("WARN", "E-stop is active - press RESET first")
            return
        self.manual_override = False
        self.running = True
        self.log("INFO", "Line STARTED - inspecting")

    def stop(self):
        if self.running:
            self.running = False
            self.log("INFO", "Line STOPPED")

    def emergency_stop(self):
        self.estop = True
        self.running = False
        self.manual_override = False
        self.rejector.locked_out = True
        self.rejector.safe()
        self.log("ALARM", "E-STOP - inspection halted, reject solenoid locked out")

    def reset(self):
        self.estop = False
        self.alarm = False
        with self._lock:
            self._consecutive_rejects = 0
        self.rejector.locked_out = False
        self.rejector.safe()
        self.log("INFO", "Reset - system re-armed")

    def ack_alarm(self):
        if self.alarm:
            self.alarm = False
            with self._lock:
                self._consecutive_rejects = 0
            self.log("INFO", "Alarm acknowledged")

    def set_inspector(self, name):
        self.inspector_name = name
        self.log("INFO", f"Inspector set to: {name}")

    def set_camera_ip(self, ip):
        try:
            ipaddress.IPv4Address(ip.strip())
        except ValueError:
            self.log("WARN", f"'{ip}' is not a valid IPv4 address")
            return False
        self.camera.ip = ip.strip()
        self.camera.online = None
        self.log("INFO", f"Camera IP set to {self.camera.ip} - checking connection")
        self._camera_wake.set()
        return True

    def check_camera(self):
        self._camera_wake.set()

    def set_defect_rate(self, rate):
        self.inspectors[SimulatedInspector.name].defect_rate = rate

    def update_setting(self, key, value):
        self.settings[key] = value

    def set_manual_override(self, on):
        if on and (self.running or self.estop):
            self.log("WARN", "Manual override only allowed while stopped (and not in E-stop)")
            return False
        self.manual_override = on
        if on:
            self.lights.all_off()
        self.log("INFO", f"Manual output override {'ON' if on else 'OFF'}")
        return True

    def manual_set(self, key, on):
        if self.manual_override:
            self.lights.set(key, on)

    def test_burst(self):
        if self.running or self.estop:
            self.log("WARN", "Test burst only allowed while stopped (and not in E-stop)")
            return
        s = dict(self.settings)

        def run():
            ok = self.rejector.fire(s["pulses"], s["on_ms"] / 1000, s["off_ms"] / 1000)
            self.log("INFO" if ok else "WARN",
                     "Test burst fired" if ok else "Test burst blocked - rejector locked out (RESET)")
        threading.Thread(target=run, daemon=True).start()

    def simulate_can(self):
        """Inject a virtual can as if the sensor had triggered."""
        if self.gpio.sim:
            self.post("sensor", True)
            threading.Timer(0.12, lambda: self.post("sensor", False)).start()
        self._on_can_detected(time.monotonic())

    # ---- background loops ----------------------------------------------------
    def _sensor_loop(self):
        last = None
        last_trigger = 0.0
        while not self._shutdown.is_set():
            try:
                level = self.gpio.read(SENSOR_PIN)
            except Exception as e:                       # pinctrl hiccup
                self.log("ERROR", f"Sensor read failed: {e}")
                time.sleep(0.5)
                continue

            if level is not None and level != last:
                active = (level is False) if SENSOR_ACTIVE_LOW else (level is True)
                self.sensor_active = active
                if not self.gpio.sim:
                    self.post("sensor", active)
                now = time.monotonic()
                # trigger on the edge into "can present"; skip the very first read
                if active and last is not None and now - last_trigger >= MIN_CAN_GAP_S:
                    last_trigger = now
                    self._on_can_detected(now)
                last = level
            time.sleep(POLL_INTERVAL_S)

    def _camera_loop(self):
        last = "start"
        while not self._shutdown.is_set():
            self.camera.check()
            state = self.camera.online
            if state != last:
                if state is True:
                    self.log("INFO", f"Camera {self.camera.ip} ONLINE")
                elif state is False:
                    self.log("WARN", f"Camera {self.camera.ip} NOT REACHABLE - check IP, cable, Pi static IP")
                last = state
            self._camera_wake.wait(CAMERA_CHECK_INTERVAL_S)
            self._camera_wake.clear()

    def _desired_lights(self):
        if self.estop:
            return {"green": False, "yellow": False, "red": True}
        now = time.monotonic()
        flash = now < self._red_flash_until
        blink = int(now * 2) % 2 == 0          # 1 Hz blink for the alarm
        return {
            "green": self.running,
            "yellow": not self.running,
            "red": (self.alarm and blink) or (not self.alarm and self.running and flash),
        }

    def _light_loop(self):
        while not self._shutdown.is_set():
            if not self.manual_override:
                for k, v in self._desired_lights().items():
                    self.lights.set(k, v)
            time.sleep(0.05)

    # ---- can handling --------------------------------------------------------
    def _on_can_detected(self, t_detect):
        if not self.running or self.estop:
            self.log("INFO", "Can detected while line stopped - ignored")
            return
        with self._lock:
            self._can_counter += 1
            can_id = self._can_counter
        # each can gets its own worker so several can be "in flight" at once
        threading.Thread(target=self._process_can, args=(can_id, t_detect), daemon=True).start()

    def _process_can(self, can_id, t_detect):
        s = dict(self.settings)
        inspector = self.inspectors[self.inspector_name]

        t0 = time.monotonic()
        try:
            passed, defect, conf = inspector.inspect()
        except Exception as e:
            # fail-safe: if we can't inspect it, we don't ship it
            passed, defect, conf = False, "Inspection error", 0.0
            self.log("ERROR", f"Can #{can_id}: inspector failed ({e}) - rejecting as fail-safe")
        inspect_ms = (time.monotonic() - t0) * 1000

        rejected = False
        if passed:
            with self._lock:
                self._consecutive_rejects = 0
        else:
            with self._lock:
                self._consecutive_rejects += 1
                streak = self._consecutive_rejects
            self._red_flash_until = time.monotonic() + RED_FLASH_S

            if s["reject_enabled"]:
                wait = t_detect + s["reject_delay_ms"] / 1000 - time.monotonic()
                if wait < 0:
                    self.log("WARN", f"Can #{can_id}: inspection ({inspect_ms:.0f} ms) exceeded "
                                     f"reject delay - burst may miss the can")
                else:
                    time.sleep(wait)
                if self.estop:
                    self.log("WARN", f"Can #{can_id}: E-stop active, reject skipped")
                else:
                    rejected = self.rejector.fire(s["pulses"], s["on_ms"] / 1000, s["off_ms"] / 1000)
                    if not rejected:
                        self.log("WARN", f"Can #{can_id}: rejector locked out - defective can NOT rejected")
                    elif s["single_shot"]:
                        self.rejector.locked_out = True
                        self.log("WARN", "Single-shot latch: rejector locked out - RESET to re-arm")
            else:
                self.log("WARN", f"Can #{can_id}: FAIL but rejection disabled - can passed through")

            if streak >= s["alarm_after"] and not self.alarm:
                self.alarm = True
                self.log("ALARM", f"{streak} rejects in a row - check upstream process")

        self.post("result", InspectionResult(can_id, datetime.now(), passed, defect,
                                             conf, inspect_ms, rejected))

    # ---- shutdown ------------------------------------------------------------
    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self.running = False
        self._shutdown.set()
        self._camera_wake.set()
        for t in self._threads:
            t.join(timeout=0.5)
        self.rejector.locked_out = True
        self.rejector.safe()
        self.lights.all_off()
        print("Shutdown: solenoid de-energized, all outputs off.")


# =============================================================================
# DASHBOARD UI
# =============================================================================
BG = "#1e2430"
PANEL = "#2a3242"
PANEL2 = "#3b4455"
TEXT = "#ffffff"
MUTED = "#8e9aaf"
DOT_OFF = "#4f5b6e"
GREEN, YELLOW, RED, BLUE, PURPLE = "#2ecc71", "#f39c12", "#e74c3c", "#2980b9", "#9b59b6"

F_TITLE = ("Helvetica", 18, "bold")
F_SECTION = ("Helvetica", 10, "bold")
F_BODY = ("Helvetica", 10)
F_BTN = ("Helvetica", 11, "bold")

LOG_COLORS = {"INFO": MUTED, "WARN": YELLOW, "ERROR": RED, "ALARM": "#ff6b6b"}
MAX_TABLE_ROWS = 200
MAX_LOG_LINES = 500


class Dashboard:
    def __init__(self, root, ctl, events):
        self.root = root
        self.ctl = ctl
        self.events = events
        self.results = []
        self.recent_times = deque()
        self.manual_btns = {}
        self.light_dots = {}

        root.title("Yards Can Inspection - Line Controller")
        root.geometry("1280x880")
        root.minsize(1120, 820)
        root.configure(bg=BG)
        root.bind("<Escape>", lambda e: self.ctl.emergency_stop())

        self._style()
        self._build_header()
        body = tk.Frame(root, bg=BG)
        body.pack(fill="both", expand=True, padx=16, pady=(6, 16))
        body.columnconfigure(0, minsize=290)
        body.columnconfigure(1, weight=1)
        body.columnconfigure(2, minsize=330)
        body.rowconfigure(0, weight=1)
        self._build_left(body)
        self._build_center(body)
        self._build_right(body)

        self.root.after(50, self._tick)

    # ---- helpers -------------------------------------------------------------
    def _style(self):
        st = ttk.Style()
        st.theme_use("clam")
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                     rowheight=24, borderwidth=0, font=F_BODY)
        st.configure("Treeview.Heading", background=PANEL2, foreground=MUTED,
                     relief="flat", font=("Helvetica", 9, "bold"))
        st.map("Treeview", background=[("selected", BLUE)])
        st.configure("TCombobox", fieldbackground=PANEL2, background=PANEL2,
                     foreground=TEXT, arrowcolor=TEXT)
        st.map("TCombobox", fieldbackground=[("readonly", PANEL2)],
               foreground=[("readonly", TEXT)])
        st.configure("Vertical.TScrollbar", background=PANEL2, troughcolor=PANEL,
                     arrowcolor=MUTED, borderwidth=0)

    def _panel(self, parent, title):
        f = tk.Frame(parent, bg=PANEL, padx=12, pady=10)
        tk.Label(f, text=title, font=F_SECTION, fg=MUTED, bg=PANEL, anchor="w").pack(fill="x", pady=(0, 6))
        return f

    def _btn(self, parent, text, color, cmd):
        b = tk.Button(parent, text=text, command=cmd, font=F_BTN, bg=color, fg=TEXT,
                      activebackground=color, activeforeground=TEXT, disabledforeground="#6b7486",
                      relief="flat", bd=0, height=2, cursor="hand2", highlightthickness=0)
        b.base_color = color
        return b

    @staticmethod
    def _enable(btn, on):
        btn.config(state="normal" if on else "disabled", bg=btn.base_color if on else PANEL2)

    def _check(self, parent, text, key):
        var = tk.BooleanVar(value=self.ctl.settings[key])
        var.trace_add("write", lambda *_: self.ctl.update_setting(key, var.get()))
        tk.Checkbutton(parent, text=text, variable=var, font=F_BODY, fg=TEXT, bg=PANEL,
                       selectcolor=PANEL2, activebackground=PANEL, activeforeground=TEXT,
                       highlightthickness=0, anchor="w").pack(fill="x")
        return var

    def _spin(self, parent, row, label, key, lo, hi, step=1):
        tk.Label(parent, text=label, font=F_BODY, fg=TEXT, bg=PANEL, anchor="w").grid(
            row=row, column=0, sticky="w", pady=2)
        var = tk.StringVar(value=str(self.ctl.settings[key]))
        tk.Spinbox(parent, from_=lo, to=hi, increment=step, textvariable=var, width=7,
                   font=F_BODY, bg=PANEL2, fg=TEXT, buttonbackground=PANEL2,
                   insertbackground=TEXT, relief="flat", justify="right").grid(
            row=row, column=1, sticky="e", pady=2)

        def changed(*_):
            try:
                v = int(var.get())
            except ValueError:
                return
            self.ctl.update_setting(key, max(lo, min(hi, v)))
        var.trace_add("write", changed)

    # ---- layout --------------------------------------------------------------
    def _build_header(self):
        h = tk.Frame(self.root, bg=BG)
        h.pack(fill="x", padx=16, pady=(14, 6))
        left = tk.Frame(h, bg=BG)
        left.pack(side="left")
        tk.Label(left, text="CAN INSPECTION - LINE CONTROL", font=F_TITLE, fg=TEXT, bg=BG).pack(anchor="w")
        tk.Label(left, text="Yards Brewing - empty can defect station", font=F_BODY,
                 fg=MUTED, bg=BG).pack(anchor="w")

        right = tk.Frame(h, bg=BG)
        right.pack(side="right")
        sim = self.ctl.gpio.sim
        tk.Label(right, text=" SIMULATION " if sim else " HARDWARE ", font=F_SECTION,
                 fg=BG if sim else TEXT, bg=YELLOW if sim else GREEN, padx=6, pady=3).pack(anchor="e")
        self.clock = tk.Label(right, font=F_BODY, fg=MUTED, bg=BG)
        self.clock.pack(anchor="e", pady=(4, 0))

        self.banner = tk.Label(self.root, font=("Helvetica", 13, "bold"), fg=TEXT, pady=10)
        self.banner.pack(fill="x", padx=16, pady=(4, 6))

    def _build_left(self, body):
        col = tk.Frame(body, bg=BG)
        col.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        p = self._panel(col, "LINE CONTROL")
        p.pack(fill="x")
        g = tk.Frame(p, bg=PANEL)
        g.pack(fill="x")
        g.columnconfigure((0, 1), weight=1, uniform="b")
        self.b_start = self._btn(g, "START", GREEN, self.ctl.start)
        self.b_stop = self._btn(g, "STOP", YELLOW, self.ctl.stop)
        self.b_estop = self._btn(g, "E-STOP  (Esc)", RED, self.ctl.emergency_stop)
        self.b_reset = self._btn(g, "RESET", BLUE, self.ctl.reset)
        self.b_ack = self._btn(g, "ACK ALARM", PURPLE, self.ctl.ack_alarm)
        self.b_sim = self._btn(g, "+ SIMULATE CAN", "#16a085", self.ctl.simulate_can)
        self.b_start.grid(row=0, column=0, sticky="ew", padx=(0, 3), pady=3)
        self.b_stop.grid(row=0, column=1, sticky="ew", padx=(3, 0), pady=3)
        self.b_estop.grid(row=1, column=0, columnspan=2, sticky="ew", pady=3)
        self.b_reset.grid(row=2, column=0, sticky="ew", padx=(0, 3), pady=3)
        self.b_ack.grid(row=2, column=1, sticky="ew", padx=(3, 0), pady=3)
        self.b_sim.grid(row=3, column=0, columnspan=2, sticky="ew", pady=3)

        p = self._panel(col, "INSPECTION")
        p.pack(fill="x", pady=(10, 0))
        tk.Label(p, text="Inspector", font=F_BODY, fg=TEXT, bg=PANEL, anchor="w").pack(fill="x")
        self.insp_var = tk.StringVar(value=self.ctl.inspector_name)
        cb = ttk.Combobox(p, textvariable=self.insp_var, values=list(self.ctl.inspectors),
                          state="readonly", font=F_BODY)
        cb.pack(fill="x", pady=(2, 8))
        cb.bind("<<ComboboxSelected>>", lambda e: self.ctl.set_inspector(self.insp_var.get()))
        tk.Label(p, text="Simulated defect rate (%)", font=F_BODY, fg=TEXT, bg=PANEL, anchor="w").pack(fill="x")
        tk.Scale(p, from_=0, to=100, orient="horizontal", font=F_BODY, bg=PANEL, fg=TEXT,
                 troughcolor=PANEL2, highlightthickness=0, bd=0, activebackground=BLUE,
                 command=lambda v: self.ctl.set_defect_rate(float(v) / 100)).pack(fill="x")
        p.winfo_children()[-1].set(10)

        p = self._panel(col, "CAMERA (ETHERNET)")
        p.pack(fill="x", pady=(10, 0))
        row = tk.Frame(p, bg=PANEL)
        row.pack(fill="x")
        tk.Label(row, text="IP", font=F_BODY, fg=TEXT, bg=PANEL).pack(side="left")
        self.cam_ip_var = tk.StringVar(value=self.ctl.camera.ip)
        ent = tk.Entry(row, textvariable=self.cam_ip_var, width=15, font=F_BODY, bg=PANEL2, fg=TEXT,
                       insertbackground=TEXT, relief="flat")
        ent.pack(side="left", padx=6, fill="x", expand=True)
        ent.bind("<Return>", lambda e: self.ctl.set_camera_ip(self.cam_ip_var.get()))
        tk.Button(row, text="Apply", command=lambda: self.ctl.set_camera_ip(self.cam_ip_var.get()),
                  font=("Helvetica", 9, "bold"), bg=PANEL2, fg=TEXT, activebackground=BLUE,
                  activeforeground=TEXT, relief="flat", bd=0, padx=8, cursor="hand2").pack(side="left")
        st = tk.Frame(p, bg=PANEL)
        st.pack(fill="x", pady=(6, 0))
        self.cam_dot = tk.Label(st, text="●", font=("Helvetica", 14), fg=DOT_OFF, bg=PANEL)
        self.cam_dot.pack(side="left")
        self.cam_txt = tk.Label(st, text="Checking...", font=F_BODY, fg=MUTED, bg=PANEL)
        self.cam_txt.pack(side="left")
        tk.Button(st, text="Check now", command=self.ctl.check_camera, font=("Helvetica", 9, "bold"),
                  bg=PANEL2, fg=TEXT, activebackground=BLUE, activeforeground=TEXT, relief="flat",
                  bd=0, padx=8, cursor="hand2").pack(side="right")

        p = self._panel(col, "REJECT SETTINGS")
        p.pack(fill="x", pady=(10, 0))
        self._check(p, "Reject enabled", "reject_enabled")
        self._check(p, "Single-shot latch (test)", "single_shot")
        g = tk.Frame(p, bg=PANEL)
        g.pack(fill="x", pady=(6, 0))
        g.columnconfigure(0, weight=1)
        self._spin(g, 0, "Reject delay (ms)", "reject_delay_ms", 0, 5000, 5)
        self._spin(g, 1, "Air pulses", "pulses", 1, 10)
        self._spin(g, 2, "Pulse ON (ms)", "on_ms", 5, 500, 5)
        self._spin(g, 3, "Pulse OFF (ms)", "off_ms", 5, 500, 5)
        self._spin(g, 4, "Alarm after N rejects", "alarm_after", 1, 50)

    def _build_center(self, body):
        col = tk.Frame(body, bg=BG)
        col.grid(row=0, column=1, sticky="nsew")
        col.rowconfigure(3, weight=1)
        col.columnconfigure(0, weight=1)

        top = tk.Frame(col, bg=BG)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)

        card = tk.Frame(top, bg=PANEL, padx=16, pady=12)
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        tk.Label(card, text="LAST RESULT", font=F_SECTION, fg=MUTED, bg=PANEL, anchor="w").pack(fill="x")
        self.res_big = tk.Label(card, text="--", font=("Helvetica", 36, "bold"), fg=MUTED, bg=PANEL, anchor="w")
        self.res_big.pack(fill="x")
        self.res_detail = tk.Label(card, text="Waiting for first can", font=F_BODY, fg=MUTED, bg=PANEL, anchor="w")
        self.res_detail.pack(fill="x")

        sc = tk.Frame(top, bg=PANEL, padx=16, pady=12)
        sc.grid(row=0, column=1, sticky="nsew")
        tk.Label(sc, text="SENSOR", font=F_SECTION, fg=MUTED, bg=PANEL).pack()
        self.sensor_dot = tk.Label(sc, text="●", font=("Helvetica", 40), fg=DOT_OFF, bg=PANEL)
        self.sensor_dot.pack()
        self.sensor_txt = tk.Label(sc, text="CLEAR", font=F_SECTION, fg=MUTED, bg=PANEL)
        self.sensor_txt.pack()
        tk.Label(sc, text=f"GPIO {SENSOR_PIN}", font=("Helvetica", 9), fg=MUTED, bg=PANEL).pack()

        stats = tk.Frame(col, bg=BG)
        stats.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.stat = {}
        for i, (key, label) in enumerate([("total", "INSPECTED"), ("pass", "PASSED"),
                                          ("fail", "FAILED"), ("yield", "YIELD"),
                                          ("rate", "CANS / MIN"), ("ms", "AVG INSPECT")]):
            stats.columnconfigure(i, weight=1, uniform="s")
            t = tk.Frame(stats, bg=PANEL, pady=8)
            t.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 6, 0))
            v = tk.Label(t, text="0", font=("Helvetica", 18, "bold"), fg=TEXT, bg=PANEL)
            v.pack()
            tk.Label(t, text=label, font=("Helvetica", 8, "bold"), fg=MUTED, bg=PANEL).pack()
            self.stat[key] = v

        self.defects_lbl = tk.Label(col, text="Defects: none", font=F_BODY, fg=MUTED, bg=BG, anchor="w")
        self.defects_lbl.grid(row=2, column=0, sticky="ew", pady=(8, 4))

        tp = tk.Frame(col, bg=PANEL, padx=10, pady=10)
        tp.grid(row=3, column=0, sticky="nsew")
        hdr = tk.Frame(tp, bg=PANEL)
        hdr.pack(fill="x", pady=(0, 6))
        tk.Label(hdr, text="RECENT INSPECTIONS", font=F_SECTION, fg=MUTED, bg=PANEL).pack(side="left")
        for txt, cmd in (("Clear", self._clear), ("Export CSV", self._export)):
            tk.Button(hdr, text=txt, command=cmd, font=("Helvetica", 9, "bold"), bg=PANEL2, fg=TEXT,
                      activebackground=BLUE, activeforeground=TEXT, relief="flat", bd=0,
                      padx=10, pady=3, cursor="hand2").pack(side="right", padx=(6, 0))

        cols = ("id", "time", "result", "defect", "conf", "ms", "air")
        heads = ("Can #", "Time", "Result", "Defect", "Conf.", "ms", "Air")
        widths = (60, 90, 80, 150, 60, 60, 50)
        self.tree = ttk.Treeview(tp, columns=cols, show="headings")
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w" if c == "defect" else "center")
        self.tree.tag_configure("pass", foreground=GREEN)
        self.tree.tag_configure("fail", foreground=RED)
        sb = ttk.Scrollbar(tp, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def _build_right(self, body):
        col = tk.Frame(body, bg=BG)
        col.grid(row=0, column=2, sticky="nsew", padx=(10, 0))
        col.rowconfigure(1, weight=1)
        col.columnconfigure(0, weight=1)

        p = self._panel(col, "OUTPUTS")
        p.grid(row=0, column=0, sticky="ew")
        for key, label, pin, color in LIGHT_OUTPUTS:
            row = tk.Frame(p, bg=PANEL)
            row.pack(fill="x", pady=2)
            dot = tk.Label(row, text="●", font=("Helvetica", 18), fg=DOT_OFF, bg=PANEL, width=2)
            dot.pack(side="left")
            dot.on_color = color
            self.light_dots[key] = dot
            tk.Label(row, text=f"{label}", font=F_SECTION, fg=TEXT, bg=PANEL).pack(side="left")
            tk.Label(row, text=f"  GPIO {pin}", font=("Helvetica", 9), fg=MUTED, bg=PANEL).pack(side="left")
            b = self._btn(row, "OFF", PANEL2, lambda k=key: self._manual_toggle(k))
            b.config(width=6, height=1)
            b.pack(side="right")
            b.on_color = color
            self.manual_btns[key] = b

        row = tk.Frame(p, bg=PANEL)
        row.pack(fill="x", pady=2)
        self.reject_dot = tk.Label(row, text="●", font=("Helvetica", 18), fg=DOT_OFF, bg=PANEL, width=2)
        self.reject_dot.pack(side="left")
        tk.Label(row, text="REJECTOR", font=F_SECTION, fg=TEXT, bg=PANEL).pack(side="left")
        tk.Label(row, text=f"  GPIO {REJECT_PIN} · relay 4", font=("Helvetica", 9),
                 fg=MUTED, bg=PANEL).pack(side="left")

        self.override_var = tk.BooleanVar(value=False)
        tk.Checkbutton(p, text="Manual override (line stopped)", variable=self.override_var,
                       command=self._override_changed, font=F_BODY, fg=TEXT, bg=PANEL,
                       selectcolor=PANEL2, activebackground=PANEL, activeforeground=TEXT,
                       highlightthickness=0, anchor="w").pack(fill="x", pady=(6, 4))
        self.b_burst = self._btn(p, f"TEST REJECT BURST  (GPIO {REJECT_PIN})", "#c0392b", self.ctl.test_burst)
        self.b_burst.pack(fill="x")

        lp = self._panel(col, "EVENT LOG")
        lp.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        inner = tk.Frame(lp, bg=PANEL)
        inner.pack(fill="both", expand=True)
        self.log_txt = tk.Text(inner, bg="#232a37", fg=TEXT, font=("Courier", 9), relief="flat",
                               wrap="word", state="disabled", highlightthickness=0, padx=6, pady=4)
        sb = ttk.Scrollbar(inner, orient="vertical", command=self.log_txt.yview)
        self.log_txt.configure(yscrollcommand=sb.set)
        self.log_txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for lvl, c in LOG_COLORS.items():
            self.log_txt.tag_configure(lvl, foreground=c)
        self.log_txt.tag_configure("ts", foreground=DOT_OFF)

    # ---- UI actions ----------------------------------------------------------
    def _override_changed(self):
        if not self.ctl.set_manual_override(self.override_var.get()):
            self.override_var.set(False)

    def _manual_toggle(self, key):
        self.ctl.manual_set(key, not self.ctl.lights.state[key])

    def _clear(self):
        if self.results and not messagebox.askyesno("Clear", "Clear all results and statistics?"):
            return
        self.results.clear()
        self.recent_times.clear()
        self.tree.delete(*self.tree.get_children())
        self.res_big.config(text="--", fg=MUTED)
        self.res_detail.config(text="Waiting for next can")
        self._update_stats()

    def _export(self):
        if not self.results:
            messagebox.showinfo("Export", "No results to export yet.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile=f"inspection_{datetime.now():%Y%m%d_%H%M%S}.csv")
        if not path:
            return
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["can_id", "timestamp", "result", "defect", "confidence", "inspect_ms", "air_fired"])
            for r in self.results:
                w.writerow([r.can_id, r.timestamp.isoformat(timespec="milliseconds"),
                            "PASS" if r.passed else "FAIL", r.defect, f"{r.confidence:.3f}",
                            f"{r.inspect_ms:.1f}", r.rejected])
        self._log("INFO", f"Exported {len(self.results)} results to {path}")

    # ---- event handling ------------------------------------------------------
    def _log(self, level, msg):
        t = self.log_txt
        t.config(state="normal")
        t.insert("end", datetime.now().strftime("%H:%M:%S "), "ts")
        t.insert("end", f"{level:<5} {msg}\n", level)
        lines = int(t.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            t.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        t.see("end")
        t.config(state="disabled")

    def _on_result(self, r):
        self.results.append(r)
        self.recent_times.append(time.monotonic())
        if r.passed:
            self.res_big.config(text="PASS", fg=GREEN)
        else:
            self.res_big.config(text="REJECT" if r.rejected else "FAIL", fg=RED)
        self.res_detail.config(
            text=f"Can #{r.can_id}  ·  {r.defect if not r.passed else 'no defect'}  ·  "
                 f"{r.confidence * 100:.0f}% conf  ·  {r.inspect_ms:.0f} ms"
                 + ("  ·  air fired" if r.rejected else ""))
        self.tree.insert("", 0, tags=("pass" if r.passed else "fail",), values=(
            r.can_id, r.timestamp.strftime("%H:%M:%S"), "PASS" if r.passed else "FAIL",
            r.defect, f"{r.confidence * 100:.0f}%", f"{r.inspect_ms:.0f}",
            "yes" if r.rejected else ""))
        kids = self.tree.get_children()
        if len(kids) > MAX_TABLE_ROWS:
            self.tree.delete(*kids[MAX_TABLE_ROWS:])
        self._update_stats()

    def _update_stats(self):
        n = len(self.results)
        npass = sum(r.passed for r in self.results)
        nfail = n - npass
        now = time.monotonic()
        while self.recent_times and now - self.recent_times[0] > 60:
            self.recent_times.popleft()
        self.stat["total"].config(text=str(n))
        self.stat["pass"].config(text=str(npass), fg=GREEN if npass else TEXT)
        self.stat["fail"].config(text=str(nfail), fg=RED if nfail else TEXT)
        self.stat["yield"].config(text=f"{npass / n * 100:.1f}%" if n else "--")
        self.stat["rate"].config(text=str(len(self.recent_times)))
        self.stat["ms"].config(text=f"{sum(r.inspect_ms for r in self.results) / n:.0f} ms" if n else "--")
        defects = Counter(r.defect for r in self.results if not r.passed)
        self.defects_lbl.config(text="Defects: " + ("  ·  ".join(f"{d} {c}" for d, c in defects.most_common())
                                                    if defects else "none"))

    def _refresh_state(self):
        c = self.ctl
        if c.estop:
            text, bg = "E-STOP ACTIVE - press RESET to re-arm", RED
        elif c.alarm:
            text, bg = "ALARM - consecutive rejects, press ACK ALARM", PURPLE
        elif c.running:
            text, bg = f"RUNNING - {c.inspector_name}", "#27ae60"
        elif c.manual_override:
            text, bg = "STOPPED - MANUAL OUTPUT OVERRIDE", BLUE
        else:
            text, bg = "STOPPED", "#34495e"
        if c.rejector.locked_out and not c.estop:
            text += "   |   REJECTOR LOCKED OUT"
        self.banner.config(text=text, bg=bg)

        for key, dot in self.light_dots.items():
            on = c.lights.state[key]
            dot.config(fg=dot.on_color if on else DOT_OFF)
            b = self.manual_btns[key]
            b.base_color = b.on_color if on else PANEL2
            b.config(text="ON" if on else "OFF")
            self._enable(b, c.manual_override)

        self.reject_dot.config(fg="#c0392b" if c.rejector.on else DOT_OFF)

        cam = c.camera
        if cam.online is True:
            self.cam_dot.config(fg=GREEN)
            self.cam_txt.config(text=f"Online · {cam.latency_ms:.0f} ms", fg=TEXT)
        elif cam.online is False:
            self.cam_dot.config(fg=RED)
            self.cam_txt.config(text="Not reachable", fg=RED)
        else:
            self.cam_dot.config(fg=DOT_OFF)
            self.cam_txt.config(text="Checking...", fg=MUTED)

        self._enable(self.b_start, not c.running and not c.estop)
        self._enable(self.b_stop, c.running)
        self._enable(self.b_ack, c.alarm)
        self._enable(self.b_sim, c.running)
        self._enable(self.b_burst, not c.running and not c.estop)
        if self.override_var.get() != c.manual_override:
            self.override_var.set(c.manual_override)
        self.clock.config(text=datetime.now().strftime("%a %b %d  %H:%M:%S"))

    def _tick(self):
        for _ in range(200):
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._log(*payload)
            elif kind == "result":
                self._on_result(payload)
            elif kind == "sensor":
                self.sensor_dot.config(fg=BLUE if payload else DOT_OFF)
                self.sensor_txt.config(text="CAN PRESENT" if payload else "CLEAR",
                                       fg=TEXT if payload else MUTED)
        self._refresh_state()
        if int(time.monotonic()) % 2 == 0:
            self.stat["rate"].config(text=str(sum(1 for t in self.recent_times
                                                  if time.monotonic() - t <= 60)))
        self.root.after(50, self._tick)


# =============================================================================
def main():
    check_pin_conflicts()
    events = queue.Queue()
    ctl = Controller(GPIO(), events)
    root = tk.Tk()
    Dashboard(root, ctl, events)

    def on_close():
        if ctl.running and not messagebox.askyesno("Quit", "Line is running. Stop and quit?"):
            return
        ctl.shutdown()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    try:
        root.mainloop()
    finally:
        ctl.shutdown()   # fail-safe: solenoid off, lights off


if __name__ == "__main__":
    main()
