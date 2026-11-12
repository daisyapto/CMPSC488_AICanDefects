"""
can_inspection_station_hailo_fast.py - Inspection station UI (live view, log, GPIO 25 sensor, stack light)
with YOLO on the Hailo-8 AI HAT, a faster pipeline, and per-can IDs.

Speed changes vs. can_inspection_station_hailo.py
  1. Two-stage pipeline: the camera thread only grabs frames; a separate inference thread
     converts + runs YOLO on the NEWEST frame. Grabbing and inference now overlap.
  2. Frames are downscaled right after debayering (PROC_MAX_SIDE, default 1280 px). YOLO resizes to
     640 anyway, so accuracy is unchanged, but every later step (resize, drawing, display) is cheaper.
  3. Boxes are only drawn on frames that will actually be displayed (DISPLAY_MAX_FPS, default 15).
     Detection, counting, tracking and the stack light still run on every frame.
  4. GUI work is batched: prediction-log lines are flushed every LOG_FLUSH_MS and labels are
     refreshed on a timer instead of on every frame.
  5. Display uses BGR888 directly (no extra colour conversion) and fast scaling.
  6. GPIO uses libgpiod in-process (pip install gpiod). The old code started a `pinctrl` process
     50 times a second just to poll the sensor. Falls back to pinctrl if gpiod is unavailable.

Can IDs (tracking)
  * Ultralytics ByteTrack gives every can a persistent ID while it is in view.
  * A "can" is a tracked box that is not inside a larger tracked box; defect boxes whose centre is
    inside a can box count as a defect vote for that can (works whether the model labels the whole
    can or only the defect area).
  * When an ID has not been seen for LOST_AFTER frames, the can gets one verdict:
    DEFECT if at least MIN_DEFECT_FRAMES frames showed a defect, otherwise GOOD.
    Tracks seen for fewer than MIN_TRACK_FRAMES frames are ignored as noise.
  * Verdicts appear in the prediction log, the system log and the "Tracked cans" counter.
  * --no-track turns tracking off (slightly faster).

Stack light (Auto mode), sensor counting and per-can sensor verdicts work as before.

Usage:
    python can_inspection_station_hailo_fast.py --continuous        # bench test, no sensor
    python can_inspection_station_hailo_fast.py                     # sensor mode
    python can_inspection_station_hailo_fast.py --no-track          # no can IDs
    python can_inspection_station_hailo_fast.py --model best.pt     # CPU model, for comparison
"""

import os
import sys
import glob
import argparse
import html
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
import socket

import numpy as np
import cv2

global GLOBAL_OBJECT_COUNTER # ACN
GLOBAL_OBJECT_COUNTER = 0
global ATTEMPTED_REQUESTS # check if request for this ACN was attempted
ATTEMPTED_REQUESTS = dict()
global PI_ADDR
PI_ADDR = ('192.168.50.10', 5000)
global _rj_socket
_rj_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

def send_rejection():
    packet = bytes([1, GLOBAL_OBJECT_COUNTER % 256])
    if GLOBAL_OBJECT_COUNTER in ATTEMPTED_REQUESTS:
        return
    else: #we build packet and we haven't tried sending it yet
        try:
            _rj_socket.sendto(packet, PI_ADDR)
            ATTEMPTED_REQUESTS[GLOBAL_OBJECT_COUNTER] = packet
            print(f"[INFO] Sending Data: {GLOBAL_OBJECT_COUNTER} as {packet} to Addr: {PI_ADDR}")
        except OSError as error:
            print(f"[ERROR] Failed to send packet: {error}")
    return

try:
    import PySpin
except ImportError:
    PySpin = None

try:
    import gpiod
    from gpiod.line import Direction, Value, Bias
except ImportError:
    gpiod = None

from ultralytics import YOLO

from PyQt6.QtCore import QThread, QTimer, pyqtSignal, Qt
from PyQt6.QtGui import QImage, QPixmap, QFontDatabase, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QSlider, QGroupBox, QTextEdit, QComboBox, QGridLayout,
    QStackedWidget, QPlainTextEdit, QButtonGroup, QFileDialog,
)

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
SENSOR_PIN = 25                    # BCM GPIO 25
SENSOR_PHYSICAL_PIN = 22           # header pin 22
SENSOR_ACTIVE_STATE = "LOW"        # active-low sensor: LOW = object present
PASS_REARM_MS = 150                # sensor must stay clear this long before the next object counts
PASS_RATE_WINDOW_S = 60            # window for the objects/min rate display
SENSOR_POLL_S = 0.02               # 50 Hz polling
SENSOR_DEBOUNCE_READS = 2          # consecutive identical reads before a change counts

GET_NEXT_IMAGE_TIMEOUT_MS = 1000

FAULT_AFTER_ERRORS = 5             # consecutive grab errors/timeouts before declaring a camera fault
CAMERA_RETRY_S = 3.0               # wait between camera reconnect attempts
BUZZER_FAULT_MS = 3000             # buzzer length when a camera fault starts (0 = until recovered)
DEFECT_CLEAR_FRAMES = 1            # non-defect frames needed before YELLOW -> GREEN (raise if it flickers)
CHASE_STEP_MS = 250                # manual test chase step length

GOOD_KEYWORD = "good"              # any class whose name contains this counts as "good"

PRED_LOG_MAX_LINES = 5000          # prediction log keeps the most recent N lines

# --- model / speed ---
DEFAULT_MODEL = "/home/team3/best/best_hailo_model"   # Hailo export folder (best.hef + metadata.yaml)
DEFAULT_IMGSZ = 640                # must match the size used at Hailo export
PROC_MAX_SIDE = 1280               # downscale camera frames to this longest side before YOLO (0 = off)
DISPLAY_MAX_FPS = 15               # max rate frames are drawn + shown in the UI
LOG_FLUSH_MS = 250                 # prediction-log batch interval
UI_REFRESH_MS = 200                # label refresh interval
FPS_WINDOW_S = 1.0                 # FPS averaging window

# --- can tracking ---
TRACKER_CFG = "bytetrack.yaml"     # Ultralytics built-in tracker config
MIN_DEFECT_FRAMES = 3              # frames that must show a defect for a DEFECT verdict
LOST_AFTER = 10                    # frames without seeing an ID = can has left
MIN_TRACK_FRAMES = 3               # ignore IDs seen fewer frames than this (noise)

# (key, label, BCM GPIO, active colour) - outputs are ACTIVE-LOW (low = on, high = off)
OUTPUTS = [
    ("green",  "GREEN - ACTIVE",       18, "#2ecc71"),
    ("yellow", "YELLOW - DEFECT",      15, "#f39c12"),
    ("red",    "RED - CAMERA FAULT",   14, "#e74c3c"),
    ("buzzer", "BUZZER - CAMERA FAULT", 23, "#9b59b6"),
]

# Spinnaker pixel format prefix -> OpenCV code.
# OpenCV's legacy Bayer names are offset by one pixel, so an RGGB sensor (BayerRG8) needs BayerBG2BGR.
BAYER_TO_CV = [
    ("BayerRG", cv2.COLOR_BayerBG2BGR),
    ("BayerGR", cv2.COLOR_BayerGB2BGR),
    ("BayerGB", cv2.COLOR_BayerGR2BGR),
    ("BayerBG", cv2.COLOR_BayerRG2BGR),
]

BTN_STYLE = "color: white; font-weight: bold; padding: 10px; border-radius: 4px;"
RED_BTN = f"background-color: #d9534f; {BTN_STYLE}"
GREEN_BTN = f"background-color: #5cb85c; {BTN_STYLE}"
VIEW_BTN = (
    "QPushButton { background-color: #2a3242; color: #8e9aaf; font-weight: bold;"
    " padding: 8px 18px; border: none; border-radius: 4px; }"
    "QPushButton:checked { background-color: #2980b9; color: white; }"
)


# ----------------------------------------------------------------------------
# GPIO - libgpiod in-process (fast), falls back to `pinctrl`, then to a no-op
# ----------------------------------------------------------------------------
class GPIO:
    def __init__(self):
        self.backend = "none"
        self._lock = threading.Lock()
        self._chip_path = None
        self._requests = {}           # pin -> gpiod LineRequest
        if gpiod is not None:
            self._chip_path = self._find_rp1_chip()
            if self._chip_path:
                self.backend = "gpiod"
        if self.backend == "none" and shutil.which("pinctrl"):
            self.backend = "pinctrl"
        self.available = self.backend != "none"

    @staticmethod
    def _find_rp1_chip():
        """The Pi 5 header pins live on the RP1 chip ('pinctrl-rp1')."""
        for path in sorted(glob.glob("/dev/gpiochip*")):
            try:
                with gpiod.Chip(path) as chip:
                    if "rp1" in chip.get_info().label.lower():
                        return path
            except Exception:
                continue
        return None

    def _fallback_to_pinctrl(self, err):
        print(f"[GPIO] gpiod failed ({err}); falling back to pinctrl")
        for req in self._requests.values():
            try:
                req.release()
            except Exception:
                pass
        self._requests.clear()
        self.backend = "pinctrl" if shutil.which("pinctrl") else "none"
        self.available = self.backend != "none"

    # -- pinctrl helper --
    def _pinctrl(self, *args):
        with self._lock:
            try:
                return subprocess.run(["pinctrl", *map(str, args)], capture_output=True,
                                      text=True, check=False, timeout=2)
            except Exception:
                return None

    # -- public API (same as before) --
    def setup_input(self, pin):
        if self.backend == "gpiod":
            try:
                with self._lock:
                    self._requests[pin] = gpiod.request_lines(
                        self._chip_path, consumer="can-inspection",
                        config={pin: gpiod.LineSettings(direction=Direction.INPUT,
                                                        bias=Bias.DISABLED)})
                return
            except Exception as e:
                self._fallback_to_pinctrl(e)
        if self.backend == "pinctrl":
            self._pinctrl("set", pin, "ip", "pn")

    def write(self, pin, on):
        if self.backend == "gpiod":
            try:
                with self._lock:
                    value = Value.INACTIVE if on else Value.ACTIVE        # active-low
                    req = self._requests.get(pin)
                    if req is None:
                        req = gpiod.request_lines(
                            self._chip_path, consumer="can-inspection",
                            config={pin: gpiod.LineSettings(direction=Direction.OUTPUT,
                                                            output_value=value)})
                        self._requests[pin] = req
                    else:
                        req.set_value(pin, value)
                return
            except Exception as e:
                self._fallback_to_pinctrl(e)
        if self.backend == "pinctrl":
            self._pinctrl("set", pin, "op", "dl" if on else "dh")

    def read(self, pin):
        if self.backend == "gpiod":
            try:
                with self._lock:
                    req = self._requests.get(pin)
                    if req is None:
                        return "UNKNOWN"
                    return "HIGH" if req.get_value(pin) == Value.ACTIVE else "LOW"
            except Exception:
                return "UNKNOWN"
        if self.backend == "pinctrl":
            r = self._pinctrl("get", pin)
            if r is None or r.returncode != 0:
                return "UNKNOWN"
            if "| lo" in r.stdout:
                return "LOW"
            if "| hi" in r.stdout:
                return "HIGH"
        return "UNKNOWN"

    def close(self):
        with self._lock:
            for req in self._requests.values():
                try:
                    req.release()
                except Exception:
                    pass
            self._requests.clear()


class SensorMonitor(QThread):
    """Polls the sensor pin and emits debounced state changes."""
    state_changed = pyqtSignal(str)   # "LOW" / "HIGH" / "UNKNOWN"

    def __init__(self, gpio, pin):
        super().__init__()
        self.gpio = gpio
        self.pin = pin
        self._running = True

    def run(self):
        last_emitted, candidate, count = None, None, 0
        while self._running:
            try:
                s = self.gpio.read(self.pin)
                if s == candidate:
                    count += 1
                else:
                    candidate, count = s, 1
                if count >= SENSOR_DEBOUNCE_READS and s != last_emitted:
                    last_emitted = s
                    self.state_changed.emit(s)
            except Exception:
                pass
            time.sleep(SENSOR_POLL_S)

    def stop(self):
        self._running = False
        self.wait()


# ----------------------------------------------------------------------------
# Image helpers
# ----------------------------------------------------------------------------
def to_bgr(frame, pixel_format):
    """Convert a raw Spinnaker NDArray into a 3-channel BGR uint8 image."""
    if frame.dtype != np.uint8:
        frame = (frame / 256).astype(np.uint8)
    if frame.ndim == 3 and frame.shape[2] == 1:
        frame = frame[:, :, 0]
    if frame.ndim == 2:
        for prefix, code in BAYER_TO_CV:
            if pixel_format.startswith(prefix):
                return cv2.cvtColor(frame, code)
        return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)       # Mono8 etc.
    if pixel_format.startswith("RGB"):
        return cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
    return frame


def shrink(img, max_side):
    """Downscale so the longest side is max_side (YOLO resizes to 640 anyway)."""
    if not max_side:
        return img
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest <= max_side:
        return img
    f = max_side / longest
    return cv2.resize(img, (int(w * f), int(h * f)), interpolation=cv2.INTER_LINEAR)


class CameraFault(Exception):
    pass


# ----------------------------------------------------------------------------
# Model loading (Hailo-8 or CPU)
# ----------------------------------------------------------------------------
# The Hailo-8 can only be opened by one model instance at a time, so the model is loaded once and
# reused when inspection is paused/resumed (each resume starts a new worker thread).
_MODEL_CACHE = {}


def is_hailo_model(path):
    p = os.path.expanduser(str(path))
    if p.endswith(".hef"):
        return True
    if os.path.isdir(p):
        return any(f.endswith(".hef") for f in os.listdir(p))
    return False


def load_model(path):
    key = os.path.abspath(os.path.expanduser(str(path)))
    if key not in _MODEL_CACHE:
        load_path = os.path.dirname(key) if key.endswith(".hef") else key   # Hailo wants the folder
        _MODEL_CACHE[key] = YOLO(load_path)
    return _MODEL_CACHE[key]


# ----------------------------------------------------------------------------
# Can tracking -> one verdict per can ID
# ----------------------------------------------------------------------------
@dataclass
class CanState:
    first_seen: int
    last_seen: int
    frames_seen: int = 0
    defect_frames: int = 0
    max_conf: float = 0.0
    defect_classes: dict = field(default_factory=dict)   # class name -> frames seen


class CanTracker:
    def __init__(self):
        self.cans: dict[int, CanState] = {}

    def reset(self):
        self.cans.clear()

    @staticmethod
    def _center_in(det, box):
        x1, y1, x2, y2 = det["xyxy"]
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        bx1, by1, bx2, by2 = box
        return bx1 <= cx <= bx2 and by1 <= cy <= by2

    @staticmethod
    def _area(xyxy):
        x1, y1, x2, y2 = xyxy
        return max(0.0, x2 - x1) * max(0.0, y2 - y1)

    def update(self, frame_idx, dets):
        """dets: list of dicts {id, name, conf, xyxy, is_good}. Returns finalized verdicts."""
        tracked = [d for d in dets if d["id"] is not None]

        # A "can" = tracked box whose centre is not inside a larger tracked box
        cans = [d for d in tracked
                if not any(o is not d and self._area(o["xyxy"]) > self._area(d["xyxy"])
                           and self._center_in(d, o["xyxy"]) for o in tracked)]

        for can in cans:
            st = self.cans.get(can["id"])
            if st is None:
                st = self.cans[can["id"]] = CanState(first_seen=frame_idx, last_seen=frame_idx)
            st.frames_seen += 1
            st.last_seen = frame_idx

            # Defect votes: the can box itself, or any defect box centred inside it (count once/frame)
            hits = [can] if not can["is_good"] else []
            hits += [d for d in dets if d is not can and not d["is_good"]
                     and self._center_in(d, can["xyxy"])]
            if hits:
                st.defect_frames += 1
                best = max(hits, key=lambda d: d["conf"])
                st.max_conf = max(st.max_conf, best["conf"])
                st.defect_classes[best["name"]] = st.defect_classes.get(best["name"], 0) + 1

        # Finalize cans that have left the view
        done = []
        for cid, st in list(self.cans.items()):
            if frame_idx - st.last_seen > LOST_AFTER:
                del self.cans[cid]
                if st.frames_seen < MIN_TRACK_FRAMES:
                    continue
                defective = st.defect_frames >= MIN_DEFECT_FRAMES
                top = max(st.defect_classes, key=st.defect_classes.get) if st.defect_classes else ""
                done.append({
                    "id": cid, "defective": defective, "frames": st.frames_seen,
                    "defect_frames": st.defect_frames, "max_conf": st.max_conf,
                    "defect_class": top, "time": time.time(),
                })
        return done


# ----------------------------------------------------------------------------
# Camera + YOLO worker (grab thread + inference thread)
# ----------------------------------------------------------------------------
class CameraInferenceWorker(QThread):
    """Owns the PySpin camera (all Spinnaker calls stay on this QThread). A separate Python thread
    runs YOLO on the newest grabbed frame, so grabbing and inference overlap."""
    frame_ready = pyqtSignal(object, dict)  # (annotated frame or None, metrics)
    can_finalized = pyqtSignal(dict)        # one verdict per tracked can ID
    status_update = pyqtSignal(str)
    feed_state = pyqtSignal(bool)
    camera_ok = pyqtSignal()
    camera_fault = pyqtSignal(str)

    def __init__(self, model_path, conf_thresh, camera_index=0, continuous=False,
                 imgsz=DEFAULT_IMGSZ, track=True):
        super().__init__()
        self.model_path = model_path
        self.conf_thresh = conf_thresh
        self.camera_index = camera_index
        self.imgsz = imgsz
        self.track = track
        self.backend = "HAILO-8" if is_hailo_model(model_path) else "CPU"
        # Written from the GUI thread, read here (plain attribute reads are atomic in CPython)
        self.continuous = continuous
        self.object_present = False
        self.display_enabled = True
        self._running = True
        self._acquiring = False

        # Hand-off between grab and inference threads (newest frame only)
        self._slot_lock = threading.Lock()
        self._slot = None
        self._slot_event = threading.Event()
        self._infer_running = False
        self.frames_grabbed = 0
        self.frames_dropped = 0

    def _sleep(self, seconds):
        end = time.monotonic() + seconds
        while self._running and time.monotonic() < end:
            self.msleep(50)

    def _set_feed(self, cam, on):
        if on and not self._acquiring:
            cam.BeginAcquisition()
            self._acquiring = True
            self.feed_state.emit(True)
        elif not on and self._acquiring:
            self._acquiring = False
            self.feed_state.emit(False)
            cam.EndAcquisition()

    # -- main loop -----------------------------------------------------------
    def run(self):
        self.status_update.emit(f"Loading YOLO model from '{self.model_path}' ({self.backend})...")
        try:
            model = load_model(self.model_path)
            good_classes = [n for n in model.names.values() if GOOD_KEYWORD in str(n).lower()]
            self.status_update.emit(f"Model loaded on {self.backend}. Classes: {list(model.names.values())}")
            self.status_update.emit(f"Good classes: {good_classes}   Tracking: {'ON' if self.track else 'OFF'}")
        except Exception as e:
            self.status_update.emit(f"Failed to load YOLO model: {e}")
            return

        if PySpin is None:
            self.camera_fault.emit("PySpin not installed - cannot open the camera.")
            return

        system = PySpin.System.GetInstance()
        try:
            while self._running:
                cam_list = system.GetCameras()
                cam = None
                infer_thread = None
                try:
                    if cam_list.GetSize() <= self.camera_index:
                        raise CameraFault("No Teledyne/FLIR camera found. Check cable / IP config.")
                    cam = cam_list.GetByIndex(self.camera_index)
                    cam.Init()
                    self._configure_camera(cam)
                    self.status_update.emit(f"Camera {self.camera_index} initialised.")
                    self.camera_ok.emit()

                    # Start the inference thread for this camera session
                    self._infer_running = True
                    infer_thread = threading.Thread(
                        target=self._infer_loop, args=(model, good_classes), daemon=True)
                    infer_thread.start()

                    self._grab_loop(cam)                     # returns only when stopped
                except Exception as e:
                    self.camera_fault.emit(str(e))
                finally:
                    self._infer_running = False
                    self._slot_event.set()
                    if infer_thread is not None:
                        infer_thread.join(timeout=5)
                    if cam is not None:
                        try:
                            self._set_feed(cam, False)
                        except Exception:
                            pass
                        try:
                            if cam.IsInitialized():
                                cam.DeInit()
                        except Exception:
                            pass
                        del cam
                    self._acquiring = False
                    cam_list.Clear()
                    del cam_list

                if self._running:
                    self.status_update.emit(f"Retrying camera in {CAMERA_RETRY_S:.0f}s...")
                    self._sleep(CAMERA_RETRY_S)
        finally:
            try:
                system.ReleaseInstance()
            except Exception as e:
                self.status_update.emit(f"Cleanup warning: {e}")
            self.feed_state.emit(False)
            self.status_update.emit("Camera session closed.")

    # -- stage 1: grab (Spinnaker thread) ------------------------------------
    def _grab_loop(self, cam):
        consecutive_errors = 0
        try:
            self._set_feed(cam, True)
        except Exception as e:
            raise CameraFault(f"BeginAcquisition failed: {e}")
        self.status_update.emit("Acquisition started - live feed running.")

        while self._running:
            image_result = None
            try:
                image_result = cam.GetNextImage(GET_NEXT_IMAGE_TIMEOUT_MS)
                counted = self.continuous or self.object_present
                if image_result.IsIncomplete():
                    raise RuntimeError(f"incomplete image (status {image_result.GetImageStatus()})")
                try:
                    pixel_format = image_result.GetPixelFormatName()
                except Exception:
                    pixel_format = ""
                raw = image_result.GetNDArray().copy()
            except Exception as e:
                consecutive_errors += 1
                self.status_update.emit(f"Grab error {consecutive_errors}/{FAULT_AFTER_ERRORS}: {e}")
                if consecutive_errors >= FAULT_AFTER_ERRORS:
                    raise CameraFault(f"Acquisition failed {consecutive_errors}x in a row: {e}")
                continue
            finally:
                if image_result is not None:
                    try:
                        image_result.Release()
                    except Exception:
                        pass
            consecutive_errors = 0
            self.frames_grabbed += 1

            # Hand the newest frame to the inference thread (overwrite if it is still busy)
            with self._slot_lock:
                if self._slot is not None:
                    self.frames_dropped += 1
                self._slot = (raw, pixel_format, counted)
            self._slot_event.set()

    # -- stage 2: convert + YOLO + tracking (inference thread) ---------------
    def _infer_loop(self, model, good_classes):
        tracker = CanTracker()
        idx = 0
        fps, fps_frames, fps_t0 = 0.0, 0, time.monotonic()
        last_display = 0.0
        display_interval = 1.0 / DISPLAY_MAX_FPS if DISPLAY_MAX_FPS > 0 else 0.0
        use_track = self.track

        while self._infer_running and self._running:
            if not self._slot_event.wait(timeout=0.5):
                continue
            with self._slot_lock:
                item, self._slot = self._slot, None
                self._slot_event.clear()
            if item is None:
                continue
            raw, pixel_format, counted = item

            try:
                frame = shrink(to_bgr(raw, pixel_format), PROC_MAX_SIDE)

                r = None
                if use_track:
                    try:
                        r = model.track(frame, persist=True, conf=self.conf_thresh,
                                        imgsz=self.imgsz, tracker=TRACKER_CFG, verbose=False)[0]
                    except Exception as e:
                        use_track = False
                        self.status_update.emit(f"Tracking disabled ({e}); using plain detection.")
                if r is None:
                    r = model.predict(frame, conf=self.conf_thresh, imgsz=self.imgsz, verbose=False)[0]

                dets = self._extract(r, model, good_classes)
                is_defect = any(not d["is_good"] for d in dets)

                if use_track:
                    for verdict in tracker.update(idx, dets):
                        self.can_finalized.emit(verdict)

                # FPS
                fps_frames += 1
                elapsed = time.monotonic() - fps_t0
                if elapsed >= FPS_WINDOW_S:
                    fps = fps_frames / elapsed
                    fps_frames, fps_t0 = 0, time.monotonic()

                # Only draw frames that will actually be shown
                annotated = None
                now = time.monotonic()
                if self.display_enabled and now - last_display >= display_interval:
                    last_display = now
                    annotated = self._draw(frame, dets, idx, is_defect)

                metrics = {
                    "frame_idx": idx,
                    "is_defect": is_defect,
                    "detections": [(d["name"], d["conf"], d["is_good"], d["id"]) for d in dets],
                    "counted": counted,
                    "time": time.time(),
                    "fps": fps,
                    "infer_ms": float(r.speed.get("inference", 0.0)),
                    "backend": self.backend,
                    "active_ids": len(tracker.cans),
                    "dropped": self.frames_dropped,
                }
                self.frame_ready.emit(annotated, metrics)
                idx += 1
            except Exception as e:
                self.status_update.emit(f"Inference error: {e}")

    @staticmethod
    def _extract(r, model, good_classes):
        boxes = r.boxes
        if boxes is None or len(boxes) == 0:
            return []
        xyxy = boxes.xyxy.cpu().numpy()
        confs = boxes.conf.cpu().numpy()
        clss = boxes.cls.cpu().numpy().astype(int)
        ids = boxes.id.cpu().numpy().astype(int) if boxes.id is not None else [None] * len(clss)
        out = []
        for b, c, k, i in zip(xyxy, confs, clss, ids):
            name = model.names[int(k)]
            out.append({"xyxy": tuple(float(v) for v in b), "conf": float(c), "name": name,
                        "is_good": name in good_classes, "id": None if i is None else int(i)})
        return out

    def _configure_camera(self, cam):
        """Continuous acquisition + newest-only buffering so YOLO never lags behind the line."""
        try:
            cam.AcquisitionMode.SetValue(PySpin.AcquisitionMode_Continuous)
        except Exception as e:
            self.status_update.emit(f"Could not set continuous mode: {e}")
        try:
            s_nodemap = cam.GetTLStreamNodeMap()
            handling = PySpin.CEnumerationPtr(s_nodemap.GetNode("StreamBufferHandlingMode"))
            newest = handling.GetEntryByName("NewestOnly")
            handling.SetIntValue(newest.GetValue())
        except Exception as e:
            self.status_update.emit(f"Could not set NewestOnly buffering: {e}")

    # -- drawing -------------------------------------------------------------
    @staticmethod
    def _draw(frame, dets, idx, is_defect):
        out = frame.copy()
        h, w = out.shape[:2]
        scale = max(h, w) / 1000
        thick = max(2, int(round(3 * scale)))
        font_scale = max(0.5, 0.7 * scale)
        FONT = cv2.FONT_HERSHEY_SIMPLEX
        GREEN, RED, GRAY = (0, 200, 0), (0, 0, 255), (110, 110, 110)
        txt_thick = max(1, thick - 1)

        for d in dets:
            x1, y1, x2, y2 = map(int, d["xyxy"])
            color = GREEN if d["is_good"] else RED
            cv2.rectangle(out, (x1, y1), (x2, y2), color, thick)
            tag = f"#{d['id']} " if d["id"] is not None else ""
            text = f"{tag}{d['name']} {d['conf']:.2f}"
            (tw, th), base = cv2.getTextSize(text, FONT, font_scale, txt_thick)
            top = max(0, y1 - th - base - 6)
            cv2.rectangle(out, (x1, top), (x1 + tw + 8, top + th + base + 6), color, -1)
            cv2.putText(out, text, (x1 + 4, top + th + 2), FONT, font_scale,
                        (255, 255, 255), txt_thick, cv2.LINE_AA)

        if not dets:
            banner, color = f"Frame {idx}: No Detection", GRAY
        else:
            banner = f"Frame {idx}: {'DEFECT' if is_defect else 'GOOD'} ({len(dets)} objects)"
            color = RED if is_defect else GREEN
        (tw, th), base = cv2.getTextSize(banner, FONT, font_scale, txt_thick)
        cv2.rectangle(out, (0, 0), (tw + 16, th + base + 16), color, -1)
        cv2.putText(out, banner, (8, th + 8), FONT, font_scale, (255, 255, 255), txt_thick, cv2.LINE_AA)
        return out

    def stop(self):
        self._running = False
        self._infer_running = False
        self._slot_event.set()
        self.wait()


# ----------------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self, model_path, conf_thresh, continuous=False, imgsz=DEFAULT_IMGSZ, track=True):
        super().__init__()
        self.backend = "HAILO-8" if is_hailo_model(model_path) else "CPU"
        self.setWindowTitle(f"Yards Can Inspection Station  [{self.backend}]")
        self.resize(1400, 860)

        self.model_path = model_path
        self.conf_thresh = conf_thresh
        self.continuous = continuous
        self.imgsz = imgsz
        self.track = track

        # Hardware
        self.gpio = GPIO()
        self.output_map = {o[0]: o for o in OUTPUTS}
        self.light_states = {o[0]: False for o in OUTPUTS}
        self.auto_lights = True
        self.sensor_state = "UNKNOWN"
        self.object_present = False

        # System state that drives the auto stack light
        self.camera_ready = False
        self.camera_faulted = False
        self.defect_in_view = False
        self.clean_frames = 0

        # Per-can accumulation (sensor counters)
        self.part_active = False
        self.part_frames = 0
        self.part_defect = False
        self.part_detected = False

        # Counters
        self.total_frames = 0
        self.cans_total = 0
        self.cans_good = 0
        self.cans_defect = 0
        self.cans_none = 0

        # Tracked-can counters (by ID)
        self.track_total = 0
        self.track_good = 0
        self.track_defect = 0

        # Object pass counter (GPIO 25)
        self.pass_count = 0
        self.flickers_ignored = 0
        self.pass_times = deque()
        self.last_pass_wall = None

        # Latest metrics for the timed UI refresh
        self._last_metrics = None
        self._live_status = None
        self._pred_buffer = []
        self._last_frame = None

        # Timers
        self.clear_timer = QTimer(self, singleShot=True)
        self.clear_timer.timeout.connect(self._object_left)
        self.rate_timer = QTimer(self)
        self.rate_timer.timeout.connect(self._refresh_pass_counter)
        self.rate_timer.start(1000)
        self.buzzer_timer = QTimer(self, singleShot=True)
        self.buzzer_timer.timeout.connect(lambda: self.set_light("buzzer", False))
        self.chase_timer = QTimer(self)
        self.chase_timer.timeout.connect(self._chase_step)
        self.chase_step = 0
        self.log_timer = QTimer(self)
        self.log_timer.timeout.connect(self._flush_pred_log)
        self.log_timer.start(LOG_FLUSH_MS)
        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self._refresh_ui)
        self.ui_timer.start(UI_REFRESH_MS)

        self._build_ui()

        if self.gpio.available:
            self.gpio.setup_input(SENSOR_PIN)
            self.log_message(f"GPIO ready ({self.gpio.backend}). Sensor on GPIO {SENSOR_PIN} "
                             f"(pin {SENSOR_PHYSICAL_PIN}), active {SENSOR_ACTIVE_STATE}.")
            if self.gpio.backend == "pinctrl":
                self.log_message("TIP: 'pip install gpiod' makes sensor polling much cheaper.")
        else:
            self.log_message("WARNING: no GPIO backend (gpiod/pinctrl) - lights/sensor shown in UI only.")
        self.all_lights_off(force=True)

        # Sensor thread
        self.sensor_monitor = SensorMonitor(self.gpio, SENSOR_PIN)
        self.sensor_monitor.state_changed.connect(self.on_sensor_changed)
        if self.gpio.available:
            self.sensor_monitor.start()

        # Camera thread
        self.worker = None
        self._start_worker()

    # -- UI construction -----------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)

        left = QVBoxLayout()
        view_bar = QHBoxLayout()
        self.btn_view_live = QPushButton("Live View")
        self.btn_view_log = QPushButton("Prediction Log")
        self.view_group = QButtonGroup(self)
        self.view_group.setExclusive(True)
        for i, b in enumerate((self.btn_view_live, self.btn_view_log)):
            b.setCheckable(True)
            b.setStyleSheet(VIEW_BTN)
            self.view_group.addButton(b, i)
            view_bar.addWidget(b)
        self.btn_view_live.setChecked(True)
        self.view_group.idClicked.connect(self.set_view)
        view_bar.addStretch()

        view_bar.addWidget(QLabel("Log:"))
        self.cmb_log_filter = QComboBox()
        self.cmb_log_filter.addItems(["All frames", "Detections only", "Defects only", "Can verdicts only"])
        self.cmb_log_filter.setCurrentIndex(3)   # verdicts only by default: cheapest
        view_bar.addWidget(self.cmb_log_filter)
        self.btn_log_clear = QPushButton("Clear Log")
        self.btn_log_clear.clicked.connect(self.clear_prediction_log)
        self.btn_log_save = QPushButton("Save Log...")
        self.btn_log_save.clicked.connect(self.save_prediction_log)
        view_bar.addWidget(self.btn_log_clear)
        view_bar.addWidget(self.btn_log_save)
        left.addLayout(view_bar)

        self.video_label = QLabel("Waiting for camera / sensor trigger...")
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setStyleSheet(
            "background-color: #111; color: #fff; font-size: 16px; border-radius: 4px;")
        self.video_label.setMinimumSize(850, 650)

        self.pred_log = QPlainTextEdit(readOnly=True)
        self.pred_log.setMaximumBlockCount(PRED_LOG_MAX_LINES)
        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        mono.setPointSize(11)
        self.pred_log.setFont(mono)
        self.pred_log.setStyleSheet(
            "background-color: #111; color: #e0e0e0; border-radius: 4px; padding: 6px;")
        self.pred_log.setMinimumSize(850, 650)

        self.view_stack = QStackedWidget()
        self.view_stack.addWidget(self.video_label)   # index 0
        self.view_stack.addWidget(self.pred_log)      # index 1
        left.addWidget(self.view_stack, stretch=1)
        main_layout.addLayout(left, stretch=3)

        QShortcut(QKeySequence("Ctrl+Tab"), self, activated=lambda: self.set_view(
            1 - self.view_stack.currentIndex()))

        right = QVBoxLayout()

        # Inspection controls
        ctrl = QGroupBox("Inspection Controls")
        cl = QVBoxLayout(ctrl)
        self.btn_toggle = QPushButton("Stop Inspection")
        self.btn_toggle.setStyleSheet(RED_BTN)
        self.btn_toggle.clicked.connect(self.toggle_inspection)
        cl.addWidget(self.btn_toggle)

        cl.addWidget(QLabel("Trigger mode:"))
        self.cmb_trigger = QComboBox()
        self.cmb_trigger.addItems([f"Sensor (GPIO {SENSOR_PIN})", "Continuous (bench test)"])
        self.cmb_trigger.setCurrentIndex(1 if self.continuous else 0)
        self.cmb_trigger.currentIndexChanged.connect(self.on_trigger_mode_changed)
        cl.addWidget(self.cmb_trigger)

        self.lbl_conf_val = QLabel(f"Confidence threshold: {self.conf_thresh:.2f}")
        cl.addWidget(self.lbl_conf_val)
        self.slider_conf = QSlider(Qt.Orientation.Horizontal)
        self.slider_conf.setRange(1, 99)
        self.slider_conf.setValue(int(self.conf_thresh * 100))
        self.slider_conf.valueChanged.connect(self.update_confidence)
        cl.addWidget(self.slider_conf)
        right.addWidget(ctrl)

        # Sensor / feed / camera status
        sens = QGroupBox("Sensor, Camera & Feed")
        sl = QVBoxLayout(sens)
        self.lbl_sensor = QLabel(f"SENSOR GPIO {SENSOR_PIN} (pin {SENSOR_PHYSICAL_PIN}): INITIALISING...")
        self.lbl_camera = QLabel("CAMERA: CONNECTING...")
        self.lbl_feed = QLabel("FEED: IDLE")
        for lbl in (self.lbl_sensor, self.lbl_camera, self.lbl_feed):
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._badge(lbl, "#34495e")
            sl.addWidget(lbl)
        right.addWidget(sens)

        # Object pass counter
        counter = QGroupBox(f"Object Counter (GPIO {SENSOR_PIN} / pin {SENSOR_PHYSICAL_PIN})")
        
        ctl = QVBoxLayout(counter)
        self.lbl_pass_count = QLabel("0")
        self.lbl_pass_count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pass_count_style = ("font-size: 40px; font-weight: bold; color: white;"
                                  " background-color: #2a3242; border-radius: 6px; padding: 4px;")
        self.lbl_pass_count.setStyleSheet(self._pass_count_style)
        ctl.addWidget(self.lbl_pass_count)
        self.lbl_pass_info = QLabel()
        self.lbl_pass_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ctl.addWidget(self.lbl_pass_info)
        self.btn_pass_reset = QPushButton("Reset Object Count")
        self.btn_pass_reset.clicked.connect(self.reset_pass_counter)
        ctl.addWidget(self.btn_pass_reset)
        right.addWidget(counter)

        # Stack light
        lights = QGroupBox("Stack Light")
        ll = QVBoxLayout(lights)
        self.cmb_light_mode = QComboBox()
        self.cmb_light_mode.addItems(["Auto (system state)", "Manual"])
        self.cmb_light_mode.currentIndexChanged.connect(self.on_light_mode_changed)
        ll.addWidget(self.cmb_light_mode)

        grid = QGridLayout()
        self.indicators, self.light_buttons = {}, {}
        for row, (key, label, pin, color) in enumerate(OUTPUTS):
            ind = QLabel("●")
            ind.setStyleSheet("color: #4f5b6e; font-size: 20px;")
            grid.addWidget(ind, row, 0)
            grid.addWidget(QLabel(f"{label}  (GPIO {pin})"), row, 1)
            btn = QPushButton("OFF")
            btn.setFixedWidth(70)
            btn.clicked.connect(lambda _=False, k=key: self.set_light(k, not self.light_states[k]))
            grid.addWidget(btn, row, 2)
            self.indicators[key] = ind
            self.light_buttons[key] = btn
        ll.addLayout(grid)

        row_btns = QHBoxLayout()
        self.btn_chase = QPushButton("Test Chase (R→Y→G)")
        self.btn_chase.clicked.connect(self.toggle_chase)
        self.btn_all_off = QPushButton("All Off")
        self.btn_all_off.clicked.connect(lambda: self.all_lights_off())
        row_btns.addWidget(self.btn_chase)
        row_btns.addWidget(self.btn_all_off)
        ll.addLayout(row_btns)
        right.addWidget(lights)

        # Metrics
        stats = QGroupBox("Live Metrics")
        stl = QVBoxLayout(stats)
        self.lbl_perf = QLabel(f"Backend: {self.backend}   FPS: --   Inference: -- ms")
        self.lbl_perf.setStyleSheet("font-weight: bold; color: #5bc0de;")
        self.lbl_tracked = QLabel()
        self.lbl_tracked.setStyleSheet("font-weight: bold;")
        self.lbl_cans = QLabel()
        self.lbl_frames = QLabel()
        self.lbl_live = QLabel("Live: --")
        self.lbl_live.setStyleSheet("font-size: 15px; font-weight: bold;")
        self.lbl_verdict = QLabel("Last can: --")
        self.lbl_verdict.setStyleSheet("font-size: 15px; font-weight: bold;")
        for w in (self.lbl_perf, self.lbl_tracked, self.lbl_cans, self.lbl_frames,
                  self.lbl_live, self.lbl_verdict):
            stl.addWidget(w)
        self.btn_reset = QPushButton("Reset Counters")
        self.btn_reset.clicked.connect(self.reset_counters)
        stl.addWidget(self.btn_reset)
        right.addWidget(stats)

        # Log
        log = QGroupBox("System Log")
        lgl = QVBoxLayout(log)
        self.console_log = QTextEdit(readOnly=True)
        self.console_log.document().setMaximumBlockCount(500)
        lgl.addWidget(self.console_log)
        right.addWidget(log, stretch=1)

        main_layout.addLayout(right, stretch=1)
        self._refresh_counters()
        self._refresh_pass_counter()
        self._update_light_buttons_enabled()

    @staticmethod
    def _badge(label, bg):
        label.setStyleSheet(f"background-color: {bg}; color: white; font-weight: bold; padding: 6px;")

    # -- worker lifecycle ----------------------------------------------------
    def _start_worker(self):
        self.worker = CameraInferenceWorker(self.model_path, self.conf_thresh,
                                            continuous=self.continuous, imgsz=self.imgsz,
                                            track=self.track)
        self.worker.object_present = self.object_present
        self.worker.display_enabled = self.view_stack.currentIndex() == 0
        self.worker.frame_ready.connect(self.handle_new_frame)
        self.worker.can_finalized.connect(self.on_can_finalized)
        self.worker.status_update.connect(self.log_message)
        self.worker.feed_state.connect(self.on_feed_state)
        self.worker.camera_ok.connect(self.on_camera_ok)
        self.worker.camera_fault.connect(self.on_camera_fault)
        self.lbl_camera.setText("CAMERA: CONNECTING...")
        self._badge(self.lbl_camera, "#34495e")
        self.worker.start()

    def toggle_inspection(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self._finish_part(discard=True)
            self.camera_ready = False
            self.camera_faulted = False
            self.defect_in_view = False
            self.lbl_camera.setText("CAMERA: STOPPED")
            self._badge(self.lbl_camera, "#34495e")
            self.apply_auto_lights()
            self.btn_toggle.setText("Resume Inspection")
            self.btn_toggle.setStyleSheet(GREEN_BTN)
            self.log_message("Inspection paused.")
        else:
            self._start_worker()
            self.btn_toggle.setText("Stop Inspection")
            self.btn_toggle.setStyleSheet(RED_BTN)
            self.log_message("Inspection resumed.")

    # -- camera health -------------------------------------------------------
    def on_camera_ok(self):
        if self.camera_faulted:
            self.log_message("Camera recovered.")
            self._log_event("CAMERA RECOVERED", "#27ae60")
        self.camera_ready = True
        self.camera_faulted = False
        self.buzzer_timer.stop()
        self.lbl_camera.setText("CAMERA: OK")
        self._badge(self.lbl_camera, "#27ae60")
        self.apply_auto_lights()

    def on_camera_fault(self, msg):
        self.log_message(f"CAMERA FAULT: {msg}")
        self.lbl_camera.setText("CAMERA: FAULT")
        self._badge(self.lbl_camera, "#c0392b")
        first = not self.camera_faulted
        if first:
            self._log_event(f"CAMERA FAULT: {msg}", "#e74c3c")
        self.camera_ready = False
        self.camera_faulted = True
        self.defect_in_view = False
        self._finish_part(discard=True)
        self.apply_auto_lights()
        if first and self.auto_lights:
            self.set_light("buzzer", True)
            if BUZZER_FAULT_MS > 0:
                self.buzzer_timer.start(BUZZER_FAULT_MS)

    # -- sensor / trigger ----------------------------------------------------
    def on_sensor_changed(self, state):
        self.sensor_state = state
        present = state == SENSOR_ACTIVE_STATE
        bg = "#c0392b" if present else ("#27ae60" if state != "UNKNOWN" else "#7f8c8d")
        self.lbl_sensor.setText(f"SENSOR GPIO {SENSOR_PIN} (pin {SENSOR_PHYSICAL_PIN}): {state}"
                                + ("  (OBJECT PRESENT)" if present else ""))
        self._badge(self.lbl_sensor, bg)
        if state == "UNKNOWN":
            self.log_message("WARNING: sensor read returned UNKNOWN.")
            return

        if present:
            if self.clear_timer.isActive():
                self.clear_timer.stop()
                self.flickers_ignored += 1
                self._refresh_pass_counter()
                return
            if not self.object_present:
                self._object_arrived()
        else:
            if self.object_present and not self.clear_timer.isActive():
                self.clear_timer.start(PASS_REARM_MS)

    def _object_arrived(self):
        global GLOBAL_OBJECT_COUNTER
        self.object_present = True
        if self.worker:
            self.worker.object_present = True

        self.pass_count += 1
        GLOBAL_OBJECT_COUNTER += 1
        now = time.monotonic()
        self.pass_times.append(now)
        self.last_pass_wall = datetime.now()
        self._refresh_pass_counter()
        self._flash_pass_counter()
        self._log_event(f"SENSOR PASS #{self.pass_count}", "#5bc0de")

        if not self.continuous:
            self._start_part()

    def _object_left(self):
        self.object_present = False
        if self.worker:
            self.worker.object_present = False
        if not self.continuous:
            self._finish_part()

    # -- object pass counter ---------------------------------------------------
    def _refresh_pass_counter(self):
        now = time.monotonic()
        while self.pass_times and now - self.pass_times[0] > PASS_RATE_WINDOW_S:
            self.pass_times.popleft()
        rate = len(self.pass_times) * 60.0 / PASS_RATE_WINDOW_S
        last = self.last_pass_wall.strftime("%H:%M:%S") if self.last_pass_wall else "--"
        self.lbl_pass_count.setText(str(self.pass_count))
        self.lbl_pass_info.setText(
            f"Rate: {rate:.0f}/min   Last: {last}   Flicker ignored: {self.flickers_ignored}")

    def _flash_pass_counter(self):
        self.lbl_pass_count.setStyleSheet(self._pass_count_style.replace("#2a3242", "#2980b9"))
        QTimer.singleShot(200, lambda: self.lbl_pass_count.setStyleSheet(self._pass_count_style))

    def reset_pass_counter(self):
        global GLOBAL_OBJECT_COUNTER
        global ATTEMPTED_REQUESTS
        self.pass_count = 0
        GLOBAL_OBJECT_COUNTER = 0
        ATTEMPTED_REQUESTS.clear()
        self.flickers_ignored = 0
        self.pass_times.clear()
        self.last_pass_wall = None
        self._refresh_pass_counter()
        self.log_message("Object count reset.")

    def on_trigger_mode_changed(self, index):
        self.continuous = index == 1
        if self.worker:
            self.worker.continuous = self.continuous
        self._finish_part(discard=True)
        self._set_defect_in_view(False)
        self.log_message(f"Trigger mode: {'CONTINUOUS' if self.continuous else 'SENSOR'}")

    def on_feed_state(self, active):
        self.lbl_feed.setText("FEED: LIVE" if active else "FEED: IDLE")
        self._badge(self.lbl_feed, "#2980b9" if active else "#34495e")
        if not active:
            self._set_defect_in_view(False)
            self._live_status = None
            self.lbl_live.setText("Live: --")
            self.lbl_live.setStyleSheet("font-size: 15px; font-weight: bold;")
            self.video_label.clear()
            self.video_label.setText("Camera feed not running (inspection stopped or camera fault).")

    # -- per-can verdict (sensor counters) -----------------------------------
    def _start_part(self):
        self.part_active = True
        self.part_frames = 0
        self.part_defect = False
        self.part_detected = False

    def _finish_part(self, discard=False):
        if not self.part_active:
            return
        self.part_active = False
        if discard:
            return

        if self.part_defect:
            verdict, color = "DEFECT", "#f0ad4e"
            self.cans_defect += 1
        elif self.part_detected:
            verdict, color = "GOOD", "#5cb85c"
            self.cans_good += 1
        else:
            verdict, color = "NO DETECTION", "#aaaaaa"
            self.cans_none += 1
        self.cans_total += 1

        self.lbl_verdict.setText(f"Last can: {verdict}  ({self.part_frames} frames)")
        self.lbl_verdict.setStyleSheet(f"color: {color}; font-size: 15px; font-weight: bold;")
        self.log_message(f"Can #{self.cans_total}: {verdict} ({self.part_frames} frames)")
        self._log_event(f"CAN #{self.cans_total}: {verdict} ({self.part_frames} frames)", color)
        self._refresh_counters()

    # -- per-can verdict (tracked IDs) ---------------------------------------
    def on_can_finalized(self, v):
        self.track_total += 1
        if v["defective"]:
            self.track_defect += 1
            verdict, color = "DEFECT", "#f0ad4e"
            detail = f"{v['defect_class']} {v['max_conf']:.2f}, {v['defect_frames']}/{v['frames']} frames"
        else:
            self.track_good += 1
            verdict, color = "GOOD", "#5cb85c"
            detail = f"{v['defect_frames']}/{v['frames']} defect frames"
        msg = f"CAN ID {v['id']}: {verdict} ({detail})"
        self.log_message(msg)
        self._log_event(msg, color)
        # send_result(v["id"], v["defective"]) would go here (PLC, database, reject gate, ...)

    # -- frames --------------------------------------------------------------
    def handle_new_frame(self, frame, metrics):
        """Runs for every inferred frame: keep it light. Labels refresh on a timer."""
        self.total_frames += 1
        is_defect = metrics["is_defect"]
        has_det = bool(metrics["detections"])
        self._last_metrics = metrics

        # Stack light follows what is in view right now
        if is_defect:
            send_rejection()
            self.clean_frames = 0
            self._set_defect_in_view(True)
            # add rejection method here
        else:
            self.clean_frames += 1
            if self.clean_frames >= DEFECT_CLEAR_FRAMES:
                self._set_defect_in_view(False)

        # Per-can tally for the sensor counters
        if not self.continuous and self.part_active and metrics.get("counted"):
            self.part_frames += 1
            self.part_defect |= is_defect
            self.part_detected |= has_det

        # Live label: only touch the widget when the status changes
        status = "defect" if is_defect else ("good" if has_det else "none")
        if status != self._live_status:
            self._live_status = status
            if status == "defect":
                self.lbl_live.setText("Live: ⚠️ DEFECT IN VIEW")
                self.lbl_live.setStyleSheet("color: #f0ad4e; font-size: 15px; font-weight: bold;")
            elif status == "good":
                self.lbl_live.setText("Live: ✔️ GOOD CAN")
                self.lbl_live.setStyleSheet("color: #5cb85c; font-size: 15px; font-weight: bold;")
            else:
                self.lbl_live.setText("Live: no detection")
                self.lbl_live.setStyleSheet("color: #aaa; font-size: 15px; font-weight: bold;")

        self._log_prediction(metrics)

        if frame is not None:
            self._last_frame = frame
            if self.view_stack.currentIndex() == 0:
                self._show_frame(frame)

    def _refresh_ui(self):
        m = self._last_metrics
        if m is not None:
            self.lbl_perf.setText(
                f"Backend: {m.get('backend', self.backend)}   FPS: {m.get('fps', 0.0):.1f}   "
                f"Inference: {m.get('infer_ms', 0.0):.1f} ms   Cans in view: {m.get('active_ids', 0)}")
        self._refresh_counters()

    # -- view switching / prediction log ---------------------------------------
    def set_view(self, index):
        self.view_stack.setCurrentIndex(index)
        (self.btn_view_live if index == 0 else self.btn_view_log).setChecked(True)
        if self.worker:
            self.worker.display_enabled = index == 0     # skip drawing while the log is shown
        if index == 0 and self._last_frame is not None and self.lbl_feed.text() == "FEED: LIVE":
            self._show_frame(self._last_frame)

    def _pred_line(self, html_text):
        self._pred_buffer.append(html_text)

    def _flush_pred_log(self):
        if self._pred_buffer:
            self.pred_log.appendHtml("<br>".join(self._pred_buffer))
            self._pred_buffer.clear()

    def _log_prediction(self, metrics):
        mode = self.cmb_log_filter.currentIndex()   # 0 all, 1 detections, 2 defects, 3 verdicts only
        dets = metrics["detections"]
        is_defect = metrics["is_defect"]
        if mode == 3 or (mode == 1 and not dets) or (mode == 2 and not is_defect):
            return

        ts = datetime.fromtimestamp(metrics.get("time", time.time())).strftime("%H:%M:%S.%f")[:-3]
        if is_defect:
            status, color = "DEFECT", "#f0ad4e"
        elif dets:
            status, color = "GOOD", "#5cb85c"
        else:
            status, color = "NONE", "#777777"

        parts = []
        for name, conf, is_good, tid in dets:
            c = "#5cb85c" if is_good else "#f0ad4e"
            tag = f"#{tid} " if tid is not None else ""
            parts.append(f'<span style="color:{c}">{tag}{html.escape(str(name))}</span> {conf:.2f}')
        det_txt = ", ".join(parts) if parts else '<span style="color:#777">no detections</span>'
        nb = lambda s: s.replace(" ", "&nbsp;")
        frame_cell = nb(f"frame {metrics['frame_idx']:>6}")
        status_cell = nb(status.ljust(6))

        self._pred_line(
            f'<span style="color:#8e9aaf">[{ts}]&nbsp;{frame_cell}</span>'
            f'&nbsp;&nbsp;<b style="color:{color}">{status_cell}</b>&nbsp;&nbsp;{det_txt}'
        )

    def _log_event(self, text, color):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._pred_line(f'<b style="color:{color}">[{ts}] ==== {html.escape(text)} ====</b>')

    def clear_prediction_log(self):
        self._pred_buffer.clear()
        self.pred_log.clear()

    def save_prediction_log(self):
        self._flush_pred_log()
        default = f"predictions_{datetime.now():%Y%m%d_%H%M%S}.txt"
        path, _ = QFileDialog.getSaveFileName(self, "Save prediction log", default, "Text files (*.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.pred_log.toPlainText())
            self.log_message(f"Prediction log saved to {path}")
        except Exception as e:
            self.log_message(f"Could not save prediction log: {e}")

    def _set_defect_in_view(self, value):
        if value != self.defect_in_view:
            self.defect_in_view = value
            self.apply_auto_lights()

    def _show_frame(self, frame):
        # BGR888 straight into Qt (no colour conversion); copy() so the QImage owns its buffer
        frame = np.ascontiguousarray(frame)
        h, w, ch = frame.shape
        qimg = QImage(frame.data, w, h, ch * w, QImage.Format.Format_BGR888).copy()
        self.video_label.setPixmap(QPixmap.fromImage(qimg).scaled(
            self.video_label.width(), self.video_label.height(),
            Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation))

    def _refresh_counters(self):
        rate = (100.0 * self.cans_defect / self.cans_total) if self.cans_total else 0.0
        self.lbl_cans.setText(
            f"Sensor cans: {self.cans_total}   Good: {self.cans_good}   "
            f"Defect: {self.cans_defect}   None: {self.cans_none}   ({rate:.1f}% defect)")
        if self.track:
            trate = (100.0 * self.track_defect / self.track_total) if self.track_total else 0.0
            self.lbl_tracked.setText(
                f"Tracked cans (by ID): {self.track_total}   Good: {self.track_good}   "
                f"Defect: {self.track_defect}   ({trate:.1f}% defect)")
        else:
            self.lbl_tracked.setText("Tracked cans: tracking off (--no-track)")
        self.lbl_frames.setText(f"Frames processed: {self.total_frames}")

    def reset_counters(self):
        self.total_frames = self.cans_total = self.cans_good = self.cans_defect = self.cans_none = 0
        self.track_total = self.track_good = self.track_defect = 0
        self.lbl_verdict.setText("Last can: --")
        self.lbl_verdict.setStyleSheet("font-size: 15px; font-weight: bold;")
        self._refresh_counters()
        self.log_message("Counters reset.")

    # -- stack light ---------------------------------------------------------
    def apply_auto_lights(self):
        if not self.auto_lights:
            return
        if self.camera_faulted:
            self.set_light("red", True)
            self.set_light("yellow", False)
            self.set_light("green", False)
        elif self.camera_ready:
            self.set_light("red", False)
            self.set_light("buzzer", False)
            self.set_light("yellow", self.defect_in_view)
            self.set_light("green", not self.defect_in_view)
        else:
            for key in ("red", "yellow", "green", "buzzer"):
                self.set_light(key, False)

    def set_light(self, key, on, force=False):
        _, _, pin, color = self.output_map[key]
        if self.light_states[key] == on and not force:
            return
        self.gpio.write(pin, on)
        self.light_states[key] = on
        self.indicators[key].setStyleSheet(f"color: {color if on else '#4f5b6e'}; font-size: 20px;")
        btn = self.light_buttons[key]
        btn.setText("ON" if on else "OFF")
        btn.setStyleSheet(f"background-color: {color}; color: white; font-weight: bold;" if on else "")

    def all_lights_off(self, force=False):
        self.chase_timer.stop()
        self.btn_chase.setText("Test Chase (R→Y→G)")
        self.buzzer_timer.stop()
        for key in self.light_states:
            self.set_light(key, False, force=force)

    def on_light_mode_changed(self, index):
        self.auto_lights = index == 0
        self.all_lights_off()
        self._update_light_buttons_enabled()
        self.apply_auto_lights()
        self.log_message(f"Stack light mode: {'AUTO' if self.auto_lights else 'MANUAL'}")

    def _update_light_buttons_enabled(self):
        manual = not self.auto_lights
        for btn in self.light_buttons.values():
            btn.setEnabled(manual)
        self.btn_chase.setEnabled(manual)
        self.btn_all_off.setEnabled(manual)

    def toggle_chase(self):
        if self.chase_timer.isActive():
            self.all_lights_off()
        else:
            self.chase_step = 0
            self._chase_step()
            self.chase_timer.start(CHASE_STEP_MS)
            self.btn_chase.setText("Stop Chase")

    def _chase_step(self):
        seq = ["red", "yellow", "green"]
        on = seq[self.chase_step % 3]
        for key in seq:
            self.set_light(key, key == on)
        self.chase_step += 1

    # -- misc ----------------------------------------------------------------
    def update_confidence(self, val):
        self.conf_thresh = val / 100.0
        self.lbl_conf_val.setText(f"Confidence threshold: {self.conf_thresh:.2f}")
        if self.worker:
            self.worker.conf_thresh = self.conf_thresh

    def log_message(self, msg):
        self.console_log.append(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def closeEvent(self, event):
        if self.worker:
            self.worker.stop()
        if self.sensor_monitor.isRunning():
            self.sensor_monitor.stop()
        self.all_lights_off(force=True)
        self.gpio.close()
        event.accept()


def main():
    global PROC_MAX_SIDE, DISPLAY_MAX_FPS
    parser = argparse.ArgumentParser(description="Can inspection station: YOLO (Hailo-8) + sensor + stack light")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="Hailo export folder (best.hef + metadata.yaml), a .hef, or a .pt for CPU")
    parser.add_argument("--conf", type=float, default=0.25, help="Detection confidence threshold")
    parser.add_argument("--imgsz", type=int, default=DEFAULT_IMGSZ,
                        help="Inference size; for Hailo it must match the export size")
    parser.add_argument("--continuous", action="store_true",
                        help="Start in continuous mode (ignore the sensor) for bench testing")
    parser.add_argument("--no-track", action="store_true", help="Disable can ID tracking")
    parser.add_argument("--proc-size", type=int, default=PROC_MAX_SIDE,
                        help="Downscale camera frames to this longest side before YOLO (0 = off)")
    parser.add_argument("--display-fps", type=int, default=DISPLAY_MAX_FPS,
                        help="Max UI display rate (inference still runs on every frame)")
    args = parser.parse_args()

    PROC_MAX_SIDE = args.proc_size
    DISPLAY_MAX_FPS = args.display_fps

    app = QApplication(sys.argv)
    window = MainWindow(args.model, args.conf, continuous=args.continuous,
                        imgsz=args.imgsz, track=not args.no_track)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
