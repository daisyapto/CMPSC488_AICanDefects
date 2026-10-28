"""
Hardware layer: GPIO access, relays, sensor input and the camera link.

GPIO backends
  * PinctrlIO - the `pinctrl` CLI (Raspberry Pi 5), same as the original test
                scripts. Output levels stay latched after the program exits,
                so the relays stay in the safe (off) state.
  * SimIO     - used when pinctrl is missing (laptop) or with --sim.

Sensor input prefers gpiozero (edge interrupts, accurate timestamps) and falls
back to polling with pinctrl if gpiozero is not available.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time

from .config import (BUZZER_PIN, LIGHT_PINS, POLL_INTERVAL_S, REJECT_PIN, RELAY_ACTIVE_LOW,
                     SENSOR_ACTIVE_LOW, SENSOR_PIN, SENSOR_PULL)

_QUIET = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}


# =============================================================================
# GPIO backends
# =============================================================================
class PinctrlIO:
    sim = False

    def write(self, pin: int, high: bool):
        subprocess.run(["pinctrl", "set", str(pin), "op", "dh" if high else "dl"],
                       check=False, timeout=2, **_QUIET)

    def setup_input(self, pin: int, pull: str):
        subprocess.run(["pinctrl", "set", str(pin), "ip", pull], check=False, timeout=2, **_QUIET)

    def read(self, pin: int):
        """True = HIGH, False = LOW, None = unknown."""
        out = subprocess.check_output(["pinctrl", "get", str(pin)], text=True, timeout=1)
        if "| hi" in out:
            return True
        if "| lo" in out:
            return False
        return None


class SimIO:
    sim = True

    def __init__(self):
        self.levels: dict[int, bool] = {}

    def write(self, pin, high):
        self.levels[pin] = high

    def setup_input(self, pin, pull):
        self.levels.setdefault(pin, SENSOR_ACTIVE_LOW)   # idle level = no can

    def read(self, pin):
        return self.levels.get(pin, SENSOR_ACTIVE_LOW)


def make_io(force_sim: bool = False):
    if force_sim or shutil.which("pinctrl") is None:
        return SimIO()
    return PinctrlIO()


# =============================================================================
# Relays (stack light + reject valve share the active-low relay board)
# =============================================================================
class Relay:
    def __init__(self, io, pin: int):
        self.io = io
        self.pin = pin
        self.on = False
        self._lock = threading.Lock()
        self.set(False, force=True)

    def set(self, on: bool, force: bool = False):
        with self._lock:
            if on == self.on and not force:
                return
            self.io.write(self.pin, (not on) if RELAY_ACTIVE_LOW else on)
            self.on = on


class StackLight:
    def __init__(self, io):
        self.lamps = {key: Relay(io, pin) for key, pin in LIGHT_PINS.items()}
        self.buzzer = Relay(io, BUZZER_PIN)        # relay 4 - switched by hand (and by rejects until the rejector is wired)

    def set(self, key: str, on: bool, force: bool = False):
        self.lamps[key].set(on, force)

    def apply(self, states: dict):
        for key, on in states.items():
            self.set(key, on)

    @property
    def state(self) -> dict:
        return {key: lamp.on for key, lamp in self.lamps.items()}

    def all_off(self):
        for lamp in self.lamps.values():
            lamp.set(False, force=True)
        self.buzzer.set(False, force=True)


class Rejector:
    """Reject solenoid on relay 4. Every burst ends with the valve closed."""

    def __init__(self, io):
        self.valve = Relay(io, REJECT_PIN)
        self.locked_out = False
        self._lock = threading.Lock()

    @property
    def on(self) -> bool:
        return self.valve.on

    def safe(self):
        self.valve.set(False, force=True)

    def fire(self, pulses: int, on_s: float, off_s: float) -> bool:
        if self.locked_out:
            return False
        with self._lock:
            try:
                for _ in range(pulses):
                    if self.locked_out:          # E-stop pressed mid-burst
                        break
                    self.valve.set(True)
                    time.sleep(on_s)
                    self.valve.set(False)
                    time.sleep(off_s)
            finally:
                self.safe()
        return True


# =============================================================================
# Sensor input
# =============================================================================
class SensorInput:
    """Calls on_edge(active, t_monotonic, initial) whenever the sensor changes."""

    def __init__(self, io, on_edge, backend: str = "auto", log=None):
        self.io = io
        self.on_edge = on_edge
        self.backend = backend
        self.log = log or (lambda level, msg: print(level, msg))
        self.mode = "not started"
        self.active = False
        self.error = ""             # non-empty while the sensor cannot be read
        self._dev = None
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        if self.io.sim:
            self.mode = "simulated"
            return
        if self.backend in ("auto", "gpiozero") and self._start_gpiozero():
            return
        self._start_polling()

    def _start_gpiozero(self) -> bool:
        try:
            import warnings
            from gpiozero import DigitalInputDevice
            dev = None
            # Pi 5: the header is gpiochip0 on current kernels (gpiochip4 on early
            # ones), and older gpiozero only looks for 4 - so try both explicitly.
            try:
                from gpiozero.pins.lgpio import LGPIOFactory
                for chip in (0, 4):
                    try:
                        dev = DigitalInputDevice(SENSOR_PIN, pull_up=None,
                                                 active_state=not SENSOR_ACTIVE_LOW,
                                                 pin_factory=LGPIOFactory(chip=chip))
                        break
                    except Exception:
                        dev = None
            except ImportError:
                pass
            if dev is None:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    dev = DigitalInputDevice(SENSOR_PIN, pull_up=None, active_state=not SENSOR_ACTIVE_LOW)
        except Exception as e:  # not installed, no pin factory, pin busy...
            level = "WARN" if self.backend == "gpiozero" else "INFO"
            self.log(level, f"gpiozero unavailable ({e.__class__.__name__}) - using pinctrl polling")
            return False
        dev.when_activated = lambda: self._edge(True)
        dev.when_deactivated = lambda: self._edge(False)
        self._dev = dev
        self.mode = "gpiozero interrupt"
        self._edge(bool(dev.is_active), initial=True)
        return True

    def _start_polling(self):
        self.io.setup_input(SENSOR_PIN, SENSOR_PULL)
        self.mode = f"pinctrl poll {POLL_INTERVAL_S * 1000:.0f} ms"
        self._thread = threading.Thread(target=self._poll, daemon=True, name="sensor")
        self._thread.start()

    def _poll(self):
        last = None
        failing = False
        while not self._stop.is_set():
            try:
                level = self.io.read(SENSOR_PIN)
                failing = False
                self.error = ""
            except Exception as e:
                if not failing:
                    self.log("ERROR", f"Sensor read failed: {e}")
                failing = True
                self.error = "cannot read GPIO"
                time.sleep(0.5)
                continue
            if level is not None and level != last:
                active = (level is False) if SENSOR_ACTIVE_LOW else (level is True)
                self._edge(active, initial=last is None)
                last = level
            time.sleep(POLL_INTERVAL_S)

    def _edge(self, active: bool, initial: bool = False, t: float | None = None):
        self.active = active
        self.on_edge(active, time.monotonic() if t is None else t, initial)

    def inject_pulse(self, width_s: float | None = None):
        """Simulate a can passing the sensor (Simulate can button).
        Width ~80 ms = a 66 mm can at about 0.8 m/s, with a little belt-speed jitter."""
        import random
        width_s = width_s if width_s is not None else random.uniform(0.077, 0.083)
        t0 = time.monotonic()
        self._edge(True, t=t0)
        # the "beam clear" time is exact, like a real sensor edge (the timer itself may run late)
        threading.Timer(width_s, lambda: self._edge(False, t=t0 + width_s)).start()

    def stop(self):
        self._stop.set()
        if self._dev is not None:
            try:
                self._dev.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=0.5)


# =============================================================================
# Camera link (Ethernet)
# =============================================================================
class CameraLink:
    """Checks the Ethernet camera answers at its IP address (one ping)."""

    def __init__(self, ip: str):
        self.ip = ip
        self.online = None        # None = unknown / not checked yet
        self.latency_ms = None
        self._ping = shutil.which("ping")

    @property
    def can_ping(self) -> bool:
        return self._ping is not None

    def check(self):
        if not self._ping:
            self.online = self.latency_ms = None
            return
        if sys.platform.startswith("win"):
            args = ["-n", "1", "-w", "1000"]          # Windows: count, timeout in ms
        elif sys.platform == "darwin":
            args = ["-c", "1", "-W", "1000"]          # macOS: timeout in ms
        else:
            args = ["-c", "1", "-W", "1"]             # Linux / Pi: timeout in s
        t0 = time.monotonic()
        try:
            r = subprocess.run([self._ping, *args, self.ip], timeout=3, **_QUIET)
            ok = r.returncode == 0
        except subprocess.TimeoutExpired:
            ok = False
        self.online = ok
        self.latency_ms = (time.monotonic() - t0) * 1000 if ok else None
