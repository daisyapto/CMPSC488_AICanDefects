"""
can_inspection_station.py - Combined inspection station UI

Merges:
  * teledyne_yolo_ui.py      - live Teledyne/FLIR (PySpin) feed with YOLO defect annotations
  * stack light controller   - sensor-triggered acquisition (GPIO 25) + stack light / buzzer outputs

Stack light (Auto mode)
  * GREEN           - system active: camera initialised and inspecting / waiting for cans
  * YELLOW          - defective can in view (GREEN turns off). When the defect leaves the
                      camera view (good can or empty frame), YELLOW turns off and GREEN returns.
  * RED + BUZZER    - camera acquisition failure (no camera, init error, repeated grab errors).
                      RED stays on until the camera recovers; the worker retries automatically.
  * all off         - inspection paused / model still loading

Trigger
  * Sensor (GPIO 25 active LOW) starts acquisition; feed is held HOLD_SECONDS after it clears.
    Each can gets one verdict (GOOD / DEFECT / NO DETECTION) for the counters.
  * Continuous mode ignores the sensor (bench testing).

Usage:
    python station/can_inspection_station_2.py --model yolo/models/best.pt --conf 0.25
    python station/can_inspection_station_2.py --model yolo/models/best.pt --continuous      # no sensor needed
"""

import sys
import argparse
from pathlib import Path
import html
import shutil
import subprocess
import threading
import time
from datetime import datetime

import numpy as np
import cv2

try:
    import PySpin
except ImportError:
    PySpin = None

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
SENSOR_PIN = 25
SENSOR_ACTIVE_STATE = "LOW"        # active-low sensor: LOW = object present
SENSOR_POLL_S = 0.02               # 50 Hz polling
SENSOR_DEBOUNCE_READS = 2          # consecutive identical reads before a change counts

HOLD_SECONDS = 15.0                # keep feed live after the sensor clears
GET_NEXT_IMAGE_TIMEOUT_MS = 1000

FAULT_AFTER_ERRORS = 5             # consecutive grab errors/timeouts before declaring a camera fault
CAMERA_RETRY_S = 3.0               # wait between camera reconnect attempts
BUZZER_FAULT_MS = 3000             # buzzer length when a camera fault starts (0 = until recovered)
DEFECT_CLEAR_FRAMES = 1            # non-defect frames needed before YELLOW -> GREEN (raise if it flickers)
CHASE_STEP_MS = 250                # manual test chase step length

GOOD_KEYWORD = "good"              # any class whose name contains this counts as "good"

PRED_LOG_MAX_LINES = 5000          # prediction log keeps the most recent N lines

# (key, label, BCM GPIO, active colour) - outputs are ACTIVE-LOW (dl = on, dh = off)
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
# GPIO (Raspberry Pi 5 `pinctrl`) - degrades to a no-op when pinctrl is missing
# ----------------------------------------------------------------------------
class GPIO:
    def __init__(self):
        self.available = shutil.which("pinctrl") is not None
        # Only one pinctrl process at a time: launching it from the sensor thread and the GUI
        # thread simultaneously can fail with "OSError: [Errno 9] Bad file descriptor".
        self._lock = threading.Lock()

    def _run(self, *args):
        if not self.available:
            return None
        with self._lock:
            try:
                return subprocess.run(["pinctrl", *map(str, args)], capture_output=True,
                                      text=True, check=False, timeout=2)
            except Exception:
                return None      # treated as UNKNOWN / skipped write, never crashes the app

    def setup_input(self, pin):
        self._run("set", pin, "ip", "pn")

    def write(self, pin, on):
        self._run("set", pin, "op", "dl" if on else "dh")   # active-low

    def read(self, pin):
        r = self._run("get", pin)
        if r is None or r.returncode != 0:
            return "UNKNOWN"
        if "| lo" in r.stdout:
            return "LOW"
        if "| hi" in r.stdout:
            return "HIGH"
        return "UNKNOWN"


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
            # An uncaught exception in a QThread aborts the whole program in PyQt6
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
# Camera + YOLO worker
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


class CameraFault(Exception):
    pass


class CameraInferenceWorker(QThread):
    """Owns the PySpin camera (all Spinnaker calls stay on this thread) and runs YOLO.
    Reconnects automatically after a camera failure."""
    frame_ready = pyqtSignal(object, dict)
    status_update = pyqtSignal(str)
    feed_state = pyqtSignal(bool)          # acquisition running?
    camera_ok = pyqtSignal()               # camera initialised and healthy
    camera_fault = pyqtSignal(str)         # acquisition failure

    def __init__(self, model_path, conf_thresh, camera_index=0, continuous=False):
        super().__init__()
        self.model_path = model_path
        self.conf_thresh = conf_thresh
        self.camera_index = camera_index
        # Written from the GUI thread, read here (plain attribute reads are atomic in CPython)
        self.continuous = continuous
        self.object_present = False
        self._running = True
        self._acquiring = False
        self._idx = 0

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
        self.status_update.emit(f"Loading YOLO model from '{self.model_path}'...")
        try:
            model = YOLO(self.model_path)
            good_classes = [n for n in model.names.values() if GOOD_KEYWORD in str(n).lower()]
            self.status_update.emit(f"Model loaded. Good classes: {good_classes}")
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
                try:
                    if cam_list.GetSize() <= self.camera_index:
                        raise CameraFault("No Teledyne/FLIR camera found. Check cable / IP config.")
                    cam = cam_list.GetByIndex(self.camera_index)
                    cam.Init()
                    self._configure_camera(cam)
                    self.status_update.emit(f"Camera {self.camera_index} initialised.")
                    self.camera_ok.emit()
                    self._acquire_loop(cam, model, good_classes)   # returns only when stopped
                except Exception as e:
                    self.camera_fault.emit(str(e))
                finally:
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

    def _acquire_loop(self, cam, model, good_classes):
        clear_time = None
        consecutive_errors = 0

        while self._running:
            want = self.continuous or self.object_present
            now = time.monotonic()

            # ---- acquisition start/stop with hold timer ----
            if want:
                clear_time = None
                if not self._acquiring:
                    try:
                        self._set_feed(cam, True)
                    except Exception as e:
                        raise CameraFault(f"BeginAcquisition failed: {e}")
                    self.status_update.emit("Acquisition started.")
            elif self._acquiring:
                if clear_time is None:
                    clear_time = now
                    self.status_update.emit(f"Object cleared - holding feed for {HOLD_SECONDS:.0f}s.")
                elif now - clear_time >= HOLD_SECONDS:
                    clear_time = None
                    self._set_feed(cam, False)
                    self.status_update.emit("Hold elapsed - acquisition stopped.")

            if not self._acquiring:
                self.msleep(20)
                continue

            # ---- grab ----
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

            # ---- infer (model errors are not camera faults) ----
            try:
                frame = to_bgr(raw, pixel_format)
                r = model.predict(frame, conf=self.conf_thresh, verbose=False)[0]
                hold_remaining = None
                if clear_time is not None:
                    hold_remaining = max(0.0, HOLD_SECONDS - (time.monotonic() - clear_time))
                annotated, metrics = self.process_detections(
                    frame, r, model, good_classes, self._idx, hold_remaining)
                metrics["counted"] = counted
                metrics["time"] = time.time()
                self.frame_ready.emit(annotated, metrics)
                self._idx += 1
            except Exception as e:
                self.status_update.emit(f"Inference error: {e}")

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
    def process_detections(self, frame, r, model, good_classes, idx, hold_remaining=None):
        out = frame.copy()
        h, w = out.shape[:2]
        scale = max(h, w) / 1000
        thick = max(2, int(round(3 * scale)))
        font_scale = max(0.5, 0.7 * scale)
        FONT = cv2.FONT_HERSHEY_SIMPLEX
        GREEN, RED, GRAY = (0, 200, 0), (0, 0, 255), (110, 110, 110)
        txt_thick = max(1, thick - 1)

        detected = []
        is_defect = False

        if r.boxes is not None and len(r.boxes) > 0:
            for box in r.boxes:
                name = model.names[int(box.cls)]
                conf = float(box.conf)
                is_good = name in good_classes
                is_defect |= not is_good

                x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                color = GREEN if is_good else RED
                cv2.rectangle(out, (x1, y1), (x2, y2), color, thick)

                text = f"{name} {conf:.2f}"
                (tw, th), base = cv2.getTextSize(text, FONT, font_scale, txt_thick)
                top = max(0, y1 - th - base - 6)
                cv2.rectangle(out, (x1, top), (x1 + tw + 8, top + th + base + 6), color, -1)
                cv2.putText(out, text, (x1 + 4, top + th + 2), FONT, font_scale,
                            (255, 255, 255), txt_thick, cv2.LINE_AA)
                detected.append((name, conf, is_good))

        if not detected:
            banner, color = f"Frame {idx}: No Detection", GRAY
        else:
            banner = f"Frame {idx}: {'DEFECT' if is_defect else 'GOOD'} ({len(detected)} objects)"
            color = RED if is_defect else GREEN

        (tw, th), base = cv2.getTextSize(banner, FONT, font_scale, txt_thick)
        cv2.rectangle(out, (0, 0), (tw + 16, th + base + 16), color, -1)
        cv2.putText(out, banner, (8, th + 8), FONT, font_scale, (255, 255, 255), txt_thick, cv2.LINE_AA)

        if hold_remaining is not None:
            hold_txt = f"Sensor clear - holding feed: {hold_remaining:.1f}s"
            (tw, th), base = cv2.getTextSize(hold_txt, FONT, font_scale, txt_thick)
            y0 = h - th - base - 16
            cv2.rectangle(out, (0, y0), (tw + 16, h), (40, 40, 40), -1)
            cv2.putText(out, hold_txt, (8, h - base - 8), FONT, font_scale,
                        (255, 255, 255), txt_thick, cv2.LINE_AA)

        return out, {"frame_idx": idx, "is_defect": is_defect, "detections": detected}

    def stop(self):
        self._running = False
        self.wait()


# ----------------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self, model_path, conf_thresh, continuous=False):
        super().__init__()
        self.setWindowTitle("Yards Can Inspection Station")
        self.resize(1400, 860)

        self.model_path = model_path
        self.conf_thresh = conf_thresh
        self.continuous = continuous

        # Hardware
        self.gpio = GPIO()
        self.output_map = {o[0]: o for o in OUTPUTS}
        self.light_states = {o[0]: False for o in OUTPUTS}
        self.auto_lights = True
        self.sensor_state = "UNKNOWN"
        self.object_present = False

        # System state that drives the auto stack light
        self.camera_ready = False      # camera initialised & healthy -> GREEN
        self.camera_faulted = False    # acquisition failure          -> RED + buzzer
        self.defect_in_view = False    # current frames show a defect -> YELLOW
        self.clean_frames = 0          # consecutive non-defect frames since last defect

        # Per-can accumulation (counters only)
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

        # Timers
        self.buzzer_timer = QTimer(self, singleShot=True)
        self.buzzer_timer.timeout.connect(lambda: self.set_light("buzzer", False))
        self.chase_timer = QTimer(self)
        self.chase_timer.timeout.connect(self._chase_step)
        self.chase_step = 0

        self._build_ui()

        if self.gpio.available:
            self.gpio.setup_input(SENSOR_PIN)
            self.log_message(f"GPIO ready (pinctrl). Sensor on GPIO {SENSOR_PIN}, active {SENSOR_ACTIVE_STATE}.")
        else:
            self.log_message("WARNING: 'pinctrl' not found - GPIO disabled (lights/sensor shown in UI only).")
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

        # Left: view switcher (Live View <-> Prediction Log)
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

        # Ctrl+Tab flips between the two views
        QShortcut(QKeySequence("Ctrl+Tab"), self, activated=lambda: self.set_view(
            1 - self.view_stack.currentIndex()))
        self._last_frame = None

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
        self.lbl_sensor = QLabel(f"SENSOR GPIO {SENSOR_PIN}: INITIALISING...")
        self.lbl_camera = QLabel("CAMERA: CONNECTING...")
        self.lbl_feed = QLabel("FEED: IDLE")
        for lbl in (self.lbl_sensor, self.lbl_camera, self.lbl_feed):
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._badge(lbl, "#34495e")
            sl.addWidget(lbl)
        right.addWidget(sens)

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
        self.lbl_cans = QLabel()
        self.lbl_frames = QLabel()
        self.lbl_live = QLabel("Live: --")
        self.lbl_live.setStyleSheet("font-size: 15px; font-weight: bold;")
        self.lbl_verdict = QLabel("Last can: --")
        self.lbl_verdict.setStyleSheet("font-size: 15px; font-weight: bold;")
        for w in (self.lbl_cans, self.lbl_frames, self.lbl_live, self.lbl_verdict):
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
        self._update_light_buttons_enabled()

    @staticmethod
    def _badge(label, bg):
        label.setStyleSheet(f"background-color: {bg}; color: white; font-weight: bold; padding: 6px;")

    # -- worker lifecycle ----------------------------------------------------
    def _start_worker(self):
        self.worker = CameraInferenceWorker(self.model_path, self.conf_thresh, continuous=self.continuous)
        self.worker.object_present = self.object_present
        self.worker.frame_ready.connect(self.handle_new_frame)
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
        self.lbl_sensor.setText(f"SENSOR GPIO {SENSOR_PIN}: {state}" + ("  (CAN PRESENT)" if present else ""))
        self._badge(self.lbl_sensor, bg)
        if state == "UNKNOWN":
            self.log_message("WARNING: sensor read returned UNKNOWN.")

        if present == self.object_present:
            return
        self.object_present = present
        if self.worker:
            self.worker.object_present = present

        if self.continuous:
            return
        if present:
            self._start_part()
        else:
            self._finish_part()

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
            # Nothing in view any more -> drop YELLOW, back to GREEN
            self._set_defect_in_view(False)
            self.lbl_live.setText("Live: --")
            self.lbl_live.setStyleSheet("font-size: 15px; font-weight: bold;")
            self.video_label.clear()
            self.video_label.setText("Waiting for sensor trigger...")

    # -- per-can verdict (counters) ------------------------------------------
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

    # -- frames --------------------------------------------------------------
    def handle_new_frame(self, frame, metrics):
        self.total_frames += 1
        is_defect = metrics["is_defect"]
        has_det = bool(metrics["detections"])

        # Stack light follows what is in view right now, in both trigger modes
        if is_defect:
            self.clean_frames = 0
            self._set_defect_in_view(True)
        else:
            self.clean_frames += 1
            if self.clean_frames >= DEFECT_CLEAR_FRAMES:
                self._set_defect_in_view(False)

        # Per-can tally for the counters (sensor mode)
        if not self.continuous and self.part_active and metrics.get("counted"):
            self.part_frames += 1
            self.part_defect |= is_defect
            self.part_detected |= has_det

        if is_defect:
            self.lbl_live.setText("Live: ⚠️ DEFECT IN VIEW")
            self.lbl_live.setStyleSheet("color: #f0ad4e; font-size: 15px; font-weight: bold;")
        elif has_det:
            self.lbl_live.setText("Live: ✔️ GOOD CAN")
            self.lbl_live.setStyleSheet("color: #5cb85c; font-size: 15px; font-weight: bold;")
        else:
            self.lbl_live.setText("Live: no detection")
            self.lbl_live.setStyleSheet("color: #aaa; font-size: 15px; font-weight: bold;")

        self._refresh_counters()
        self._log_prediction(metrics)

        # Only render video while the live view is visible (saves CPU on the Pi)
        self._last_frame = frame
        if self.view_stack.currentIndex() == 0:
            self._show_frame(frame)

    # -- view switching / prediction log ---------------------------------------
    def set_view(self, index):
        self.view_stack.setCurrentIndex(index)
        (self.btn_view_live if index == 0 else self.btn_view_log).setChecked(True)
        if index == 0 and self._last_frame is not None and self.lbl_feed.text() == "FEED: LIVE":
            self._show_frame(self._last_frame)

    def _pred_line(self, html_text):
        self.pred_log.appendHtml(html_text)

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
        for name, conf, is_good in dets:
            c = "#5cb85c" if is_good else "#f0ad4e"
            parts.append(f'<span style="color:{c}">{html.escape(str(name))}</span> {conf:.2f}')
        det_txt = ", ".join(parts) if parts else '<span style="color:#777">no detections</span>'
        nb = lambda s: s.replace(" ", "&nbsp;")    # keep column alignment in HTML
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
        self.pred_log.clear()

    def save_prediction_log(self):
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
        # BGR -> QPixmap (copy() so the QImage owns its buffer)
        h, w, ch = frame.shape
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        qimg = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888).copy()
        self.video_label.setPixmap(QPixmap.fromImage(qimg).scaled(
            self.video_label.width(), self.video_label.height(),
            Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def _refresh_counters(self):
        rate = (100.0 * self.cans_defect / self.cans_total) if self.cans_total else 0.0
        self.lbl_cans.setText(
            f"Cans: {self.cans_total}   Good: {self.cans_good}   "
            f"Defect: {self.cans_defect}   None: {self.cans_none}   ({rate:.1f}% defect)")
        self.lbl_frames.setText(f"Frames processed: {self.total_frames}")

    def reset_counters(self):
        self.total_frames = self.cans_total = self.cans_good = self.cans_defect = self.cans_none = 0
        self.lbl_verdict.setText("Last can: --")
        self.lbl_verdict.setStyleSheet("font-size: 15px; font-weight: bold;")
        self._refresh_counters()
        self.log_message("Counters reset.")

    # -- stack light ---------------------------------------------------------
    def apply_auto_lights(self):
        """Single source of truth for the Auto stack light - recomputed from state every time."""
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
        event.accept()


def main():
    parser = argparse.ArgumentParser(description="Can inspection station: YOLO + sensor + stack light")
    parser.add_argument("--model", default=str(Path(__file__).resolve().parent.parent / "yolo" / "models" / "best.pt"), help="Path to trained YOLO .pt model")
    parser.add_argument("--conf", type=float, default=0.25, help="Detection confidence threshold")
    parser.add_argument("--continuous", action="store_true",
                        help="Start in continuous mode (ignore the sensor) for bench testing")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    window = MainWindow(args.model, args.conf, continuous=args.continuous)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()