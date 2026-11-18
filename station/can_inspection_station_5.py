"""
can_inspection_station.py - Yards can inspection station

  SENSOR (GPIO 25 / pin 22)      gives every can its Can ID: #1, #2, #3 ...
  CAMERA DETECTION ZONE          a narrow strip of the camera image acts as a second sensor and
                                 confirms when that same can is under the camera
  YOLO MODEL                     inspects the frames of that can -> one GOOD / DEFECT per Can ID

How a can is followed
  1. Can passes the sensor           -> Can #N created, status "On the way"
  2. Can enters the camera zone      -> matched to the oldest waiting Can ID (cans can't overtake
                                        each other on a belt), status "Inspecting"
                                        The frame that confirmed it is ALWAYS sent to YOLO, so
                                        every can is inspected at least once.
  3. Can leaves the camera zone      -> all YOLO results for Can #N -> final GOOD or DEFECT
     Never seen by the camera        -> MISSED (after the expected travel time)
     Camera saw a can with no ID     -> CAMERA ONLY

The zone check runs on every camera frame in the camera thread; YOLO runs in its own thread on
the newest frame. A slow model therefore never makes the zone miss a gap between cans.

Stack light (Auto mode)
  * GREEN           - system active (camera OK)
  * YELLOW          - defect in view (GREEN off); back to GREEN when it leaves
  * RED + BUZZER    - camera acquisition failure (retries automatically)

Setup on the belt
  * Sensor must be BEFORE the camera (cans reach the sensor first).
  * Live View shows the zone box. Keep it NARROWER than a can, across the belt.
  * With no can in the zone press "Capture Empty Belt"; set Trigger level between the
    "Image change" values with and without a can. Re-capture if the lighting changes.

Usage:
    python can_inspection_station.py --model best.pt --conf 0.25
"""

import sys
import argparse
import csv
import html
import shutil
import subprocess
import threading
import time
from collections import deque
from datetime import datetime

import numpy as np
import cv2

try:
    import PySpin
except ImportError:
    PySpin = None

try:
    from ultralytics import YOLO
except ImportError:
    YOLO = None

from PyQt6.QtCore import QThread, QTimer, pyqtSignal, Qt
from PyQt6.QtGui import QImage, QPixmap, QFontDatabase, QKeySequence, QShortcut, QColor, QFont
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QSlider, QGroupBox, QTextEdit, QComboBox, QGridLayout,
    QStackedWidget, QPlainTextEdit, QButtonGroup, QFileDialog, QTableWidget,
    QTableWidgetItem, QHeaderView, QProgressBar, QScrollArea,
)

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
SENSOR_PIN = 25                    # BCM GPIO 25
SENSOR_PHYSICAL_PIN = 22           # header pin 22
SENSOR_ACTIVE_STATE = "LOW"        # active-low sensor: LOW = can present
SENSOR_POLL_S = 0.005              # sensor polling interval (pinctrl itself takes ~10 ms per read)
PASS_REARM_MS = 30                 # sensor must read clear this long before the next can counts
PASS_RATE_WINDOW_S = 60            # window for the cans/min display

# Camera detection zone, fractions of the image (left, top, right, bottom).
# Default: narrow vertical strip in the middle, for a belt moving left<->right in the image.
# For a belt moving up/down in the image use a narrow HORIZONTAL strip, e.g. (0.2, 0.47, 0.8, 0.53).
ZONE = (0.47, 0.20, 0.53, 0.80)
ZONE_ON_LEVEL = 25                 # image change (0-255) meaning "can in zone" - slider in the UI
ZONE_OFF_RATIO = 0.6               # zone clears when change drops below ON_LEVEL * this
ZONE_MIN_FRAMES = 2                # frames in a row before a NEW can counts (clearing needs 1)
BACKGROUND_ADAPT = 0.02            # follow slow lighting drift while the zone is empty (0 = off)

MAX_TRAVEL_S = 5.0                 # a Can ID the camera hasn't seen within this time -> MISSED
                                   # (adapts to the measured sensor->camera travel time)
EXPECTED_TRAVEL_S = None           # optional: sensor -> camera travel time in seconds (e.g. 0.35).
                                   # The UI shows the measured value; enter it here so matching uses
                                   # timing from the very first can (otherwise the first few cans
                                   # after start-up are matched by order only).
MATCH_TOLERANCE_S = 0.15           # a camera sighting must be within this of a can's expected
                                   # arrival (also limited to 40% of the gap between cans)
FINALIZE_TIMEOUT_S = 3.0           # decide a can at the latest this long after it left the zone
MUST_QUEUE_MAX = 8                 # cans waiting for YOLO before it is considered overloaded

GET_NEXT_IMAGE_TIMEOUT_MS = 1000
FAULT_AFTER_ERRORS = 5             # consecutive grab errors/timeouts before declaring a camera fault
CAMERA_RETRY_S = 3.0               # wait between camera reconnect attempts
BUZZER_FAULT_MS = 3000             # buzzer length when a camera fault starts (0 = until recovered)
DEFECT_CLEAR_FRAMES = 1            # non-defect frames needed before YELLOW -> GREEN
CHASE_STEP_MS = 250                # manual test chase step length

GOOD_KEYWORD = "good"              # any class whose name contains this counts as "good"

DISPLAY_MAX_WIDTH = 1280           # live view is downscaled to this width (in the YOLO thread)
PRED_LOG_MAX_LINES = 5000
CAN_LOG_MAX_ROWS = 2000
CAN_LOG_COLUMNS = ["Can ID", "Sensor time", "Camera time", "Travel (ms)", "Result", "Details"]

# (key, label, BCM GPIO, active colour) - outputs are ACTIVE-LOW (dl = on, dh = off)
OUTPUTS = [
    ("green",  "GREEN - ACTIVE",        18, "#2ecc71"),
    ("yellow", "YELLOW - DEFECT",       15, "#f39c12"),
    ("red",    "RED - CAMERA FAULT",    14, "#e74c3c"),
    ("buzzer", "BUZZER - CAMERA FAULT", 23, "#9b59b6"),
]

# Spinnaker pixel format prefix -> OpenCV code.
# OpenCV's legacy Bayer names are offset by one pixel, so an RGGB sensor (BayerRG8) needs BayerBG2BGR.
BAYER_TO_BGR = {"BayerRG": cv2.COLOR_BayerBG2BGR, "BayerGR": cv2.COLOR_BayerGB2BGR,
                "BayerGB": cv2.COLOR_BayerGR2BGR, "BayerBG": cv2.COLOR_BayerRG2BGR}
BAYER_TO_GRAY = {"BayerRG": cv2.COLOR_BayerBG2GRAY, "BayerGR": cv2.COLOR_BayerGB2GRAY,
                 "BayerGB": cv2.COLOR_BayerGR2GRAY, "BayerBG": cv2.COLOR_BayerRG2GRAY}

BTN_STYLE = "color: white; font-weight: bold; padding: 10px; border-radius: 4px;"
RED_BTN = f"background-color: #d9534f; {BTN_STYLE}"
GREEN_BTN = f"background-color: #5cb85c; {BTN_STYLE}"
VIEW_BTN = (
    "QPushButton { background-color: #2a3242; color: #8e9aaf; font-weight: bold;"
    " padding: 8px 18px; border: none; border-radius: 4px; }"
    "QPushButton:checked { background-color: #2980b9; color: white; }"
)


# ----------------------------------------------------------------------------
# GPIO (Raspberry Pi 5 `pinctrl`) - one call at a time, never raises
# ----------------------------------------------------------------------------
class GPIO:
    def __init__(self):
        self.available = shutil.which("pinctrl") is not None
        # One pinctrl process at a time: launching it from two threads at once can fail with
        # "OSError: [Errno 9] Bad file descriptor".
        self._lock = threading.Lock()

    def _run(self, *args):
        if not self.available:
            return None
        with self._lock:
            try:
                return subprocess.run(["pinctrl", *map(str, args)], capture_output=True,
                                      text=True, check=False, timeout=2)
            except Exception:
                return None

    def setup_input(self, pin):
        self._run("set", pin, "ip", "pn")

    def write(self, pin, on):
        self._run("set", pin, "op", "dl" if on else "dh")   # active-low

    def read(self, pin):
        r = self._run("get", pin)
        if r is None or r.returncode != 0:
            return None
        if "| lo" in r.stdout:
            return "LOW"
        if "| hi" in r.stdout:
            return "HIGH"
        return None


# ----------------------------------------------------------------------------
# Can ID matching (sensor -> camera), thread-safe
# ----------------------------------------------------------------------------
class CanMatcher:
    """Cans reach the camera in the same order they passed the sensor (FIFO)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._reset()

    def _reset(self):
        self.next_id = 0
        self.waiting = deque()               # [(can_id, sensor_time)] not yet seen by the camera
        self.travel = deque(maxlen=20)       # travel times of matched cans (display)
        self.sensor_hist = deque(maxlen=200) # recent sensor times
        self.camera_hist = deque(maxlen=30)  # recent camera sighting times
        self._tt = EXPECTED_TRAVEL_S         # replaced by the measured value as soon as it's known
        self.sensor_count = self.camera_count = self.matched = self.missed = self.camera_only = 0

    def reset(self):
        with self._lock:
            self._reset()

    def typical_travel(self):
        return self._tt

    def _estimate_travel(self):
        """Sensor -> camera travel time measured WITHOUT relying on the matching: compare every
        recent camera sighting with every recent sensor time; the true travel time is the gap
        that keeps repeating (wrong pairings scatter, because cans aren't evenly spaced).
        So a can missed early on can never poison the estimate."""
        if len(self.camera_hist) < 3:
            return
        diffs = sorted(c - s for c in self.camera_hist for s in self.sensor_hist
                       if 0.0 < c - s <= MAX_TRAVEL_S)
        if not diffs:
            return
        tol = 0.03
        windows, j = [], 0
        for i in range(len(diffs)):                       # count per 2*tol window (two pointers)
            while diffs[i] - diffs[j] > 2 * tol:
                j += 1
            windows.append((i - j + 1, j, i))
        best_n = max(w[0] for w in windows)
        # Only trust it when most recent sightings agree on the same travel time
        if best_n < max(3, 0.6 * len(self.camera_hist)):
            return
        strong = [w for w in windows if w[0] >= 0.8 * best_n]
        mean = lambda w: sum(diffs[w[1]:w[2] + 1]) / w[0]
        if self.travel:                                   # near-ties (evenly spaced cans):
            ref = sorted(self.travel)[len(self.travel) // 2]   # stay closest to matched cans
            pick = min(strong, key=lambda w: abs(mean(w) - ref))
        else:
            pick = max(windows, key=lambda w: w[0])
        self._tt = mean(pick)

    def _tolerance(self):
        gaps = sorted(b - a for a, b in zip(list(self.sensor_hist)[:-1], list(self.sensor_hist)[1:]))
        if not gaps:
            return MATCH_TOLERANCE_S
        return min(MATCH_TOLERANCE_S, 0.4 * gaps[len(gaps) // 2])

    def timeout(self):
        tt = self.typical_travel()
        return MAX_TRAVEL_S if tt is None else min(MAX_TRAVEL_S, tt * 1.5 + 0.5)

    def sensor_can(self, t):
        with self._lock:
            self.next_id += 1
            self.sensor_count += 1
            self.waiting.append((self.next_id, t))
            self.sensor_hist.append(t)
            return {"id": self.next_id, "event": "sensor", "sensor_t": t}

    def camera_can(self, t):
        """Match a can arriving under the camera to its sensor Can ID.
        Before the travel time is known: oldest waiting ID (cans can't overtake on a belt).
        After: the waiting ID whose expected arrival (sensor time + travel time) is closest to now.
        Any older IDs skipped over were not seen by the camera -> reported as missed, so one
        missed can never shifts the IDs of all the cans behind it."""
        with self._lock:
            self.camera_count += 1
            self.camera_hist.append(t)
            try:
                self._estimate_travel()
            except Exception:
                pass                                  # keep the last estimate; never stop the camera
            if not self.waiting:
                self.camera_only += 1
                return {"id": None, "event": "camera_only", "camera_t": t, "skipped": []}
            pick = 0
            tt = self._tt
            if tt is not None:
                errs = [abs(t - (ts + tt)) for _, ts in self.waiting]
                pick = min(range(len(errs)), key=errs.__getitem__)
                if errs[pick] > self._tolerance():
                    # not when any waiting can is due: something the sensor didn't count
                    self.camera_only += 1
                    return {"id": None, "event": "camera_only", "camera_t": t, "skipped": []}
            skipped = []
            for _ in range(pick):
                sid, sts = self.waiting.popleft()
                self.missed += 1
                skipped.append({"id": sid, "event": "missed", "sensor_t": sts})
            can_id, ts = self.waiting.popleft()
            self.matched += 1
            self.travel.append(t - ts)
            return {"id": can_id, "event": "seen", "sensor_t": ts, "camera_t": t, "travel": t - ts,
                    "skipped": skipped}

    def check_timeouts(self, now):
        out = []
        with self._lock:
            limit = self.timeout()
            while self.waiting and now - self.waiting[0][1] > limit:
                can_id, ts = self.waiting.popleft()
                self.missed += 1
                out.append({"id": can_id, "event": "missed", "sensor_t": ts})
        return out


# ----------------------------------------------------------------------------
# Sensor thread: one event per can (leading edge), re-arm protects against flicker
# ----------------------------------------------------------------------------
class SensorThread(QThread):
    can_event = pyqtSignal(dict)       # matcher event for a new can
    state = pyqtSignal(bool)           # True = can in front of the sensor

    def __init__(self, gpio, matcher):
        super().__init__()
        self.gpio = gpio
        self.matcher = matcher
        self._running = True

    def run(self):
        present, clear_since = False, None
        while self._running:
            try:                                   # an exception here would abort PyQt6
                s = self.gpio.read(SENSOR_PIN)
                now = time.monotonic()
                if s is not None:
                    if s == SENSOR_ACTIVE_STATE:
                        clear_since = None
                        if not present:
                            present = True
                            self.can_event.emit(self.matcher.sensor_can(now))
                            self.state.emit(True)
                    elif present:
                        if clear_since is None:
                            clear_since = now
                        elif (now - clear_since) * 1000 >= PASS_REARM_MS:
                            present = False
                            self.state.emit(False)
            except Exception:
                pass
            if SENSOR_POLL_S > 0:
                time.sleep(SENSOR_POLL_S)

    def stop(self):
        self._running = False
        self.wait()


# ----------------------------------------------------------------------------
# Camera detection zone ("virtual sensor")
# ----------------------------------------------------------------------------
class ZoneDetector:
    """'Can in zone' = the zone differs from the empty-belt background in EVERY thin slice
    across the belt. A gap narrower than the zone therefore still clears it."""

    def __init__(self):
        self.background = None
        self.on_level = ZONE_ON_LEVEL
        self.present = False
        self._streak = 0
        self.score = 0.0
        self.capture_requested = True       # learn the background on the first frame

    @staticmethod
    def zone_pixels(gray, zone):
        h, w = gray.shape[:2]
        x1, y1 = int(zone[0] * w), int(zone[1] * h)
        x2, y2 = max(x1 + 1, int(zone[2] * w)), max(y1 + 1, int(zone[3] * h))
        roi = gray[y1:y2, x1:x2]
        scale = min(1.0, 120.0 / max(roi.shape))
        if scale < 1.0:
            roi = cv2.resize(roi, (max(1, int(roi.shape[1] * scale)), max(1, int(roi.shape[0] * scale))),
                             interpolation=cv2.INTER_AREA)
        return cv2.GaussianBlur(roi, (5, 5), 0).astype(np.float32)

    def update(self, gray, zone):
        """Returns True/False when the zone state CHANGES, else None."""
        roi = self.zone_pixels(gray, zone)
        if self.capture_requested or self.background is None or self.background.shape != roi.shape:
            self.background = roi.copy()
            self.capture_requested = False
            self.present, self._streak = False, 0
        diff = np.abs(roi - self.background)
        profile = diff.mean(axis=0) if roi.shape[0] >= roi.shape[1] else diff.mean(axis=1)
        if profile.size >= 3:
            profile = np.convolve(profile, np.ones(3) / 3, mode="valid")   # ignore 1-slice noise
        self.score = float(profile.min())

        want = self.score >= self.on_level if not self.present \
            else self.score >= self.on_level * ZONE_OFF_RATIO
        if not self.present and BACKGROUND_ADAPT > 0 and not want:
            self.background += BACKGROUND_ADAPT * (roi - self.background)
        if want != self.present:
            self._streak += 1
            if self._streak >= (ZONE_MIN_FRAMES if want else 1):
                self.present, self._streak = want, 0
                return self.present
        else:
            self._streak = 0
        return None


def raw_to_gray(raw, fmt):
    if raw.dtype != np.uint8:
        raw = (raw / 256).astype(np.uint8)
    if raw.ndim == 3 and raw.shape[2] == 1:
        raw = raw[:, :, 0]
    if raw.ndim == 2:
        return cv2.cvtColor(raw, BAYER_TO_GRAY[fmt[:7]]) if fmt[:7] in BAYER_TO_GRAY else raw
    return cv2.cvtColor(raw, cv2.COLOR_RGB2GRAY if fmt.startswith("RGB") else cv2.COLOR_BGR2GRAY)


def raw_to_bgr(raw, fmt):
    if raw.dtype != np.uint8:
        raw = (raw / 256).astype(np.uint8)
    if raw.ndim == 3 and raw.shape[2] == 1:
        raw = raw[:, :, 0]
    if raw.ndim == 2:
        return cv2.cvtColor(raw, BAYER_TO_BGR.get(fmt[:7], cv2.COLOR_GRAY2BGR))
    return cv2.cvtColor(raw, cv2.COLOR_RGB2BGR) if fmt.startswith("RGB") else raw


# ----------------------------------------------------------------------------
# Frame hand-off camera -> YOLO: newest frame + frames that must be inspected
# ----------------------------------------------------------------------------
class FrameMailbox:
    def __init__(self):
        self._cv = threading.Condition()
        self._latest = None
        self._must = deque()

    def put(self, job, must=False):
        with self._cv:
            if must:
                self._must.append(job)
            else:
                self._latest = job                 # older un-inspected frames are dropped
            self._cv.notify()

    def get(self, timeout=0.2):
        with self._cv:
            if not self._must and self._latest is None:
                self._cv.wait(timeout)
            if self._must:
                return self._must.popleft()
            job, self._latest = self._latest, None
            return job

    def backlog(self):
        with self._cv:
            return len(self._must)


class CameraFault(Exception):
    pass


class CameraThread(QThread):
    """Owns the PySpin camera. Every frame: zone check + Can ID matching, then hands the frame
    (stamped with its Can ID) to YOLO. Reconnects automatically after a failure."""
    status_update = pyqtSignal(str)
    feed_state = pyqtSignal(bool)
    camera_ok = pyqtSignal()
    camera_fault = pyqtSignal(str)
    zone_state = pyqtSignal(bool, float)       # in zone?, score
    can_entered = pyqtSignal(dict)             # matcher event (seen / camera_only)
    can_left = pyqtSignal(object, float)       # can_id (or None), monotonic time

    def __init__(self, matcher, detector, mailbox, camera_index=0):
        super().__init__()
        self.matcher = matcher
        self.detector = detector
        self.mailbox = mailbox
        self.camera_index = camera_index
        self.zone = ZONE
        self.zone_can_id = None                # Can ID currently in the zone (read by the UI)
        self._running = True
        self._acquiring = False
        self._last_score_emit = 0.0

    def _sleep(self, seconds):
        end = time.monotonic() + seconds
        while self._running and time.monotonic() < end:
            self.msleep(50)

    def run(self):
        if PySpin is None:
            self.camera_fault.emit("PySpin not installed - cannot open the camera.")
            return
        system = PySpin.System.GetInstance()
        try:
            while self._running:
                cam_list = system.GetCameras()
                cam = None
                try:
                    if cam_list.GetSize() <= self.camera_index:
                        raise CameraFault("No Teledyne/FLIR camera found. Check cable / IP config.")
                    cam = cam_list.GetByIndex(self.camera_index)
                    cam.Init()
                    self._configure(cam)
                    cam.BeginAcquisition()
                    self._acquiring = True
                    self.feed_state.emit(True)
                    self.camera_ok.emit()
                    self.status_update.emit("Camera streaming.")
                    self._loop(cam)
                except Exception as e:
                    self.camera_fault.emit(str(e))
                finally:
                    self._leave_zone(time.monotonic())
                    if cam is not None:
                        try:
                            if self._acquiring:
                                cam.EndAcquisition()
                        except Exception:
                            pass
                        try:
                            if cam.IsInitialized():
                                cam.DeInit()
                        except Exception:
                            pass
                        del cam
                    if self._acquiring:
                        self._acquiring = False
                        self.feed_state.emit(False)
                    cam_list.Clear()
                    del cam_list
                if self._running:
                    self.status_update.emit(f"Retrying camera in {CAMERA_RETRY_S:.0f}s...")
                    self._sleep(CAMERA_RETRY_S)
        finally:
            try:
                system.ReleaseInstance()
            except Exception:
                pass
            self.status_update.emit("Camera session closed.")

    def _configure(self, cam):
        try:
            cam.AcquisitionMode.SetValue(PySpin.AcquisitionMode_Continuous)
        except Exception as e:
            self.status_update.emit(f"Could not set continuous mode: {e}")
        try:
            s_nodemap = cam.GetTLStreamNodeMap()
            handling = PySpin.CEnumerationPtr(s_nodemap.GetNode("StreamBufferHandlingMode"))
            handling.SetIntValue(handling.GetEntryByName("NewestOnly").GetValue())
        except Exception as e:
            self.status_update.emit(f"Could not set NewestOnly buffering: {e}")

    def _loop(self, cam):
        errors = 0
        fps_t, fps_n = time.monotonic(), 0
        while self._running:
            img = None
            try:
                img = cam.GetNextImage(GET_NEXT_IMAGE_TIMEOUT_MS)
                t = time.monotonic()
                if img.IsIncomplete():
                    raise RuntimeError(f"incomplete image (status {img.GetImageStatus()})")
                try:
                    fmt = img.GetPixelFormatName()
                except Exception:
                    fmt = ""
                raw = img.GetNDArray().copy()
            except Exception as e:
                errors += 1
                self.status_update.emit(f"Grab error {errors}/{FAULT_AFTER_ERRORS}: {e}")
                if errors >= FAULT_AFTER_ERRORS:
                    raise CameraFault(f"Acquisition failed {errors}x in a row: {e}")
                continue
            finally:
                if img is not None:
                    try:
                        img.Release()
                    except Exception:
                        pass
            errors = 0
            self.process_frame(raw, fmt, t)
            fps_n += 1
            if t - fps_t >= 30:
                self.status_update.emit(f"Camera zone check: {fps_n / (t - fps_t):.0f} frames/s")
                fps_t, fps_n = t, 0

    def process_frame(self, raw, fmt, t):
        """Zone check + Can ID matching on EVERY frame (fast), then hand to YOLO."""
        change = self.detector.update(raw_to_gray(raw, fmt), self.zone)
        must = False
        if change is True:
            ev = self.matcher.camera_can(t)
            self.zone_can_id = ev["id"]
            self.can_entered.emit(ev)
            must = ev["id"] is not None                 # guarantee YOLO sees this can
        elif change is False:
            self._leave_zone(t)
        if change is not None or t - self._last_score_emit >= 0.1:
            self._last_score_emit = t
            self.zone_state.emit(self.detector.present, self.detector.score)
        self.mailbox.put({"raw": raw, "fmt": fmt, "t": t, "wall": time.time(),
                          "can_id": self.zone_can_id, "zone": self.zone,
                          "in_zone": self.detector.present}, must=must)

    def _leave_zone(self, t):
        if self.detector.present or self.zone_can_id is not None:
            cid, self.zone_can_id = self.zone_can_id, None
            self.can_left.emit(cid, t)
        self.detector.present = False

    def stop(self):
        self._running = False
        self.wait()


# ----------------------------------------------------------------------------
# YOLO thread
# ----------------------------------------------------------------------------
class YoloThread(QThread):
    frame_ready = pyqtSignal(object, dict)     # QImage (or None), metrics
    status_update = pyqtSignal(str)
    model_ready = pyqtSignal(bool)

    def __init__(self, model_path, conf_thresh, mailbox):
        super().__init__()
        self.model_path = model_path
        self.conf_thresh = conf_thresh
        self.mailbox = mailbox
        self.display_enabled = True
        self.display_size = (DISPLAY_MAX_WIDTH, 100000)
        self._running = True
        self._idx = 0

    def run(self):
        self.status_update.emit(f"Loading YOLO model from '{self.model_path}'...")
        try:
            if YOLO is None:
                raise RuntimeError("ultralytics not installed")
            model = YOLO(self.model_path)
            good = [n for n in model.names.values() if GOOD_KEYWORD in str(n).lower()]
            self.status_update.emit(f"Model loaded. Good classes: {good}")
            self.model_ready.emit(True)
        except Exception as e:
            self.status_update.emit(f"Failed to load YOLO model: {e}")
            self.model_ready.emit(False)
            return
        while self._running:
            job = self.mailbox.get(timeout=0.2)
            if job is None:
                continue
            try:
                frame = raw_to_bgr(job["raw"], job["fmt"])
                r = model.predict(frame, conf=self.conf_thresh, verbose=False)[0]
                metrics = self.extract(r, model, good)
                metrics.update(frame_idx=self._idx, grab_t=job["t"], time=job["wall"],
                               can_id=job["can_id"], in_zone=job["in_zone"])
                qimg = self.render(frame, metrics, job["zone"]) if self.display_enabled else None
                self.frame_ready.emit(qimg, metrics)
                self._idx += 1
            except Exception as e:
                self.status_update.emit(f"Inference error: {e}")

    @staticmethod
    def extract(r, model, good_classes):
        detected, boxes, is_defect = [], [], False
        if r.boxes is not None and len(r.boxes) > 0:
            for box in r.boxes:
                name = model.names[int(box.cls)]
                conf = float(box.conf)
                is_good = name in good_classes
                is_defect |= not is_good
                detected.append((name, conf, is_good))
                boxes.append((*box.xyxy[0].tolist(), name, conf, is_good))
        return {"is_defect": is_defect, "detections": detected, "boxes": boxes}

    def render(self, frame, metrics, zone):
        """Downscale first, then draw (cheap), then a ready QImage for the UI."""
        h, w = frame.shape[:2]
        s = min(1.0, min(DISPLAY_MAX_WIDTH, max(160, self.display_size[0])) / w,
                max(120, self.display_size[1]) / h)
        out = cv2.resize(frame, (int(w * s), int(h * s)), interpolation=cv2.INTER_LINEAR) \
            if s < 1.0 else frame.copy()
        oh, ow = out.shape[:2]
        F = cv2.FONT_HERSHEY_SIMPLEX
        GREEN, RED, GRAY = (0, 200, 0), (0, 0, 255), (110, 110, 110)
        fs = max(0.5, 0.6 * ow / 1000)
        for x1, y1, x2, y2, name, conf, is_good in metrics["boxes"]:
            x1, y1, x2, y2 = int(x1 * s), int(y1 * s), int(x2 * s), int(y2 * s)
            c = GREEN if is_good else RED
            cv2.rectangle(out, (x1, y1), (x2, y2), c, 2)
            text = f"{name} {conf:.2f}"
            (tw, th), base = cv2.getTextSize(text, F, fs, 1)
            top = max(0, y1 - th - base - 6)
            cv2.rectangle(out, (x1, top), (x1 + tw + 8, top + th + base + 6), c, -1)
            cv2.putText(out, text, (x1 + 4, top + th + 2), F, fs, (255, 255, 255), 1, cv2.LINE_AA)
        # detection zone + the Can ID inside it
        zc = (0, 140, 255) if metrics["in_zone"] else (255, 200, 0)
        zx1, zy1, zx2, zy2 = int(zone[0] * ow), int(zone[1] * oh), int(zone[2] * ow), int(zone[3] * oh)
        cv2.rectangle(out, (zx1, zy1), (zx2, zy2), zc, 2)
        tag = f"Can #{metrics['can_id']}" if metrics["can_id"] else ("CAN (no ID)" if metrics["in_zone"] else "zone")
        cv2.putText(out, tag, (zx1, max(18, zy1 - 8)), F, fs * 1.1, zc, 2, cv2.LINE_AA)
        # banner
        n = len(metrics["detections"])
        banner = f"Frame {metrics['frame_idx']}: " + (
            "No Detection" if not n else f"{'DEFECT' if metrics['is_defect'] else 'GOOD'} ({n} objects)")
        bc = GRAY if not n else (RED if metrics["is_defect"] else GREEN)
        (tw, th), base = cv2.getTextSize(banner, F, fs, 1)
        cv2.rectangle(out, (0, 0), (tw + 16, th + base + 16), bc, -1)
        cv2.putText(out, banner, (8, th + 8), F, fs, (255, 255, 255), 1, cv2.LINE_AA)
        rgb = cv2.cvtColor(out, cv2.COLOR_BGR2RGB)
        return QImage(rgb.data, ow, oh, 3 * ow, QImage.Format.Format_RGB888).copy()

    def stop(self):
        self._running = False
        self.wait()


# ----------------------------------------------------------------------------
# Per-can YOLO evidence
# ----------------------------------------------------------------------------
class CanRecord:
    def __init__(self, can_id, sensor_t=None):
        self.can_id = can_id
        self.sensor_t = sensor_t
        self.camera_t = None
        self.travel = None
        self.left_t = None                     # left the camera zone (monotonic)
        self.frames = 0
        self.defect_frames = 0
        self.defects = {}                      # class -> max conf
        self.best_good = ("", 0.0)
        self.status = "waiting"                # waiting -> inspecting -> done / missed
        self.items = None                      # Can Log row widgets

    def add(self, detections):
        self.frames += 1
        bad = False
        for name, conf, good in detections:
            if good:
                if conf > self.best_good[1]:
                    self.best_good = (str(name), conf)
            else:
                bad = True
                self.defects[str(name)] = max(self.defects.get(str(name), 0.0), conf)
        self.defect_frames += bad

    def defect_summary(self):
        return ", ".join(f"{n} {c:.2f}" for n, c in sorted(self.defects.items(), key=lambda kv: -kv[1]))


# ----------------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self, model_path, conf_thresh):
        super().__init__()
        self.setWindowTitle("Yards Can Inspection Station")
        self.resize(1500, 900)
        self.model_path = model_path
        self.conf_thresh = conf_thresh
        self.wall0 = time.time() - time.monotonic()

        # Shared pieces
        self.matcher = CanMatcher()
        self.detector = ZoneDetector()
        self.mailbox = FrameMailbox()
        self.gpio = GPIO()
        self.camera = None
        self.yolo = None
        self.sensor = None

        # Stack light state
        self.output_map = {o[0]: o for o in OUTPUTS}
        self.light_states = {o[0]: False for o in OUTPUTS}
        self.auto_lights = True
        self.camera_ready = False
        self.camera_faulted = False
        self.defect_in_view = False
        self.clean_frames = 0

        # Cans
        self.cans = {}                         # can_id -> CanRecord
        self.pending_final = {}                # can_id -> time it left the zone
        self.total_frames = 0
        self.cans_good = self.cans_defect = 0
        self.pass_times = deque()
        self._pending_qimg = None
        self._last_qimg = None
        self._live_state = None

        # Timers
        self.buzzer_timer = QTimer(self, singleShot=True)
        self.buzzer_timer.timeout.connect(lambda: self.set_light("buzzer", False))
        self.chase_timer = QTimer(self)
        self.chase_timer.timeout.connect(self._chase_step)
        self.chase_step = 0
        self.tick_timer = QTimer(self)                 # missed / finalize / counters, 5x per second
        self.tick_timer.timeout.connect(self._tick)
        self.tick_timer.start(200)
        self.display_timer = QTimer(self)
        self.display_timer.timeout.connect(self._paint_pending_frame)
        self.display_timer.start(40)

        self._build_ui()

        if self.gpio.available:
            self.gpio.setup_input(SENSOR_PIN)
            self.log_message(f"GPIO ready. Sensor on GPIO {SENSOR_PIN} (pin {SENSOR_PHYSICAL_PIN}).")
            self.sensor = SensorThread(self.gpio, self.matcher)
            self.sensor.can_event.connect(self.on_sensor_can)
            self.sensor.state.connect(self.on_sensor_state)
            self.sensor.start()
        else:
            self.log_message("WARNING: 'pinctrl' not found - GPIO disabled (sensor / lights in UI only).")
        self.all_lights_off(force=True)
        self._start_inspection()

    # -- UI construction -----------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)

        # Left: Live View / Can Log / Prediction Log
        left = QVBoxLayout()
        bar = QHBoxLayout()
        self.view_buttons = []
        self.view_group = QButtonGroup(self)
        self.view_group.setExclusive(True)
        for i, name in enumerate(("Live View", "Can Log", "Prediction Log")):
            b = QPushButton(name)
            b.setCheckable(True)
            b.setStyleSheet(VIEW_BTN)
            self.view_group.addButton(b, i)
            bar.addWidget(b)
            self.view_buttons.append(b)
        self.view_group.idClicked.connect(self.set_view)
        bar.addStretch()
        self.lbl_log_filter = QLabel("Log:")
        bar.addWidget(self.lbl_log_filter)
        self.cmb_log_filter = QComboBox()
        self.cmb_log_filter.addItems(["All frames", "Detections only", "Defects only", "Can events only"])
        bar.addWidget(self.cmb_log_filter)
        self.btn_log_clear = QPushButton("Clear Log")
        self.btn_log_clear.clicked.connect(self.clear_current_log)
        self.btn_log_save = QPushButton("Save Log...")
        self.btn_log_save.clicked.connect(self.save_current_log)
        bar.addWidget(self.btn_log_clear)
        bar.addWidget(self.btn_log_save)
        left.addLayout(bar)

        self.video_label = QLabel("Waiting for camera and model...")
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setStyleSheet("background-color:#111; color:#fff; font-size:16px; border-radius:4px;")
        self.video_label.setMinimumSize(850, 650)

        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        mono.setPointSize(11)
        self.can_table = QTableWidget(0, len(CAN_LOG_COLUMNS))
        self.can_table.setHorizontalHeaderLabels(CAN_LOG_COLUMNS)
        self.can_table.verticalHeader().setVisible(False)
        self.can_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.can_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        hdr = self.can_table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hdr.setStretchLastSection(True)
        self.can_table.setFont(mono)
        self.can_table.setStyleSheet(
            "QTableWidget { background-color:#111; color:#e0e0e0; gridline-color:#2a3242; }"
            "QHeaderView::section { background-color:#2a3242; color:white; font-weight:bold;"
            " padding:6px; border:none; }")

        self.pred_log = QPlainTextEdit(readOnly=True)
        self.pred_log.setMaximumBlockCount(PRED_LOG_MAX_LINES)
        self.pred_log.setFont(mono)
        self.pred_log.setStyleSheet("background-color:#111; color:#e0e0e0; padding:6px;")
        self._pred_buffer = []
        self.pred_flush = QTimer(self)
        self.pred_flush.timeout.connect(self._flush_pred_log)
        self.pred_flush.start(250)

        self.view_stack = QStackedWidget()
        for wdg in (self.video_label, self.can_table, self.pred_log):
            self.view_stack.addWidget(wdg)
        left.addWidget(self.view_stack, stretch=1)
        main_layout.addLayout(left, stretch=3)
        QShortcut(QKeySequence("Ctrl+Tab"), self, activated=lambda: self.set_view(
            (self.view_stack.currentIndex() + 1) % self.view_stack.count()))

        # Right side (scrollable)
        right_w = QWidget()
        right = QVBoxLayout(right_w)

        ctrl = QGroupBox("Inspection Controls")
        cl = QVBoxLayout(ctrl)
        self.btn_toggle = QPushButton("Stop Inspection")
        self.btn_toggle.setStyleSheet(RED_BTN)
        self.btn_toggle.clicked.connect(self.toggle_inspection)
        cl.addWidget(self.btn_toggle)
        self.lbl_conf_val = QLabel(f"YOLO confidence threshold: {self.conf_thresh:.2f}")
        cl.addWidget(self.lbl_conf_val)
        self.slider_conf = QSlider(Qt.Orientation.Horizontal)
        self.slider_conf.setRange(1, 99)
        self.slider_conf.setValue(int(self.conf_thresh * 100))
        self.slider_conf.valueChanged.connect(self.update_confidence)
        cl.addWidget(self.slider_conf)
        right.addWidget(ctrl)

        status = QGroupBox("Status")
        sl = QGridLayout(status)
        self.lbl_sensor = QLabel(f"SENSOR (pin {SENSOR_PHYSICAL_PIN}): clear")
        self.lbl_zone = QLabel("CAMERA ZONE: empty")
        self.lbl_camera = QLabel("CAMERA: CONNECTING...")
        self.lbl_model = QLabel("MODEL: LOADING...")
        for i, lbl in enumerate((self.lbl_sensor, self.lbl_zone, self.lbl_camera, self.lbl_model)):
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._badge(lbl, "#34495e")
            sl.addWidget(lbl, i // 2, i % 2)
        right.addWidget(status)

        counts = QGroupBox("Cans")
        gl = QGridLayout(counts)
        big = QFont()
        big.setPointSize(22)
        big.setBold(True)
        self.counters = {}
        for i, (key, label, color) in enumerate([
                ("sensor", "Sensor count", "#5bc0de"), ("good", "GOOD", "#5cb85c"),
                ("defect", "DEFECT", "#ff5e3a"), ("waiting", "On the way", "#f0ad4e"),
                ("missed", "MISSED by camera", "#d9534f"), ("camera_only", "Camera only", "#aaaaaa")]):
            name = QLabel(label)
            val = QLabel("0")
            val.setFont(big)
            val.setStyleSheet(f"color:{color};")
            gl.addWidget(name, (i // 3) * 2, i % 3)
            gl.addWidget(val, (i // 3) * 2 + 1, i % 3)
            self.counters[key] = val
        self.lbl_travel = QLabel()
        gl.addWidget(self.lbl_travel, 4, 0, 1, 3)
        self.lbl_verdict = QLabel("Last can: --")
        self.lbl_verdict.setStyleSheet("font-size:15px; font-weight:bold;")
        gl.addWidget(self.lbl_verdict, 5, 0, 1, 3)
        self.lbl_live = QLabel("Live: --")
        self.lbl_live.setStyleSheet("font-size:14px; font-weight:bold;")
        gl.addWidget(self.lbl_live, 6, 0, 1, 3)
        btn_reset = QPushButton("Reset Counts")
        btn_reset.clicked.connect(self.reset_counts)
        gl.addWidget(btn_reset, 7, 0, 1, 3)
        right.addWidget(counts)

        zbox = QGroupBox("Camera Detection Zone")
        zl = QGridLayout(zbox)
        zl.addWidget(QLabel("Image change:"), 0, 0)
        self.score_bar = QProgressBar()
        self.score_bar.setRange(0, 100)
        self.score_bar.setFormat("%v")
        zl.addWidget(self.score_bar, 0, 1, 1, 2)
        zl.addWidget(QLabel("Trigger level:"), 1, 0)
        self.sld_level = QSlider(Qt.Orientation.Horizontal)
        self.sld_level.setRange(3, 100)
        self.sld_level.setValue(ZONE_ON_LEVEL)
        self.sld_level.valueChanged.connect(self.on_level_changed)
        zl.addWidget(self.sld_level, 1, 1)
        self.lbl_level = QLabel(str(ZONE_ON_LEVEL))
        zl.addWidget(self.lbl_level, 1, 2)
        zl.addWidget(QLabel("Position:"), 2, 0)
        self.sld_pos = QSlider(Qt.Orientation.Horizontal)
        self.sld_pos.setRange(0, 100)
        self.sld_pos.setValue(int((ZONE[0] + ZONE[2]) / 2 * 100))
        self.sld_pos.valueChanged.connect(self.on_zone_slider)
        zl.addWidget(self.sld_pos, 2, 1, 1, 2)
        zl.addWidget(QLabel("Width:"), 3, 0)
        self.sld_width = QSlider(Qt.Orientation.Horizontal)
        self.sld_width.setRange(1, 50)
        self.sld_width.setValue(max(1, int((ZONE[2] - ZONE[0]) * 100)))
        self.sld_width.valueChanged.connect(self.on_zone_slider)
        zl.addWidget(self.sld_width, 3, 1, 1, 2)
        btn_bg = QPushButton("Capture Empty Belt")
        btn_bg.setToolTip("Press with NO can in the zone. Re-press if the lighting changes.")
        btn_bg.clicked.connect(self.capture_background)
        zl.addWidget(btn_bg, 4, 0, 1, 3)
        right.addWidget(zbox)

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
            ind.setStyleSheet("color:#4f5b6e; font-size:20px;")
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

        log = QGroupBox("System Log")
        lgl = QVBoxLayout(log)
        self.console_log = QTextEdit(readOnly=True)
        self.console_log.document().setMaximumBlockCount(500)
        self.console_log.setMinimumHeight(140)
        lgl.addWidget(self.console_log)
        right.addWidget(log, stretch=1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(right_w)
        scroll.setMinimumWidth(460)
        main_layout.addWidget(scroll, stretch=1)

        self._update_light_buttons_enabled()
        self.set_view(0)
        self.refresh_counts()

    @staticmethod
    def _badge(label, bg):
        label.setStyleSheet(f"background-color:{bg}; color:white; font-weight:bold; padding:6px;")

    # -- start / stop ----------------------------------------------------------
    def _start_inspection(self):
        self.yolo = YoloThread(self.model_path, self.conf_thresh, self.mailbox)
        self.yolo.frame_ready.connect(self.handle_new_frame)
        self.yolo.status_update.connect(self.log_message)
        self.yolo.model_ready.connect(self.on_model_ready)
        self.yolo.display_enabled = self.view_stack.currentIndex() == 0
        self.yolo.display_size = (self.video_label.width(), self.video_label.height())
        self.yolo.start()

        self.camera = CameraThread(self.matcher, self.detector, self.mailbox)
        self.camera.zone = self._zone_from_sliders()
        self.camera.status_update.connect(self.log_message)
        self.camera.feed_state.connect(self.on_feed_state)
        self.camera.camera_ok.connect(self.on_camera_ok)
        self.camera.camera_fault.connect(self.on_camera_fault)
        self.camera.zone_state.connect(self.on_zone_state)
        self.camera.can_entered.connect(self.on_camera_can)
        self.camera.can_left.connect(self.on_can_left)
        self.lbl_camera.setText("CAMERA: CONNECTING...")
        self._badge(self.lbl_camera, "#34495e")
        self.camera.start()

    def _stop_inspection(self):
        if self.camera is not None:
            self.camera.stop()
            self.camera = None
        if self.yolo is not None:
            self.yolo.stop()
            self.yolo = None
        self._finalize_all()

    def toggle_inspection(self):
        if self.camera is not None:
            self._stop_inspection()
            self.camera_ready = self.camera_faulted = self.defect_in_view = False
            self.lbl_camera.setText("CAMERA: STOPPED")
            self._badge(self.lbl_camera, "#34495e")
            self.apply_auto_lights()
            self.btn_toggle.setText("Resume Inspection")
            self.btn_toggle.setStyleSheet(GREEN_BTN)
            self.log_message("Inspection paused.")
        else:
            self._start_inspection()
            self.btn_toggle.setText("Stop Inspection")
            self.btn_toggle.setStyleSheet(RED_BTN)
            self.log_message("Inspection resumed.")

    # -- camera / model health -------------------------------------------------
    def on_model_ready(self, ok):
        self.lbl_model.setText("MODEL: READY" if ok else "MODEL: FAILED")
        self._badge(self.lbl_model, "#27ae60" if ok else "#c0392b")

    def on_camera_ok(self):
        if self.camera_faulted:
            self.log_message("Camera recovered.")
            self._log_event("CAMERA RECOVERED", "#27ae60")
        self.camera_ready, self.camera_faulted = True, False
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
        self.camera_ready, self.camera_faulted, self.defect_in_view = False, True, False
        self.apply_auto_lights()
        if first and self.auto_lights:
            self.set_light("buzzer", True)
            if BUZZER_FAULT_MS > 0:
                self.buzzer_timer.start(BUZZER_FAULT_MS)

    def on_feed_state(self, active):
        if not active:
            self._set_defect_in_view(False)
            self.video_label.clear()
            self.video_label.setText("Camera feed not running (inspection stopped or camera fault).")

    # -- sensor ---------------------------------------------------------------
    def on_sensor_state(self, active):
        self.lbl_sensor.setText(f"SENSOR (pin {SENSOR_PHYSICAL_PIN}): {'CAN' if active else 'clear'}")
        self._badge(self.lbl_sensor, "#c0392b" if active else "#34495e")

    def on_sensor_can(self, ev):
        rec = CanRecord(ev["id"], ev["sensor_t"])
        self.cans[ev["id"]] = rec
        self.pass_times.append(ev["sensor_t"])
        self._add_row(rec, "On the way...", "#f0ad4e", "")
        self._log_event(f"SENSOR: CAN #{ev['id']}", "#5bc0de")
        self.refresh_counts()

    # -- camera zone ----------------------------------------------------------
    def on_zone_state(self, present, score):
        self.score_bar.setValue(int(min(100, score)))
        txt = "CAMERA ZONE: CAN" if present else "CAMERA ZONE: empty"
        if self.lbl_zone.text() != txt:
            self.lbl_zone.setText(txt)
            self._badge(self.lbl_zone, "#e67e22" if present else "#34495e")

    def on_camera_can(self, ev):
        for sk in ev.get("skipped", []):              # IDs the camera never saw
            self._mark_missed(sk["id"])
        if ev["event"] == "seen":
            rec = self.cans.get(ev["id"]) or CanRecord(ev["id"], ev["sensor_t"])
            self.cans[ev["id"]] = rec
            rec.camera_t, rec.travel, rec.status = ev["camera_t"], ev["travel"], "inspecting"
            self._update_row(rec, "Inspecting...", "#5bc0de", "")
            self._log_event(f"CAMERA: CAN #{ev['id']} under camera "
                            f"({ev['travel'] * 1000:.0f} ms after sensor)", "#5bc0de")
        else:
            rec = CanRecord(None)
            rec.camera_t, rec.status = ev["camera_t"], "done"
            self._add_row(rec, "CAMERA ONLY", "#aaaaaa", "camera saw a can the sensor did not count")
            self._log_event("CAMERA: can with no sensor ID", "#aaaaaa")
        self.refresh_counts()

    def _mark_missed(self, can_id):
        rec = self.cans.get(can_id)
        if rec is not None:
            rec.status = "missed"
            self._update_row(rec, "MISSED by camera", "#d9534f",
                             "camera never saw this can (check zone / trigger level)")
        self._log_event(f"CAN #{can_id} MISSED by camera", "#d9534f")
        self.refresh_counts()

    def on_can_left(self, can_id, t):
        if can_id is not None and can_id in self.cans:
            self.cans[can_id].left_t = t
            self.pending_final[can_id] = t       # decide once YOLO caught up past this moment

    # -- frames from YOLO -----------------------------------------------------
    def handle_new_frame(self, qimg, metrics):
        self.total_frames += 1
        is_defect = metrics["is_defect"]
        has_det = bool(metrics["detections"])

        # Stack light follows what is in view right now
        if is_defect:
            self.clean_frames = 0
            self._set_defect_in_view(True)
        else:
            self.clean_frames += 1
            if self.clean_frames >= DEFECT_CLEAR_FRAMES:
                self._set_defect_in_view(False)

        # Evidence for the can that was in the zone when this frame was grabbed
        rec = self.cans.get(metrics["can_id"]) if metrics["can_id"] else None
        if rec is not None and rec.status == "inspecting":
            rec.add(metrics["detections"])

        # Every can that left the zone before this frame was grabbed has all its frames now
        for cid, left_t in list(self.pending_final.items()):
            if metrics["grab_t"] >= left_t:
                self._finalize(cid)

        cid = metrics["can_id"]
        tag = f" (can #{cid})" if cid else ""
        if is_defect:
            state, text, color = "defect", f"Live: ⚠️ DEFECT IN VIEW{tag}", "#f0ad4e"
        elif has_det:
            state, text, color = "good", f"Live: ✔️ GOOD CAN{tag}", "#5cb85c"
        else:
            state, text, color = "none", f"Live: no detection{tag}", "#aaa"
        if self.lbl_live.text() != text:
            self.lbl_live.setText(text)
        if state != self._live_state:
            self._live_state = state
            self.lbl_live.setStyleSheet(f"color:{color}; font-size:14px; font-weight:bold;")

        self._log_prediction(metrics)
        if qimg is not None:
            self._pending_qimg = qimg

    def _finalize(self, can_id):
        self.pending_final.pop(can_id, None)
        rec = self.cans.get(can_id)
        if rec is None or rec.status != "inspecting":
            return
        rec.status = "done"
        if rec.defect_frames:
            result, color = "DEFECT", "#ff5e3a"
            details = rec.defect_summary()
            self.cans_defect += 1
        else:
            result, color = "GOOD", "#5cb85c"
            name, conf = rec.best_good
            details = f"{name} {conf:.2f}" if name else "no defect found (model saw no can box)"
            self.cans_good += 1
        details += f"   [{rec.frames} frame{'s' if rec.frames != 1 else ''}]"
        self._update_row(rec, result, color, details)
        self.lbl_verdict.setText(f"Last can: #{can_id} {result}")
        self.lbl_verdict.setStyleSheet(f"color:{color}; font-size:15px; font-weight:bold;")
        self._log_event(f"CAN #{can_id}: {result} ({details})", color)
        self.refresh_counts()
        # keep memory bounded
        done = [k for k, r in self.cans.items() if r.status in ("done", "missed")]
        for k in done[:-CAN_LOG_MAX_ROWS]:
            del self.cans[k]

    def _finalize_all(self):
        for cid in list(self.pending_final):
            self._finalize(cid)
        for cid, rec in list(self.cans.items()):
            if rec.status == "inspecting":
                self._finalize(cid)

    def _tick(self):
        now = time.monotonic()
        for ev in self.matcher.check_timeouts(now):
            self._mark_missed(ev["id"])
        for cid, left_t in list(self.pending_final.items()):     # YOLO stalled or stopped
            if now - left_t > FINALIZE_TIMEOUT_S:
                self._finalize(cid)
        if self.mailbox.backlog() > MUST_QUEUE_MAX:
            self.log_message("WARNING: YOLO is falling behind the belt (cans waiting for inspection).")
        self.refresh_counts()

    # -- display ---------------------------------------------------------------
    def _paint_pending_frame(self):
        if self._pending_qimg is None or self.view_stack.currentIndex() != 0:
            return
        img, self._pending_qimg = self._pending_qimg, None
        self._last_qimg = img
        self._set_video_pixmap(img)
        if self.yolo is not None:
            self.yolo.display_size = (self.video_label.width(), self.video_label.height())

    def _set_video_pixmap(self, img):
        pix = QPixmap.fromImage(img)
        lw, lh = self.video_label.width(), self.video_label.height()
        if pix.width() > lw or pix.height() > lh:
            pix = pix.scaled(lw, lh, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.FastTransformation)
        self.video_label.setPixmap(pix)

    def set_view(self, index):
        self.view_stack.setCurrentIndex(index)
        self.view_buttons[index].setChecked(True)
        self.lbl_log_filter.setVisible(index == 2)
        self.cmb_log_filter.setVisible(index == 2)
        self.btn_log_clear.setEnabled(index != 0)
        self.btn_log_save.setEnabled(index != 0)
        if self.yolo is not None:
            self.yolo.display_enabled = index == 0
        if index == 0:
            img = self._pending_qimg or self._last_qimg
            if img is not None:
                self._set_video_pixmap(img)
        elif index == 2:
            self._flush_pred_log()

    # -- Can Log ----------------------------------------------------------------
    def _wall(self, t):
        return datetime.fromtimestamp(t + self.wall0).strftime("%H:%M:%S.%f")[:-3] if t else "--"

    def _row_values(self, rec, result, details):
        return [f"#{rec.can_id}" if rec.can_id else "--", self._wall(rec.sensor_t),
                self._wall(rec.camera_t), f"{rec.travel * 1000:.0f}" if rec.travel is not None else "--",
                result, details]

    def _add_row(self, rec, result, color, details):
        self.can_table.insertRow(0)                       # newest on top
        items = []
        for c, v in enumerate(self._row_values(rec, result, details)):
            it = QTableWidgetItem(v)
            if c == 4:
                it.setForeground(QColor(color))
                f = it.font()
                f.setBold(True)
                it.setFont(f)
            self.can_table.setItem(0, c, it)
            items.append(it)
        rec.items = items
        while self.can_table.rowCount() > CAN_LOG_MAX_ROWS:
            self.can_table.removeRow(self.can_table.rowCount() - 1)

    def _update_row(self, rec, result, color, details):
        if rec.items is None:
            self._add_row(rec, result, color, details)
            return
        try:
            for it, v in zip(rec.items, self._row_values(rec, result, details)):
                it.setText(v)
            rec.items[4].setForeground(QColor(color))
        except RuntimeError:                               # row already scrolled out of the table
            rec.items = None

    # -- counts -------------------------------------------------------------------
    def refresh_counts(self):
        m = self.matcher
        self.counters["sensor"].setText(str(m.sensor_count))
        self.counters["good"].setText(str(self.cans_good))
        self.counters["defect"].setText(str(self.cans_defect))
        self.counters["waiting"].setText(str(len(m.waiting)))
        self.counters["missed"].setText(str(m.missed))
        self.counters["camera_only"].setText(str(m.camera_only))
        now = time.monotonic()
        while self.pass_times and now - self.pass_times[0] > PASS_RATE_WINDOW_S:
            self.pass_times.popleft()
        rate = len(self.pass_times) * 60.0 / PASS_RATE_WINDOW_S
        tt = m.typical_travel()
        self.lbl_travel.setText(
            f"Rate: {rate:.0f} cans/min   Sensor → camera: "
            + ("--" if tt is None else f"~{tt * 1000:.0f} ms (MISSED after {m.timeout():.1f} s)"))

    def reset_counts(self):
        self.matcher.reset()
        self.cans.clear()
        self.pending_final.clear()
        self.cans_good = self.cans_defect = self.total_frames = 0
        self.pass_times.clear()
        self.can_table.setRowCount(0)
        self.lbl_verdict.setText("Last can: --")
        self.lbl_verdict.setStyleSheet("font-size:15px; font-weight:bold;")
        self.refresh_counts()
        self.log_message("Counts reset (Can IDs restart at #1).")

    # -- zone controls ------------------------------------------------------------
    def on_level_changed(self, v):
        self.detector.on_level = v
        self.lbl_level.setText(str(v))

    def _zone_from_sliders(self):
        c = self.sld_pos.value() / 100.0
        half = self.sld_width.value() / 200.0
        return (max(0.0, c - half), ZONE[1], min(1.0, c + half), ZONE[3])

    def on_zone_slider(self, _=None):
        if self.camera is not None:
            self.camera.zone = self._zone_from_sliders()
        self.detector.capture_requested = True             # zone moved: re-learn the empty belt

    def capture_background(self):
        self.detector.capture_requested = True
        self.log_message("Empty belt captured - make sure no can was in the zone.")

    # -- prediction log -----------------------------------------------------------
    def _pred_line(self, html_text):
        self._pred_buffer.append(html_text)
        if len(self._pred_buffer) > PRED_LOG_MAX_LINES:
            del self._pred_buffer[:-PRED_LOG_MAX_LINES]

    def _flush_pred_log(self):
        if not self._pred_buffer or self.view_stack.currentIndex() != 2:
            return
        lines, self._pred_buffer = self._pred_buffer, []
        self.pred_log.setUpdatesEnabled(False)
        for line in lines:
            self.pred_log.appendHtml(line)
        self.pred_log.setUpdatesEnabled(True)
        sb = self.pred_log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _log_prediction(self, metrics):
        mode = self.cmb_log_filter.currentIndex()   # 0 all, 1 detections, 2 defects, 3 can events only
        dets, is_defect = metrics["detections"], metrics["is_defect"]
        if mode == 3 or (mode == 1 and not dets) or (mode == 2 and not is_defect):
            return
        ts = datetime.fromtimestamp(metrics["time"]).strftime("%H:%M:%S.%f")[:-3]
        status, color = ("DEFECT", "#f0ad4e") if is_defect else (("GOOD", "#5cb85c") if dets else ("NONE", "#777"))
        parts = [f'<span style="color:{"#5cb85c" if g else "#f0ad4e"}">{html.escape(str(n))}</span> {c:.2f}'
                 for n, c, g in dets]
        det_txt = ", ".join(parts) if parts else '<span style="color:#777">no detections</span>'
        nb = lambda s: s.replace(" ", "&nbsp;")
        can = f"#{metrics['can_id']}" if metrics["can_id"] else "-"
        self._pred_line(
            f'<span style="color:#8e9aaf">[{ts}]&nbsp;{nb(f"frame {metrics["frame_idx"]:>6}")}'
            f'&nbsp;&nbsp;{nb(f"can {can:>5}")}</span>&nbsp;&nbsp;<b style="color:{color}">'
            f'{nb(status.ljust(6))}</b>&nbsp;&nbsp;{det_txt}')

    def _log_event(self, text, color):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._pred_line(f'<b style="color:{color}">[{ts}] ==== {html.escape(text)} ====</b>')

    def clear_current_log(self):
        if self.view_stack.currentIndex() == 1:
            self.can_table.setRowCount(0)
            for rec in self.cans.values():
                rec.items = None
        else:
            self._pred_buffer.clear()
            self.pred_log.clear()

    def save_current_log(self):
        stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
        if self.view_stack.currentIndex() == 1:
            path, _ = QFileDialog.getSaveFileName(self, "Save can log", f"can_log_{stamp}.csv",
                                                  "CSV files (*.csv)")
            if not path:
                return
            try:
                with open(path, "w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    w.writerow(CAN_LOG_COLUMNS)
                    for r in range(self.can_table.rowCount() - 1, -1, -1):   # oldest first
                        w.writerow([self.can_table.item(r, c).text() for c in range(len(CAN_LOG_COLUMNS))])
                self.log_message(f"Can log saved to {path}")
            except Exception as e:
                self.log_message(f"Could not save can log: {e}")
        else:
            self._flush_pred_log()
            path, _ = QFileDialog.getSaveFileName(self, "Save prediction log", f"predictions_{stamp}.txt",
                                                  "Text files (*.txt)")
            if not path:
                return
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self.pred_log.toPlainText())
                self.log_message(f"Prediction log saved to {path}")
            except Exception as e:
                self.log_message(f"Could not save prediction log: {e}")

    # -- stack light --------------------------------------------------------------
    def _set_defect_in_view(self, value):
        if value != self.defect_in_view:
            self.defect_in_view = value
            self.apply_auto_lights()

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
        self.indicators[key].setStyleSheet(f"color:{color if on else '#4f5b6e'}; font-size:20px;")
        btn = self.light_buttons[key]
        btn.setText("ON" if on else "OFF")
        btn.setStyleSheet(f"background-color:{color}; color:white; font-weight:bold;" if on else "")

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

    # -- misc ---------------------------------------------------------------------
    def update_confidence(self, val):
        self.conf_thresh = val / 100.0
        self.lbl_conf_val.setText(f"YOLO confidence threshold: {self.conf_thresh:.2f}")
        if self.yolo is not None:
            self.yolo.conf_thresh = self.conf_thresh

    def log_message(self, msg):
        self.console_log.append(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def closeEvent(self, event):
        self._stop_inspection()
        if self.sensor is not None:
            self.sensor.stop()
        self.all_lights_off(force=True)
        event.accept()


def main():
    parser = argparse.ArgumentParser(description="Can inspection station: sensor IDs + camera zone + YOLO")
    parser.add_argument("--model", default="best.pt", help="Path to trained YOLO .pt model")
    parser.add_argument("--conf", type=float, default=0.25, help="Detection confidence threshold")
    parser.add_argument("--continuous", action="store_true", help=argparse.SUPPRESS)   # old flag, ignored
    args = parser.parse_args()

    app = QApplication(sys.argv)
    window = MainWindow(args.model, args.conf)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
