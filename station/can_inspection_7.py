"""
can_inspection_station_hailo_hough.py - Inspection station UI (live view, log, GPIO 25 sensor, stack light)
with YOLO on the Hailo-8 AI HAT, a faster pipeline, and per-can IDs from Hough circle detection.

Hough can IDs (new, default: --id-mode hough) - built for a fast belt
  * Hough runs in its OWN thread on EVERY camera frame (not only the frames YOLO gets to), on a cheap
    grayscale taken straight from the raw sensor data and shrunk to 320 px wide (--hough-width).
  * Constant-velocity tracking: a can may move most of a diameter per frame and keep its ID; a can
    that is lost right at the line is still counted from its motion.
  * YOLO defects are matched to the can that was at that spot IN THE SAME FRAME (position history).
    A can is counted at the line; its GOOD/DEFECT verdict comes when it leaves the view and YOLO has
    processed its last frame (max 0.4 s after the line), so YOLO's whole look at the can counts.
  * Camera options for speed: --exposure (no motion blur), --cam-fps, --binning 2.
  * Every processed frame is also run through cv2.HoughCircles (on a 640 px wide copy) to find the
    can rims. A centroid tracker keeps one ID per can while it moves along the belt.
  * Each YOLO defect box is assigned to the can whose circle contains its centre (defect vote).
  * A virtual trigger line (--line, fraction of image width) replaces the photoelectric sensor:
    when a can's centre crosses it, that can gets ONE verdict
    (DEFECT if >= HOUGH_MIN_DEFECT_FRAMES frames showed a defect on it, otherwise GOOD).
  * Trigger mode "Vision (Hough line)" (default, --trigger vision): the line crossing counts the
    object (object counter + ACN) and a DEFECT verdict sends the rejection packet for that ACN.
    The GPIO 25 sensor is still shown in the UI but no longer counts cans in this mode.
  * View "Hough Circles" shows only the detected can rims as white circles on black.
  * View "Hough Can Log" lists every Hough verdict (time, can ID, GOOD/DEFECT) in every trigger
    mode, independent of the sensor. The same lines are appended to hough_can_log.csv (--hough-log).
  * --id-mode bytetrack keeps the old ByteTrack IDs; --id-mode off (or --no-track) disables IDs.

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
    python can_inspection_station_hailo_hough.py                        # Hough IDs + vision trigger
    python can_inspection_station_hailo_hough.py --trigger sensor       # Hough IDs, GPIO 25 counts cans
    python can_inspection_station_hailo_hough.py --continuous           # bench test, no sensor
    python can_inspection_station_hailo_hough.py --line 0.6 --direction -1   # line position / belt direction
    python can_inspection_station_hailo_hough.py --id-mode bytetrack    # old ByteTrack IDs
    python can_inspection_station_hailo_hough.py --no-track             # no can IDs
    python can_inspection_station_hailo_hough.py --model best.pt        # CPU model, for comparison
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

# --- Hough circle can IDs (vision-only trigger) ---
# Radii / distances are REFERENCE px = the camera image scaled to HOUGH_PROC_WIDTH wide.
# (These are the numbers shown in "Detected radii" and on the sliders.)
HOUGH_PROC_WIDTH = 640             # reference width for all Hough numbers
HOUGH_WORK_WIDTH = 320             # Hough actually runs on an image this wide (4x fewer px = fast)
HOUGH_MIN_RADIUS = 155             # smallest can rim to accept (slider in the UI)
HOUGH_MAX_RADIUS = 165             # largest can rim to accept (slider in the UI); can = 160 px
HOUGH_MIN_DIST = 280               # min distance between circle centres (~ 1 can diameter)
HOUGH_CANNY_HIGH = 120             # HoughCircles param1 (upper Canny threshold)
HOUGH_ACCUM_THRESH = 35            # HoughCircles param2 (lower = more circles, more noise)
HOUGH_GATE_NEW = 2.5               # 2nd sighting may be up to this x radius further down the belt
HOUGH_GATE_TRACKED = 0.8           # later sightings must be within this x radius of the prediction
HOUGH_MAX_MISSED = 4               # frames a can may vanish before its track ends
HOUGH_MIN_TRACK_FRAMES = 2         # Hough sightings needed before a can can be counted
HOUGH_VERDICT_WAIT_S = 0.4         # verdict = when the can leaves view (YOLO caught up), max this long after the line
HOUGH_HISTORY = 90                 # per-can position history (frames) used to match YOLO frames
HOUGH_VOTE_FRAME_TOL = 3           # YOLO frame may be this many frames from a Hough sighting
HOUGH_EXTRAP_FRAMES = 15           # unmatched defects: follow a can's path this many frames back/forward

# --- camera speed options (None = leave the camera's own setting) ---
CAM_EXPOSURE_US = 300              # us. Short exposure = no motion blur on a fast belt (None = camera's own)
CAM_FPS = None                     # camera frame rate (e.g. 120)
CAM_BINNING = 1                    # 2 = bin/decimate 2x2 on the camera: 4x less data, higher max FPS
HOUGH_LINE_FRAC = 0.5              # virtual trigger line position (fraction of image width)
HOUGH_DIRECTION = +1               # +1 = cans move left->right, -1 = right->left
HOUGH_MIN_DEFECT_FRAMES = 1        # YOLO defect frames needed for DEFECT. Fast belt: YOLO only sees each
                                   # can 2-3 times, so 1. Raise to 2 if good cans get rejected.
HOUGH_CIRCLE_MARGIN = 1.3          # defect box centre may be this x radius from the can centre
HOUGH_LOG_FILE = "hough_can_log.csv"   # Hough verdicts only (can ID + decision), appended

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
# Hough circle can IDs - runs on EVERY grabbed frame in its own thread
# ----------------------------------------------------------------------------
def fast_gray(raw, pixel_format):
    """Cheapest grayscale for Hough, straight from the raw sensor array (no full debayer).
    Bayer: one green pixel per 2x2 cell -> half resolution, practically free."""
    if raw.dtype != np.uint8:
        raw = (raw >> 8).astype(np.uint8) if raw.dtype == np.uint16 else raw.astype(np.uint8)
    if raw.ndim == 3:
        if raw.shape[2] == 1:
            raw = raw[:, :, 0]
        else:
            code = cv2.COLOR_RGB2GRAY if pixel_format.startswith("RGB") else cv2.COLOR_BGR2GRAY
            return cv2.cvtColor(raw, code)
    if pixel_format.startswith(("BayerRG", "BayerBG")):
        return np.ascontiguousarray(raw[0::2, 1::2])
    if pixel_format.startswith(("BayerGR", "BayerGB")):
        return np.ascontiguousarray(raw[0::2, 0::2])
    return raw


def detect_circles(gray, scale=1.0):
    """Hough circles on the working image.
    scale = working px per reference px (reference = image scaled to HOUGH_PROC_WIDTH wide).
    Radii/distances come from the HOUGH_* settings (reference px). Returns [(x, y, r)] in reference px."""
    min_r = max(1, int(np.floor(HOUGH_MIN_RADIUS * scale)))
    max_r = max(min_r + 1, int(np.ceil(HOUGH_MAX_RADIUS * scale)))
    blur = cv2.medianBlur(gray, 5)
    circles = cv2.HoughCircles(
        blur, cv2.HOUGH_GRADIENT, dp=1.2, minDist=max(1.0, HOUGH_MIN_DIST * scale),
        param1=HOUGH_CANNY_HIGH, param2=HOUGH_ACCUM_THRESH,
        minRadius=min_r, maxRadius=max_r)
    if circles is None:
        return []
    inv = 1.0 / scale
    return [(int(round(x * inv)), int(round(y * inv)), int(round(r * inv))) for x, y, r in circles[0]]


class HoughCanTracker:
    """Vision-only can IDs, built for fast belts. Thread-safe:
      * update()    - Hough thread, every grabbed frame (frame number fno)
      * vote()      - YOLO thread: defect boxes vote for the can that was at that spot IN THAT FRAME
      * yolo_done() - YOLO thread: YOLO has finished frame fno
      * pop_ready() - verdicts whose YOLO evidence is complete (or waited HOUGH_VERDICT_WAIT_S)
    Tracking uses constant-velocity prediction, so a can moving most of a diameter per frame keeps
    its ID; a can that coasts over the line unseen is still counted when its track ends.
    All coordinates are reference px (image scaled to HOUGH_PROC_WIDTH wide)."""

    def __init__(self, line_frac=None, direction=None):
        self.line_frac = HOUGH_LINE_FRAC if line_frac is None else line_frac
        self.direction = HOUGH_DIRECTION if direction is None else direction
        self.line_x = int(HOUGH_PROC_WIDTH * self.line_frac)
        self.lock = threading.Lock()
        self.next_id = 1
        self.tracks = {}
        self.pending = []          # (tid, cross_fno, deadline)
        self.yolo_fno = -1
        self.ref_shape = None      # (h, w) in reference px
        self.speed = 0.0           # fastest visible can, reference px per frame
        self.last_fno = -1
        self.orphans = deque(maxlen=400)   # defect boxes no can was found for yet
        self.orphans_lost = 0              # defects that never matched any can (diagnostics)
        self.defects_seen = 0

    # -- Hough thread --------------------------------------------------------
    def update(self, fno, circles, ref_shape):
        with self.lock:
            self.ref_shape = ref_shape
            self.last_fno = fno
            self._match(fno, circles)
            sp = [abs(t["vx"]) for t in self.tracks.values()
                  if t["frames"] >= 2 and not t["missed"] and not t["dead"]]
            self.speed = max(sp) if sp else self.speed * 0.9

    @staticmethod
    def _predict(t, fno):
        dt = fno - t["fno"]
        return t["x"] + t["vx"] * dt, t["y"] + t["vy"] * dt

    def _match(self, fno, circles):
        pairs = []
        for tid, t in self.tracks.items():
            if t["dead"]:
                continue
            r = t["r"]
            px, py = self._predict(t, fno)
            for i, (x, y, _) in enumerate(circles):
                if t["frames"] >= 2:                          # velocity known: tight gate
                    ok = np.hypot(x - px, y - py) <= HOUGH_GATE_TRACKED * r
                else:                                         # 2nd sighting: long forward jump allowed
                    fwd = self.direction * (x - t["x"])
                    ok = -0.25 * r <= fwd <= HOUGH_GATE_NEW * r and abs(y - t["y"]) <= 0.5 * r
                if ok:
                    pairs.append((float(np.hypot(x - px, y - py)), tid, i))
        pairs.sort()

        used_t, used_c = set(), set()
        for _, tid, i in pairs:
            if tid in used_t or i in used_c:
                continue
            used_t.add(tid)
            used_c.add(i)
            x, y, r = circles[i]
            t = self.tracks[tid]
            dt = max(1, fno - t["fno"])
            vx, vy = (x - t["x"]) / dt, (y - t["y"]) / dt
            if t["frames"] >= 2:
                vx, vy = 0.5 * t["vx"] + 0.5 * vx, 0.5 * t["vy"] + 0.5 * vy
            prev_x = t["x"]
            t.update(x=x, y=y, r=r, vx=vx, vy=vy, fno=fno, missed=0, frames=t["frames"] + 1)
            t["hist"].append((fno, x, y, r))
            if not t["triggered"] and self._crossed(prev_x, x):
                self._trigger(tid, fno)

        for tid in list(self.tracks):
            t = self.tracks[tid]
            if tid in used_t or t["dead"]:
                continue
            t["missed"] += 1
            if t["missed"] > HOUGH_MAX_MISSED:
                # Lost before being seen at the line, but its motion carried it over: count it
                if not t["triggered"] and t["frames"] >= HOUGH_MIN_TRACK_FRAMES:
                    px, _ = self._predict(t, fno)
                    if self._crossed(t["x"], px):
                        self._trigger(tid, fno)
                if t["pending"]:
                    t["dead"] = True               # removed after its verdict is emitted
                else:
                    del self.tracks[tid]

        for i, (x, y, r) in enumerate(circles):
            if i in used_c:
                continue
            past = self.direction * (x - self.line_x) > 0   # appeared already past the line
            self.tracks[self.next_id] = dict(
                x=x, y=y, r=r, vx=0.0, vy=0.0, fno=fno, missed=0, frames=1,
                triggered=past, pending=False, dead=False, cross_fno=None,
                defect_frames=0, max_conf=0.0, classes={}, yolo_fnos=set(), voted=set(),
                hist=deque([(fno, x, y, r)], maxlen=HOUGH_HISTORY))
            self.next_id += 1

    def _trigger(self, tid, fno):
        t = self.tracks[tid]
        t["triggered"] = True
        if t["frames"] < HOUGH_MIN_TRACK_FRAMES:
            return                                  # one-frame flicker, not a can
        t["pending"] = True
        t["cross_fno"] = fno
        self.pending.append((tid, fno, time.monotonic() + HOUGH_VERDICT_WAIT_S))

    def _crossed(self, x0, x1):
        if self.direction > 0:
            return x0 < self.line_x <= x1
        return x0 > self.line_x >= x1

    # -- YOLO thread ---------------------------------------------------------
    @staticmethod
    def _pos_at(t, fno):
        """Where this can was in frame fno (from its history; short extrapolation if needed)."""
        best = None
        for hf, x, y, r in reversed(t["hist"]):
            d = abs(hf - fno)
            if best is None or d < best[0]:
                best = (d, hf, x, y, r)
            if hf < fno:
                break
        if best is None or best[0] > HOUGH_VOTE_FRAME_TOL:
            return None
        _, hf, x, y, r = best
        return x + t["vx"] * (fno - hf), y + t["vy"] * (fno - hf), r

    def vote(self, fno, dets, frame_w):
        """dets from the YOLO frame fno (pixel coords of a frame frame_w wide)."""
        s = HOUGH_PROC_WIDTH / frame_w
        with self.lock:
            present = {}
            for tid, t in self.tracks.items():
                pos = self._pos_at(t, fno)
                if pos is not None:
                    present[tid] = pos
                    t["yolo_fnos"].add(fno)        # YOLO looked at this can in this frame
            hits = {}
            for d in dets:
                if d["is_good"]:
                    continue
                x1, y1, x2, y2 = d["xyxy"]
                cx, cy = (x1 + x2) / 2 * s, (y1 + y2) / 2 * s
                best = None
                for tid, (x, y, r) in present.items():
                    dist = float(np.hypot(cx - x, cy - y))
                    if dist <= r * HOUGH_CIRCLE_MARGIN and (best is None or dist < best[0]):
                        best = (dist, tid)
                if best is not None and (best[1] not in hits or d["conf"] > hits[best[1]]["conf"]):
                    hits[best[1]] = d
            matched = set()
            for tid, d in hits.items():
                matched.add(id(d))
                self._add_vote(self.tracks[tid], fno, d["name"], d["conf"])
            # Defects with no circle under them right now: keep them for the can whose path passes there
            for d in dets:
                if d["is_good"]:
                    continue
                self.defects_seen += 1
                if id(d) not in matched:
                    x1, y1, x2, y2 = d["xyxy"]
                    self.orphans.append([fno, (x1 + x2) / 2 * s, (y1 + y2) / 2 * s,
                                         d["name"], d["conf"], False])

    @staticmethod
    def _add_vote(t, fno, name, conf):
        if fno in t["voted"]:
            return
        t["voted"].add(fno)
        t["defect_frames"] += 1
        t["max_conf"] = max(t["max_conf"], conf)
        t["classes"][name] = t["classes"].get(name, 0) + 1

    def _path_pos(self, t, fno):
        """Position on the can's straight-line path at frame fno (history + velocity)."""
        if not t["hist"]:
            return None
        first, last = t["hist"][0], t["hist"][-1]
        ref = first if fno < first[0] else (last if fno > last[0] else None)
        if ref is None:                                  # inside the seen range: nearest sighting
            ref = min(t["hist"], key=lambda h: abs(h[0] - fno))
        if abs(fno - ref[0]) > HOUGH_EXTRAP_FRAMES:
            return None
        hf, x, y, r = ref
        return x + t["vx"] * (fno - hf), y + t["vy"] * (fno - hf), r

    def _claim_orphans(self, t):
        for o in self.orphans:
            if o[5]:
                continue
            pos = self._path_pos(t, o[0])
            if pos is None:
                continue
            x, y, r = pos
            if np.hypot(o[1] - x, o[2] - y) <= r * HOUGH_CIRCLE_MARGIN:
                o[5] = True
                self._add_vote(t, o[0], o[3], o[4])

    def _expire_orphans(self):
        while self.orphans and (self.orphans[0][5] or self.orphans[0][0] < self.last_fno - 300):
            o = self.orphans.popleft()
            if not o[5]:
                self.orphans_lost += 1

    def yolo_done(self, fno):
        with self.lock:
            self.yolo_fno = max(self.yolo_fno, fno)

    # -- verdicts --------------------------------------------------------------
    def pop_ready(self):
        now = time.monotonic()
        out, keep = [], []
        with self.lock:
            for tid, need, deadline in self.pending:
                t = self.tracks.get(tid)
                if t is None:
                    continue
                # Wait until the can has left the view AND YOLO has processed its last frame, so
                # YOLO's whole look at the can counts (capped by the deadline after the line).
                done = t["dead"] and self.yolo_fno >= t["fno"]
                if not done and now < deadline:
                    keep.append((tid, need, deadline))
                    continue
                t["pending"] = False
                self._claim_orphans(t)            # defects YOLO saw where Hough had no circle
                yolo_seen = len(t["yolo_fnos"])
                # fast belt: if YOLO only saw the can 1-2 times, one defect frame is enough
                need_defects = min(HOUGH_MIN_DEFECT_FRAMES, max(1, yolo_seen))
                top = max(t["classes"], key=t["classes"].get) if t["classes"] else ""
                out.append({
                    "id": tid, "defective": t["defect_frames"] >= need_defects,
                    "frames": yolo_seen, "hough_frames": t["frames"],
                    "defect_frames": t["defect_frames"], "max_conf": t["max_conf"],
                    "defect_class": top, "time": time.time(), "source": "hough",
                })
                if t["dead"]:
                    del self.tracks[tid]
            self.pending = keep
            self._expire_orphans()
        return out

    # -- display -------------------------------------------------------------
    def visible(self):
        with self.lock:
            return [(tid, dict(x=t["x"], y=t["y"], r=t["r"], triggered=t["triggered"],
                               defect_frames=t["defect_frames"]))
                    for tid, t in self.tracks.items() if not t["missed"] and not t["dead"]]

    def circle_view(self):
        """White can rims on a black background - nothing else (3-channel for Qt)."""
        h, w = self.ref_shape if self.ref_shape else (480, HOUGH_PROC_WIDTH)
        out = np.zeros((h, w, 3), dtype=np.uint8)
        for _, t in self.visible():
            cv2.circle(out, (t["x"], t["y"]), t["r"], (255, 255, 255), 2)
        return out

    def draw_on(self, out, thick, font_scale, fno):
        """Circles (where each can was in frame fno), IDs and the trigger line on the YOLO frame."""
        s = out.shape[1] / HOUGH_PROC_WIDTH
        h = out.shape[0]
        lx = int(self.line_x * s)
        cv2.line(out, (lx, 0), (lx, h), (0, 0, 255), thick)
        with self.lock:
            items = [(tid, self._pos_at(t, fno), t["defect_frames"], t["triggered"])
                     for tid, t in self.tracks.items() if not t["dead"]]
        for tid, pos, defects, triggered in items:
            if pos is None:
                continue
            x, y, r = pos
            color = (0, 0, 255) if defects else ((0, 200, 0) if triggered else (255, 200, 0))
            c, rr = (int(x * s), int(y * s)), int(r * s)
            cv2.circle(out, c, rr, color, thick)
            ty = min(h - 8, c[1] + rr + int(32 * font_scale))     # below the rim, clear of YOLO labels
            cv2.putText(out, f"ID {tid}", (c[0] - rr // 3, ty),
                        cv2.FONT_HERSHEY_SIMPLEX, font_scale, color, max(1, thick - 1), cv2.LINE_AA)


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
                 imgsz=DEFAULT_IMGSZ, id_mode="hough"):
        super().__init__()
        self.model_path = model_path
        self.conf_thresh = conf_thresh
        self.camera_index = camera_index
        self.imgsz = imgsz
        self.id_mode = id_mode                       # "hough" | "bytetrack" | "off"
        self.track = id_mode == "bytetrack"          # Ultralytics ByteTrack
        self.backend = "HAILO-8" if is_hailo_model(model_path) else "CPU"
        # Written from the GUI thread, read here (plain attribute reads are atomic in CPython)
        self.continuous = continuous
        self.object_present = False
        self.display_mode = 0                        # 0 live, 1 log (no drawing), 2 hough circles
        self._running = True
        self._acquiring = False

        # Hand-off between grab and inference threads (newest frame only)
        self._slot_lock = threading.Lock()
        self._slot = None
        self._slot_event = threading.Event()
        self._infer_running = False
        self.frames_grabbed = 0
        self.frames_dropped = 0

        # Hough runs on EVERY grabbed frame in its own thread (independent of YOLO speed)
        self.hough = None
        self._hslot_lock = threading.Lock()
        self._hslot = None
        self._hslot_event = threading.Event()
        self.hough_dropped = 0
        self.hough_fps = 0.0
        self.hough_ms = 0.0

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
            self.status_update.emit(f"Good classes: {good_classes}   Can IDs: {self.id_mode.upper()}")
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
                hough_thread = None
                try:
                    if cam_list.GetSize() <= self.camera_index:
                        raise CameraFault("No Teledyne/FLIR camera found. Check cable / IP config.")
                    cam = cam_list.GetByIndex(self.camera_index)
                    cam.Init()
                    self._configure_camera(cam)
                    self.status_update.emit(f"Camera {self.camera_index} initialised.")
                    self.camera_ok.emit()

                    # Start the inference thread for this camera session
                    # Create the Hough tracker FIRST and pass it to both threads.
                    # (Bug fix: the YOLO thread used to start before the tracker existed, read
                    #  self.hough == None, and never passed defects on -> every can logged GOOD.)
                    self.hough = HoughCanTracker() if self.id_mode == "hough" else None
                    self._infer_running = True
                    infer_thread = threading.Thread(
                        target=self._infer_loop, args=(model, good_classes, self.hough), daemon=True)
                    infer_thread.start()
                    if self.hough is not None:
                        hough_thread = threading.Thread(
                            target=self._hough_loop, args=(self.hough,), daemon=True)
                        hough_thread.start()

                    self._grab_loop(cam)                     # returns only when stopped
                except Exception as e:
                    self.camera_fault.emit(str(e))
                finally:
                    self._infer_running = False
                    self._slot_event.set()
                    self._hslot_event.set()
                    if infer_thread is not None:
                        infer_thread.join(timeout=5)
                    if hough_thread is not None:
                        hough_thread.join(timeout=5)
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

            fno = self.frames_grabbed
            # Hough gets every frame (it is fast); only counts as dropped if it fell behind
            if self.id_mode == "hough":
                with self._hslot_lock:
                    if self._hslot is not None:
                        self.hough_dropped += 1
                    self._hslot = (fno, raw, pixel_format)
                self._hslot_event.set()

            # Hand the newest frame to the inference thread (overwrite if it is still busy)
            with self._slot_lock:
                if self._slot is not None:
                    self.frames_dropped += 1
                self._slot = (fno, raw, pixel_format, counted)
            self._slot_event.set()

    # -- stage 1b: Hough on every frame (own thread) ---------------------------
    def _hough_loop(self, hough):
        n, t0, last_err = 0, time.monotonic(), 0.0
        while self._infer_running and self._running:
            item = None
            if self._hslot_event.wait(timeout=0.05):
                with self._hslot_lock:
                    item, self._hslot = self._hslot, None
                    self._hslot_event.clear()
            if item is not None:
                fno, raw, pixel_format = item
                try:
                    t_start = time.perf_counter()
                    gray = fast_gray(raw, pixel_format)
                    gh, gw = gray.shape[:2]
                    ref_shape = (int(round(gh * HOUGH_PROC_WIDTH / gw)), HOUGH_PROC_WIDTH)
                    work_h = max(1, int(round(gh * HOUGH_WORK_WIDTH / gw)))
                    work = cv2.resize(gray, (HOUGH_WORK_WIDTH, work_h), interpolation=cv2.INTER_AREA)
                    circles = detect_circles(work, HOUGH_WORK_WIDTH / HOUGH_PROC_WIDTH)
                    hough.update(fno, circles, ref_shape)
                    self.hough_ms = (time.perf_counter() - t_start) * 1000.0
                    n += 1
                except Exception as e:
                    if time.monotonic() - last_err > 2.0:
                        last_err = time.monotonic()
                        self.status_update.emit(f"Hough error: {e}")
            for verdict in hough.pop_ready():
                self.can_finalized.emit(verdict)
            el = time.monotonic() - t0
            if el >= FPS_WINDOW_S:
                self.hough_fps, n, t0 = n / el, 0, time.monotonic()

    # -- stage 2: convert + YOLO + tracking (inference thread) ---------------
    def _infer_loop(self, model, good_classes, hough=None):
        tracker = CanTracker()
        if self.id_mode == "hough" and hough is None:
            self.status_update.emit("WARNING: Hough tracker missing - defects cannot reach can verdicts.")
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
            fno, raw, pixel_format, counted = item

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
                if hough is not None:
                    hough.vote(fno, dets, frame.shape[1])   # defects -> the can at that spot in frame fno
                    hough.yolo_done(fno)

                # FPS
                fps_frames += 1
                elapsed = time.monotonic() - fps_t0
                if elapsed >= FPS_WINDOW_S:
                    fps = fps_frames / elapsed
                    fps_frames, fps_t0 = 0, time.monotonic()

                # Only draw frames that will actually be shown
                annotated = None
                now = time.monotonic()
                mode = self.display_mode
                if mode in (0, 2) and now - last_display >= display_interval:
                    last_display = now
                    if mode == 2:          # circles only, white on black
                        annotated = (hough.circle_view() if hough is not None
                                     else np.zeros((360, 640, 3), np.uint8))
                    else:
                        annotated = self._draw(frame, dets, idx, is_defect, hough, fno)

                vis = hough.visible() if hough is not None else []
                metrics = {
                    "frame_idx": idx,
                    "is_defect": is_defect,
                    "detections": [(d["name"], d["conf"], d["is_good"], d["id"]) for d in dets],
                    "counted": counted,
                    "time": time.time(),
                    "fps": fps,
                    "infer_ms": float(r.speed.get("inference", 0.0)),
                    "backend": self.backend,
                    "active_ids": len(vis) if hough is not None else len(tracker.cans),
                    "radii": sorted(t["r"] for _, t in vis),
                    "hough_fps": self.hough_fps,
                    "hough_ms": self.hough_ms,
                    "hough_dropped": self.hough_dropped,
                    "can_speed": hough.speed if hough is not None else 0.0,
                    "defects_seen": hough.defects_seen if hough is not None else 0,
                    "defects_unmatched": hough.orphans_lost if hough is not None else 0,
                    "grabbed": self.frames_grabbed,
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
        # --- speed options for a fast belt ---
        if CAM_BINNING and CAM_BINNING > 1:
            done = False
            for h_name, v_name in (("BinningHorizontal", "BinningVertical"),
                                   ("DecimationHorizontal", "DecimationVertical")):
                try:
                    getattr(cam, h_name).SetValue(int(CAM_BINNING))
                    getattr(cam, v_name).SetValue(int(CAM_BINNING))
                    self.status_update.emit(f"Camera {h_name[:-10].lower()} {CAM_BINNING}x{CAM_BINNING}")
                    done = True
                    break
                except Exception:
                    continue
            if not done:
                self.status_update.emit("Camera does not support binning/decimation - skipped.")
        if CAM_EXPOSURE_US:
            try:
                cam.ExposureAuto.SetValue(PySpin.ExposureAuto_Off)
                cam.ExposureTime.SetValue(float(CAM_EXPOSURE_US))
                self.status_update.emit(f"Exposure {CAM_EXPOSURE_US:.0f} us")
            except Exception as e:
                self.status_update.emit(f"Could not set exposure: {e}")
        if CAM_FPS:
            try:
                try:
                    cam.AcquisitionFrameRateEnable.SetValue(True)
                except Exception:
                    pass
                cam.AcquisitionFrameRate.SetValue(float(CAM_FPS))
                self.status_update.emit(f"Camera frame rate {cam.AcquisitionFrameRate.GetValue():.1f} fps")
            except Exception as e:
                self.status_update.emit(f"Could not set frame rate: {e}")

    # -- drawing -------------------------------------------------------------
    @staticmethod
    def _draw(frame, dets, idx, is_defect, hough=None, fno=0):
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

        if hough is not None:
            hough.draw_on(out, thick, font_scale, fno)

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
    TRIGGER_MODES = ["sensor", "continuous", "vision"]
    # view mode -> stacked widget: 0 live, 1 prediction log, 2 hough circles, 3 hough can log
    VIEW_TO_STACK = {0: 0, 1: 1, 2: 0, 3: 2}

    def __init__(self, model_path, conf_thresh, trigger="vision", imgsz=DEFAULT_IMGSZ, id_mode="hough"):
        super().__init__()
        self.backend = "HAILO-8" if is_hailo_model(model_path) else "CPU"
        self.setWindowTitle(f"Yards Can Inspection Station  [{self.backend}]")
        self.resize(1400, 860)

        self.model_path = model_path
        self.conf_thresh = conf_thresh
        self.id_mode = id_mode
        self.track = id_mode != "off"
        if trigger == "vision" and id_mode != "hough":
            print("[WARN] vision trigger needs --id-mode hough; using sensor trigger")
            trigger = "sensor"
        self.trigger_mode = trigger
        self.continuous = trigger != "sensor"        # frames always "counted" unless sensor-gated
        self.imgsz = imgsz
        self.view_mode = 2 if id_mode == "hough" else 0   # 0 live, 1 log, 2 hough circles

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
        self.btn_view_circles = QPushButton("Hough Circles")
        self.btn_view_hlog = QPushButton("Hough Can Log")
        self.view_buttons = (self.btn_view_live, self.btn_view_log, self.btn_view_circles,
                             self.btn_view_hlog)
        self.view_group = QButtonGroup(self)
        self.view_group.setExclusive(True)
        for i, b in enumerate(self.view_buttons):
            b.setCheckable(True)
            b.setStyleSheet(VIEW_BTN)
            self.view_group.addButton(b, i)
            view_bar.addWidget(b)
        self.btn_view_circles.setEnabled(self.id_mode == "hough")
        self.btn_view_hlog.setEnabled(self.id_mode == "hough")
        self.view_buttons[self.view_mode].setChecked(True)
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

        # Hough-only verdict log: can ID + decision, independent of the GPIO sensor
        self.hough_log = QPlainTextEdit(readOnly=True)
        self.hough_log.setMaximumBlockCount(PRED_LOG_MAX_LINES)
        self.hough_log.setFont(mono)
        self.hough_log.setStyleSheet(
            "background-color: #111; color: #e0e0e0; border-radius: 4px; padding: 6px;")
        self.hough_log.setMinimumSize(850, 650)
        self.hough_log.appendPlainText(f"{'TIME':<14}{'CAN ID':>7}   {'DECISION':<9}DETAIL")
        self.view_stack.addWidget(self.hough_log)     # index 2
        self.view_stack.setCurrentIndex(self.VIEW_TO_STACK[self.view_mode])
        left.addWidget(self.view_stack, stretch=1)
        main_layout.addLayout(left, stretch=3)

        n_views = 4 if self.id_mode == "hough" else 2
        QShortcut(QKeySequence("Ctrl+Tab"), self, activated=lambda: self.set_view(
            (self.view_mode + 1) % n_views))

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
        self.cmb_trigger.addItems([f"Sensor (GPIO {SENSOR_PIN})", "Continuous (bench test)",
                                   "Vision (Hough line)"])
        self.cmb_trigger.setCurrentIndex(self.TRIGGER_MODES.index(self.trigger_mode))
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

        # Hough can-size sliders (live)
        if self.id_mode == "hough":
            size = QGroupBox(f"Can Size (Hough, px at {HOUGH_PROC_WIDTH} wide)")
            szl = QVBoxLayout(size)
            self.lbl_min_r = QLabel()
            self.slider_min_r = QSlider(Qt.Orientation.Horizontal)
            self.slider_min_r.setRange(5, 400)
            self.slider_min_r.setValue(HOUGH_MIN_RADIUS)
            self.slider_min_r.valueChanged.connect(self.update_min_radius)
            self.lbl_max_r = QLabel()
            self.slider_max_r = QSlider(Qt.Orientation.Horizontal)
            self.slider_max_r.setRange(5, 400)
            self.slider_max_r.setValue(HOUGH_MAX_RADIUS)
            self.slider_max_r.valueChanged.connect(self.update_max_radius)
            self.lbl_radii = QLabel("Detected radii: --")
            self.lbl_radii.setStyleSheet("color: #5bc0de;")
            self.lbl_hough_perf = QLabel("Hough: --")
            self.lbl_hough_perf.setWordWrap(True)
            for wdg in (self.lbl_min_r, self.slider_min_r, self.lbl_max_r,
                        self.slider_max_r, self.lbl_radii, self.lbl_hough_perf):
                szl.addWidget(wdg)
            self._refresh_radius_labels()
            right.addWidget(size)

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
                                            id_mode=self.id_mode)
        self.worker.object_present = self.object_present
        self.worker.display_mode = self.view_mode
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
        if self.trigger_mode == "vision":
            return          # Hough line does the counting; sensor is display-only

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
        mode = self.TRIGGER_MODES[index]
        if mode == "vision" and self.id_mode != "hough":
            self.log_message("Vision trigger needs Hough can IDs (--id-mode hough).")
            self.cmb_trigger.blockSignals(True)
            self.cmb_trigger.setCurrentIndex(self.TRIGGER_MODES.index(self.trigger_mode))
            self.cmb_trigger.blockSignals(False)
            return
        self.trigger_mode = mode
        self.continuous = mode != "sensor"
        if mode != "sensor" and self.object_present:
            self.clear_timer.stop()
            self.object_present = False
            if self.worker:
                self.worker.object_present = False
        if self.worker:
            self.worker.continuous = self.continuous
        self._finish_part(discard=True)
        self._set_defect_in_view(False)
        self.log_message(f"Trigger mode: {self.trigger_mode.upper()}")

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
        if v.get("source") == "hough":
            self._hough_log_verdict(v, verdict)
        acn = ""
        if v.get("source") == "hough" and self.trigger_mode == "vision":
            acn = f"ACN {self._vision_pass(v)}, "
        msg = f"CAN ID {v['id']}: {verdict} ({acn}{detail})"
        self.log_message(msg)
        self._log_event(msg, color)
        self.lbl_verdict.setText(f"Last can: ID {v['id']} {verdict}")
        self.lbl_verdict.setStyleSheet(f"color: {color}; font-size: 15px; font-weight: bold;")
        if v.get("source") == "hough" and self.trigger_mode == "vision" and v["defective"]:
            send_rejection()      # ACN = GLOBAL_OBJECT_COUNTER, set by _vision_pass

    def _hough_log_verdict(self, v, verdict):
        """Hough-only log: can ID + decision. Never touches the sensor / object counter."""
        ts = datetime.fromtimestamp(v["time"]).strftime("%H:%M:%S.%f")[:-3]
        hf = v.get("hough_frames", v["frames"])
        if v["defective"]:
            detail = (f"{v['defect_class']} {v['max_conf']:.2f}  "
                      f"({v['defect_frames']}/{v['frames']} YOLO frames, {hf} Hough frames)")
        else:
            detail = f"({v['defect_frames']}/{v['frames']} YOLO defect frames, {hf} Hough frames)"
        self.hough_log.appendPlainText(f"{ts:<14}{v['id']:>7}   {verdict:<9}{detail}")
        try:
            new = not os.path.exists(HOUGH_LOG_FILE)
            with open(HOUGH_LOG_FILE, "a", encoding="utf-8") as f:
                if new:
                    f.write("timestamp,can_id,decision,defect_class,max_conf,defect_frames,"
                            "yolo_frames,hough_frames\n")
                f.write(f"{datetime.fromtimestamp(v['time']):%Y-%m-%d %H:%M:%S.%f},{v['id']},{verdict},"
                        f"{v['defect_class']},{v['max_conf']:.3f},{v['defect_frames']},{v['frames']},"
                        f"{hf}\n")
        except OSError as e:
            self.log_message(f"Could not write {HOUGH_LOG_FILE}: {e}")

    def _vision_pass(self, v):
        """A Hough-tracked can crossed the virtual line: count it like a sensor pass."""
        global GLOBAL_OBJECT_COUNTER
        self.pass_count += 1
        GLOBAL_OBJECT_COUNTER += 1
        self.pass_times.append(time.monotonic())
        self.last_pass_wall = datetime.now()
        self._refresh_pass_counter()
        self._flash_pass_counter()
        self._log_event(f"VISION PASS #{self.pass_count} (can ID {v['id']})", "#5bc0de")
        return GLOBAL_OBJECT_COUNTER
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
            if self.trigger_mode != "vision":     # vision mode rejects per can at the line
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
            if self.view_mode in (0, 2):
                self._show_frame(frame)

    def _refresh_ui(self):
        m = self._last_metrics
        if m is not None:
            self.lbl_perf.setText(
                f"Backend: {m.get('backend', self.backend)}   FPS: {m.get('fps', 0.0):.1f}   "
                f"Inference: {m.get('infer_ms', 0.0):.1f} ms   Cans in view: {m.get('active_ids', 0)}")
            if self.id_mode == "hough":
                radii = m.get("radii", [])
                self.lbl_radii.setText("Detected radii: " + (", ".join(f"{r}" for r in radii) + " px"
                                                             if radii else "none"))
                # Is Hough keeping up, and does each can get enough frames?
                speed = m.get("can_speed", 0.0)
                per_can = (HOUGH_PROC_WIDTH / speed) if speed > 1 else 0
                grabbed = max(1, m.get("grabbed", 1))
                drop_pct = 100.0 * m.get("hough_dropped", 0) / grabbed
                ok = drop_pct < 1.0 and (per_can == 0 or per_can >= 4)
                self.lbl_hough_perf.setText(
                    f"Hough: {m.get('hough_fps', 0):.0f} fps, {m.get('hough_ms', 0):.1f} ms/frame, "
                    f"skipped {drop_pct:.1f}%   Can speed: {speed:.0f} px/frame"
                    + (f" (~{per_can:.0f} frames across view)" if per_can else "")
                    + f"   Defect boxes: {m.get('defects_seen', 0)}, not matched to a can: "
                      f"{m.get('defects_unmatched', 0)}")
                lost = m.get("defects_unmatched", 0)
                if lost > getattr(self, "_reported_unmatched", 0):
                    self._reported_unmatched = lost
                    self.log_message(f"WARNING: {lost} defect detection(s) had no Hough can under them "
                                     "- check the radius sliders / line position.")
                self.lbl_hough_perf.setStyleSheet(
                    f"color: {'#5cb85c' if ok else '#f0ad4e'}; font-weight: bold;")
        self._refresh_counters()

    # -- view switching / prediction log ---------------------------------------
    def set_view(self, index):
        if index in (2, 3) and self.id_mode != "hough":
            index = 0
        changed = index != self.view_mode
        self.view_mode = index
        self.view_stack.setCurrentIndex(self.VIEW_TO_STACK[index])
        self.view_buttons[index].setChecked(True)
        if self.worker:
            self.worker.display_mode = index          # 1 / 3 = a log: skip drawing entirely
        if changed:
            self._last_frame = None                   # old frame belongs to the other view
        elif index in (0, 2) and self._last_frame is not None and self.lbl_feed.text() == "FEED: LIVE":
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
        if self.view_mode == 3:
            self.hough_log.clear()
            return
        self._pred_buffer.clear()
        self.pred_log.clear()

    def save_prediction_log(self):
        self._flush_pred_log()
        hough = self.view_mode == 3
        log_widget = self.hough_log if hough else self.pred_log
        default = f"{'hough_cans' if hough else 'predictions'}_{datetime.now():%Y%m%d_%H%M%S}.txt"
        path, _ = QFileDialog.getSaveFileName(self, "Save prediction log", default, "Text files (*.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(log_widget.toPlainText())
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
            how = "Hough" if self.id_mode == "hough" else "ByteTrack"
            self.lbl_tracked.setText(
                f"Tracked cans ({how} IDs): {self.track_total}   Good: {self.track_good}   "
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

    def update_min_radius(self, val):
        """Circles smaller than this are ignored (not cans). Takes effect on the next frame."""
        global HOUGH_MIN_RADIUS, HOUGH_MAX_RADIUS
        HOUGH_MIN_RADIUS = val
        if val >= HOUGH_MAX_RADIUS:                 # keep max above min
            self.slider_max_r.setValue(min(400, val + 1))
        self._refresh_radius_labels()

    def update_max_radius(self, val):
        global HOUGH_MIN_RADIUS, HOUGH_MAX_RADIUS
        HOUGH_MAX_RADIUS = val
        if val <= HOUGH_MIN_RADIUS:
            self.slider_min_r.setValue(max(5, val - 1))
        self._refresh_radius_labels()

    def _refresh_radius_labels(self):
        self.lbl_min_r.setText(f"Min radius: {HOUGH_MIN_RADIUS} px  (smaller circles ignored)")
        self.lbl_max_r.setText(f"Max radius: {HOUGH_MAX_RADIUS} px  (larger circles ignored)")

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
    global PROC_MAX_SIDE, DISPLAY_MAX_FPS, HOUGH_LINE_FRAC, HOUGH_DIRECTION
    global HOUGH_MIN_RADIUS, HOUGH_MAX_RADIUS, HOUGH_LOG_FILE, HOUGH_WORK_WIDTH
    global CAM_EXPOSURE_US, CAM_FPS, CAM_BINNING
    parser = argparse.ArgumentParser(description="Can inspection station: YOLO (Hailo-8) + sensor + stack light")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="Hailo export folder (best.hef + metadata.yaml), a .hef, or a .pt for CPU")
    parser.add_argument("--conf", type=float, default=0.40, help="Detection confidence threshold (default 0.40)")
    parser.add_argument("--imgsz", type=int, default=DEFAULT_IMGSZ,
                        help="Inference size; for Hailo it must match the export size")
    parser.add_argument("--trigger", choices=["vision", "sensor", "continuous"], default="vision",
                        help="vision = Hough line counts cans (default), sensor = GPIO 25, "
                             "continuous = bench test")
    parser.add_argument("--continuous", action="store_true",
                        help="Same as --trigger continuous")
    parser.add_argument("--id-mode", choices=["hough", "bytetrack", "off"], default="hough",
                        help="How cans get IDs (default: hough)")
    parser.add_argument("--no-track", action="store_true", help="Same as --id-mode off")
    parser.add_argument("--line", type=float, default=HOUGH_LINE_FRAC,
                        help="Hough trigger line position, fraction of image width (0-1)")
    parser.add_argument("--direction", type=int, choices=[1, -1], default=HOUGH_DIRECTION,
                        help="Belt direction in the image: 1 = left->right, -1 = right->left")
    parser.add_argument("--min-radius", type=int, default=HOUGH_MIN_RADIUS,
                        help="Starting min can radius (px at 640 wide); also a slider in the UI")
    parser.add_argument("--max-radius", type=int, default=HOUGH_MAX_RADIUS,
                        help="Starting max can radius (px at 640 wide); also a slider in the UI")
    parser.add_argument("--hough-width", type=int, default=HOUGH_WORK_WIDTH,
                        help="Width Hough runs at (320 = fast, 640 = more precise, 240 = fastest)")
    parser.add_argument("--exposure", type=float, default=CAM_EXPOSURE_US,
                        help=f"Camera exposure in microseconds (default {CAM_EXPOSURE_US}; 0 = camera's own)")
    parser.add_argument("--cam-fps", type=float, default=CAM_FPS,
                        help="Camera frame rate to request (e.g. 120)")
    parser.add_argument("--binning", type=int, default=CAM_BINNING, choices=[1, 2, 4],
                        help="Camera binning/decimation (2 = 4x less data, higher max frame rate)")
    parser.add_argument("--hough-log", default=HOUGH_LOG_FILE,
                        help="CSV file for Hough can ID + decision log (appended)")
    parser.add_argument("--proc-size", type=int, default=PROC_MAX_SIDE,
                        help="Downscale camera frames to this longest side before YOLO (0 = off)")
    parser.add_argument("--display-fps", type=int, default=DISPLAY_MAX_FPS,
                        help="Max UI display rate (inference still runs on every frame)")
    args = parser.parse_args()

    PROC_MAX_SIDE = args.proc_size
    DISPLAY_MAX_FPS = args.display_fps
    HOUGH_LINE_FRAC = args.line
    HOUGH_DIRECTION = args.direction
    HOUGH_MIN_RADIUS, HOUGH_MAX_RADIUS = args.min_radius, max(args.max_radius, args.min_radius + 1)
    HOUGH_LOG_FILE = args.hough_log
    HOUGH_WORK_WIDTH = args.hough_width
    CAM_EXPOSURE_US, CAM_FPS, CAM_BINNING = args.exposure, args.cam_fps, args.binning
    trigger = "continuous" if args.continuous else args.trigger
    id_mode = "off" if args.no_track else args.id_mode

    app = QApplication(sys.argv)
    window = MainWindow(args.model, args.conf, trigger=trigger,
                        imgsz=args.imgsz, id_mode=id_mode)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
