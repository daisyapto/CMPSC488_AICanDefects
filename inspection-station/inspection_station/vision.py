"""
Image processing for can inspection (OpenCV + NumPy).

Pipeline for every frame
  1. Resize to WORK_WIDTH, convert to grayscale, blur.
  2. Find the can (Hough circle) and build the region of interest (ROI).
  3. Build a difference image - how much each pixel deviates from "normal":
       * reference mode : compared to a taught GOOD can (aligned by the circle).
                          Best for real cans with rings / embossing.
       * local mode     : compared to its own neighbourhood (median filter).
                          Works without teaching; good for smooth surfaces.
  4. Threshold by sensitivity, clean up, keep blobs >= min area.
  5. Classify each blob: Scratch (long, thin), Contamination (small, dark), Dent.

This is a classic machine-vision baseline ("Built-in" engine).

TEAM ENGINE: the "Team" detection engine runs detect(image) from team_vision.py
in the project folder instead, so the team's own OpenCV code drives PASS / FAIL,
the boxes in the live view, saved images and the reject. The file is reloaded
automatically when it changes. See team_vision.py for the contract.
"""
from __future__ import annotations

import importlib.util
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .config import APP_DIR

TEAM_FILE = APP_DIR / "team_vision.py"
WORK_WIDTH = 640          # processing resolution (min area is measured at this size)

# BGR colors for overlays
C_ROI = (238, 211, 34)    # cyan
C_FAIL = (68, 68, 239)    # red
C_PASS = (94, 197, 34)    # green
C_TEXT = (243, 237, 230)


@dataclass
class VisionParams:
    sensitivity: int = 50        # 1..100, higher finds fainter defects
    min_area: int = 40           # px at WORK_WIDTH
    roi_scale: float = 0.88      # inspect this fraction of the can radius (skips the rim edge)
    engine: str = "builtin"      # "builtin" | "team" (team_vision.py) | "yolo" | "hailo" (AI HAT)
    yolo_model: str = ""
    yolo_conf: float = 0.5
    yolo_imgsz: int = 640
    yolo_crop: bool = True
    hailo_model: str = ""
    area: tuple = (0.0, 0.0, 1.0, 1.0)   # inspection area (x0, y0, x1, y1) as fractions of the image

    @property
    def threshold(self) -> int:
        return int(max(5, 65 - 0.6 * self.sensitivity))


@dataclass
class Defect:
    label: str
    x: int
    y: int
    w: int
    h: int
    area: int
    strength: float              # mean deviation (gray levels)


@dataclass
class VisionResult:
    passed: bool
    defects: list
    circle: tuple | None         # (x, y, r) of the can at WORK_WIDTH, or None
    confidence: float
    ms: float
    method: str                  # "reference" | "local" | "team"
    work: np.ndarray = field(repr=False)     # BGR image at WORK_WIDTH
    blur: np.ndarray = field(repr=False)
    diff: np.ndarray = field(repr=False)
    mask: np.ndarray = field(repr=False)
    circles: list = field(default_factory=list)      # [(x, y, r, label)] at WORK_WIDTH (team engine)
    measures: dict = field(default_factory=dict)     # numbers to show, e.g. {"Diameter": "512 px"}
    area: tuple | None = None                        # inspection area (x0, y0, x1, y1) at WORK_WIDTH

    @property
    def label(self) -> str:
        return self.defects[0].label if self.defects else ""

    def summary(self) -> str:
        if self.passed:
            return "No defects"
        counts = {}
        for d in self.defects:
            counts[d.label] = counts.get(d.label, 0) + 1
        return ", ".join(f"{n} {k.lower()}" for k, n in counts.items())


def to_work(frame: np.ndarray) -> np.ndarray:
    """Any camera frame (gray / BGR / BGRA, any size) -> BGR at WORK_WIDTH."""
    if frame.ndim == 2:
        bgr = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    elif frame.shape[2] == 4:
        bgr = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
    else:
        bgr = frame
    h, w = bgr.shape[:2]
    if w != WORK_WIDTH:
        s = WORK_WIDTH / w
        bgr = cv2.resize(bgr, (WORK_WIDTH, max(1, round(h * s))),
                         interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
    return bgr


def _blur(work: np.ndarray) -> np.ndarray:
    return cv2.GaussianBlur(cv2.cvtColor(work, cv2.COLOR_BGR2GRAY), (5, 5), 0)


class Vision:
    def __init__(self):
        self.reference = None            # (blurred gray, circle) of a good can
        self.reference_time = None
        self.team = TeamDetector()
        self.yolo = YoloDetector()
        from .hailo import HailoDetector
        self.hailo = HailoDetector()

    # ------------------------------------------------------------ can location
    @staticmethod
    def find_can(blur: np.ndarray):
        h, w = blur.shape
        m = min(h, w)
        circles = cv2.HoughCircles(blur, cv2.HOUGH_GRADIENT, dp=1.5, minDist=m,
                                   param1=120, param2=40,
                                   minRadius=int(m * 0.2), maxRadius=int(m * 0.49))
        if circles is None:
            return None
        x, y, r = circles[0][0]
        return float(x), float(y), float(r)

    # -------------------------------------------------------- golden reference
    def teach(self, frame: np.ndarray) -> bool:
        """Store a GOOD can as the reference. Returns True if the can was located."""
        blur = _blur(to_work(frame))
        circle = self.find_can(blur)
        self.reference = (blur, circle)
        self.reference_time = time.time()
        return circle is not None

    def clear_reference(self):
        self.reference = None
        self.reference_time = None

    def _expected(self, blur, circle):
        ref = self.reference
        if ref is not None and ref[0].shape == blur.shape:
            ref_img, ref_circle = ref
            if circle is not None and ref_circle is not None:
                s = circle[2] / ref_circle[2]
                m = np.float32([[s, 0, circle[0] - s * ref_circle[0]],
                                [0, s, circle[1] - s * ref_circle[1]]])
                ref_img = cv2.warpAffine(ref_img, m, (blur.shape[1], blur.shape[0]),
                                         borderMode=cv2.BORDER_REPLICATE)
            return ref_img, "reference"
        k = max(21, (blur.shape[1] // 16) | 1)
        return cv2.medianBlur(blur, k), "local"

    # ---------------------------------------------------------------- analyse
    def analyze(self, frame: np.ndarray, p: VisionParams, live: bool = False) -> VisionResult:
        """Inspect only inside the inspection area: crop, run the engine, map the result back."""
        box = area_pixels(p.area, frame.shape)
        if box is None:
            return self._analyze(frame, p, live)
        x0, y0, x1, y1 = box
        res = self._analyze(frame[y0:y1, x0:x1], p, live)
        return embed(res, frame, box)

    def _analyze(self, frame: np.ndarray, p: VisionParams, live: bool = False) -> VisionResult:
        if p.engine == "team":
            return self.team.analyze(frame, live=live)
        if p.engine == "hailo":
            return self.hailo.analyze(frame, p, live=live)
        if p.engine == "yolo":
            from .edition import YOLO_AVAILABLE
            if not YOLO_AVAILABLE:
                raise RuntimeError("YOLO is not included in the lite edition")
            return self.yolo.analyze(frame, p, live=live)
        t0 = time.perf_counter()
        work = to_work(frame)
        blur = _blur(work)
        h, w = blur.shape
        circle = self.find_can(blur)

        roi = np.zeros_like(blur)
        if circle is not None:
            cv2.circle(roi, (int(circle[0]), int(circle[1])), int(circle[2] * p.roi_scale), 255, -1)
        else:
            mh, mw = int(h * 0.05), int(w * 0.05)
            roi[mh:h - mh, mw:w - mw] = 255

        expected, method = self._expected(blur, circle)
        signed = blur.astype(np.int16) - expected.astype(np.int16)
        diff = np.clip(np.abs(signed), 0, 255).astype(np.uint8)
        _, mask = cv2.threshold(diff, p.threshold, 255, cv2.THRESH_BINARY)
        mask = cv2.bitwise_and(mask, roi)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        defects = []
        blob = np.zeros_like(blur)
        for c in contours:
            area = cv2.contourArea(c)
            if area < p.min_area:
                continue
            x, y, bw, bh = cv2.boundingRect(c)
            (_, _), (rw, rh), _ = cv2.minAreaRect(c)
            elongation = max(rw, rh) / max(1.0, min(rw, rh))
            blob[:] = 0
            cv2.drawContours(blob, [c], -1, 255, -1)
            darker = cv2.mean(signed, mask=blob)[0] < 0
            strength = cv2.mean(diff, mask=blob)[0]
            if elongation >= 3.5:
                label = "Scratch"
            elif area < p.min_area * 4 and darker:
                label = "Contamination"
            else:
                label = "Dent"
            defects.append(Defect(label, x, y, bw, bh, int(area), float(strength)))
        defects.sort(key=lambda d: d.area, reverse=True)

        thr = p.threshold
        if defects:
            confidence = min(0.99, 0.5 + max(d.strength for d in defects) / (thr * 4))
        else:
            peak = cv2.minMaxLoc(diff, mask=roi)[1] if roi.any() else 0
            confidence = float(np.clip(1 - peak / (thr * 2), 0.5, 0.99))

        return VisionResult(not defects, defects, circle, confidence,
                            (time.perf_counter() - t0) * 1000, method,
                            work, blur, diff, mask)


# =============================================================================
# Inspection area
# =============================================================================
def area_pixels(area, shape):
    """Fractions -> pixel box (x0, y0, x1, y1), or None for the whole image."""
    try:
        fx0, fy0, fx1, fy1 = (float(v) for v in area)
    except (TypeError, ValueError):
        return None
    if fx0 <= 0.001 and fy0 <= 0.001 and fx1 >= 0.999 and fy1 >= 0.999:
        return None
    h, w = shape[:2]
    x0, x1 = int(max(0, min(fx0, fx1)) * w), int(min(1, max(fx0, fx1)) * w)
    y0, y1 = int(max(0, min(fy0, fy1)) * h), int(min(1, max(fy0, fy1)) * h)
    if x1 - x0 < 32 or y1 - y0 < 32:
        return None
    return x0, y0, x1, y1


def embed(res: VisionResult, frame: np.ndarray, box) -> VisionResult:
    """A result computed on the area crop -> the same result on the full image."""
    x0, y0, x1, y1 = box
    work = to_work(frame)
    sf = work.shape[1] / frame.shape[1]                    # full image -> full work
    sc = res.work.shape[1] / (x1 - x0)                     # crop -> crop work

    def X(v):
        return int(round((v / sc + x0) * sf))

    def Y(v):
        return int(round((v / sc + y0) * sf))

    k = sf / sc
    defects = [Defect(d.label, X(d.x), Y(d.y), int(round(d.w * k)), int(round(d.h * k)),
                      int(d.area * k * k), d.strength) for d in res.defects]
    circle = None if res.circle is None else (res.circle[0] / sc * sf + x0 * sf,
                                               res.circle[1] / sc * sf + y0 * sf, res.circle[2] * k)
    circles = [(c[0] / sc * sf + x0 * sf, c[1] / sc * sf + y0 * sf, c[2] * k, c[3]) for c in res.circles]
    ax0, ay0, ax1, ay1 = int(x0 * sf), int(y0 * sf), int(x1 * sf), int(y1 * sf)
    blur = _blur(work)

    def paste(a):
        full = np.zeros(blur.shape, a.dtype)
        if ax1 > ax0 and ay1 > ay0:
            full[ay0:ay1, ax0:ax1] = cv2.resize(a, (ax1 - ax0, ay1 - ay0), interpolation=cv2.INTER_NEAREST)
        return full

    return VisionResult(res.passed, defects, circle, res.confidence, res.ms, res.method, work, blur,
                        paste(res.diff), paste(res.mask), circles, res.measures, (ax0, ay0, ax1, ay1))


def draw_area(img: np.ndarray, area_px) -> np.ndarray:
    """Dashed outline of the inspection area + dim everything outside it."""
    if area_px is None:
        return img
    x0, y0, x1, y1 = area_px
    dim = img.copy()
    dim[:] = (dim * 0.45).astype(img.dtype)
    dim[y0:y1, x0:x1] = img[y0:y1, x0:x1]
    for i in range(x0, x1, 14):
        cv2.line(dim, (i, y0), (min(i + 7, x1), y0), C_ROI, 2)
        cv2.line(dim, (i, y1 - 1), (min(i + 7, x1), y1 - 1), C_ROI, 2)
    for i in range(y0, y1, 14):
        cv2.line(dim, (x0, i), (x0, min(i + 7, y1)), C_ROI, 2)
        cv2.line(dim, (x1 - 1, i), (x1 - 1, min(i + 7, y1)), C_ROI, 2)
    return dim


# =============================================================================
# Team engine - runs detect(image) from team_vision.py
# =============================================================================
class TeamDetector:
    """Loads team_vision.py (reloading it when the file changes) and turns what
    its detect() returns into a VisionResult. Any error is raised, so a can
    inspected while the team code is broken is rejected (fail-safe)."""

    def __init__(self, path=TEAM_FILE):
        self.path = path
        self.error = ""
        self._mod = None
        self._mtime = None
        self._lock = threading.Lock()

    def _module(self):
        with self._lock:
            try:
                mtime = self.path.stat().st_mtime
            except OSError:
                self.error = f"{self.path.name} not found in the project folder"
                raise RuntimeError(self.error)
            if mtime != self._mtime:
                try:
                    spec = importlib.util.spec_from_file_location("team_vision", self.path)
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    if not callable(getattr(mod, "detect", None)):
                        raise RuntimeError("no detect(image) function")
                except Exception as e:
                    self._mod, self._mtime = None, mtime      # don't retry until the file changes
                    self.error = f"{self.path.name}: {e}"
                    raise RuntimeError(self.error) from e
                self._mod, self._mtime, self.error = mod, mtime, ""
            if self._mod is None:
                raise RuntimeError(self.error)
            return self._mod

    def analyze(self, frame: np.ndarray, live: bool = False) -> VisionResult:
        # inspections always get the detector; the live view only when it is free
        lock = self.__dict__.setdefault("_run_lock", threading.Lock())
        if not lock.acquire(blocking=not live):
            raise Busy()
        try:
            return self._analyze_team(frame)
        finally:
            lock.release()

    def _analyze_team(self, frame: np.ndarray) -> VisionResult:
        mod = self._module()
        if frame.ndim == 2:
            image = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        elif frame.shape[2] == 4:
            image = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
        else:
            image = frame
        t0 = time.perf_counter()
        try:
            out = mod.detect(image.copy())
        except Exception as e:
            self.error = f"detect() failed: {type(e).__name__}: {e}"
            raise RuntimeError(self.error) from e
        ms = (time.perf_counter() - t0) * 1000
        self.error = ""

        work = to_work(image)
        scale = work.shape[1] / image.shape[1]
        circles, measures = [], {}
        if isinstance(out, dict):                         # {"defects": [...], "circles": [...], "measurements": {...}}
            for c in out.get("circles") or []:
                try:
                    circles.append((float(c["x"]) * scale, float(c["y"]) * scale, float(c["r"]) * scale,
                                    str(c.get("label", ""))))
                except (KeyError, TypeError, ValueError):
                    pass
            measures = {str(k): str(v) for k, v in (out.get("measurements") or {}).items()}
            out = out.get("defects") or []
        defects, confs = _parse_team(out, scale, work.shape)
        blur = _blur(work)
        mask = np.zeros_like(blur)
        for d in defects:
            if d.w and d.h:
                cv2.rectangle(mask, (d.x, d.y), (d.x + d.w, d.y + d.h), 255, -1)
        conf = max(confs) if confs else getattr(mod, "PASS_CONFIDENCE", 0.95)
        return VisionResult(not defects, defects, None, float(conf), ms, "team",
                            work, blur, np.zeros_like(blur), mask, circles, measures)


def _parse_team(out, scale, shape):
    """detect() may return:
         True / False                       good / defective (no box)
         [] or None                         good can
         [{"label", "box": (x, y, w, h), "confidence"}, ...]
         [("Dent", x, y, w, h[, confidence]), ...]
    Boxes are in pixels of the image that was passed in."""
    if out is None or out is True:
        return [], []
    if out is False:
        return [Defect("Defect", 0, 0, 0, 0, 0, 0.0)], [0.9]
    if not isinstance(out, (list, tuple)):
        raise RuntimeError(f"detect() returned {type(out).__name__}; expected a list or True/False")
    h, w = shape[:2]
    defects, confs = [], []
    for item in out:
        if isinstance(item, dict):
            label = str(item.get("label", "Defect"))
            box = item.get("box") or (0, 0, 0, 0)
            conf = item.get("confidence", 0.9)
        elif isinstance(item, (list, tuple)) and len(item) >= 5:
            label, box, conf = str(item[0]), item[1:5], (item[5] if len(item) > 5 else 0.9)
        else:
            raise RuntimeError(f"detect() returned an item it can't read: {item!r}")
        x, y, bw, bh = (float(v) * scale for v in box)
        x, y = int(np.clip(x, 0, w - 1)), int(np.clip(y, 0, h - 1))
        bw, bh = int(min(bw, w - x)), int(min(bh, h - y))
        defects.append(Defect(label, x, y, bw, bh, bw * bh, 0.0))
        confs.append(float(np.clip(conf, 0.0, 1.0)))
    order = sorted(range(len(defects)), key=lambda i: defects[i].area, reverse=True)
    return [defects[i] for i in order], [confs[i] for i in order]


# =============================================================================
# YOLO engine - a trained Ultralytics model (detection or classification)
# =============================================================================
class Busy(Exception):
    """The model is in use by an inspection; the live view skips this frame."""


GOOD_NAMES = {"good", "ok", "pass", "no_defect", "normal"}


class YoloDetector:
    """Runs an Ultralytics YOLO model (pip install ultralytics).

    * Detection models (yolo11n.pt / yolo26n.pt trained on boxes): each box = a defect.
    * Classification models (*-cls): the top class decides; a class named
      good / ok / pass / normal means PASS.
    * Formats: .pt, .onnx, or an NCNN export folder (*_ncnn_model) - NCNN is the
      fastest on a Raspberry Pi.
    * Inspections always get the model; the live view only uses it when it is
      free, so the live view can never delay a reject.
    """

    def __init__(self):
        self.error = ""
        self.info = ""
        self.ms = None
        self._model = None
        self._key = None
        self._lock = threading.Lock()

    def _load(self, p: VisionParams):
        path = Path(p.yolo_model).expanduser()
        if not path.is_absolute():
            path = APP_DIR / path
        if not path.exists():
            self.error = f"model not found: {path}"
            raise RuntimeError(self.error)
        key = (str(path), path.stat().st_mtime, p.yolo_imgsz)
        if key == self._key and self._model is not None:
            return self._model
        if key == self._key:                      # failed before and nothing changed
            raise RuntimeError(self.error)
        self._key, self._model = key, None
        try:
            from ultralytics import YOLO
        except Exception as e:
            self.error = f"ultralytics not installed ({e}) - pip install ultralytics"
            raise RuntimeError(self.error) from e
        try:
            model = YOLO(str(path), task=None)
            dummy = np.zeros((p.yolo_imgsz, p.yolo_imgsz, 3), np.uint8)
            model.predict(dummy, imgsz=p.yolo_imgsz, verbose=False)    # warm-up: 1st run is slow
        except Exception as e:
            self.error = f"could not load {path.name}: {e}"
            raise RuntimeError(self.error) from e
        names = getattr(model, "names", {}) or {}
        self._model, self.error = model, ""
        self.info = f"{path.name} · {getattr(model, 'task', '?')} · {len(names)} classes: " + \
                    ", ".join(str(n) for n in list(names.values())[:8])
        return model

    def preload(self, p: VisionParams):
        """Load + warm up the model now, so the first can isn't late (a cold model
        can take seconds - that can would pass the air nozzle before the decision)."""
        with self._lock:
            try:
                self._load(p)
            except RuntimeError:
                pass                                  # error is shown in the dashboard

    def analyze(self, frame: np.ndarray, p: VisionParams, live: bool = False) -> VisionResult:
        if not self._lock.acquire(blocking=not live):
            raise Busy()
        try:
            model = self._load(p)
            return self._run(model, frame, p)
        finally:
            self._lock.release()

    def _run(self, model, frame, p: VisionParams) -> VisionResult:
        if frame.ndim == 2:
            image = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        elif frame.shape[2] == 4:
            image = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
        else:
            image = frame
        work = to_work(image)
        blur = _blur(work)
        scale = work.shape[1] / image.shape[1]

        # Crop to the can so YOLO's resize to imgsz does not shrink small defects away.
        circle, ox, oy, crop = None, 0, 0, image
        if p.yolo_crop:
            circle = Vision.find_can(blur)
            if circle is not None:
                cx, cy, r = (v / scale for v in circle)
                half = int(r * 1.08)
                x0, y0 = max(0, int(cx - half)), max(0, int(cy - half))
                x1, y1 = min(image.shape[1], int(cx + half)), min(image.shape[0], int(cy + half))
                if x1 - x0 > 32 and y1 - y0 > 32:
                    ox, oy, crop = x0, y0, image[y0:y1, x0:x1]

        t0 = time.perf_counter()
        try:
            r = model.predict(crop, imgsz=p.yolo_imgsz, conf=p.yolo_conf, verbose=False)[0]
        except Exception as e:
            self.error = f"inference failed: {e}"
            raise RuntimeError(self.error) from e
        ms = (time.perf_counter() - t0) * 1000
        self.ms, self.error = ms, ""
        names = r.names or {}

        defects, confs = [], []
        if getattr(r, "probs", None) is not None:                 # classification model
            top = int(r.probs.top1)
            conf = float(r.probs.top1conf)
            label = str(names.get(top, top))
            if label.lower() not in GOOD_NAMES and conf >= p.yolo_conf:
                defects.append(Defect(label.replace("_", " ").capitalize(), 0, 0, 0, 0, 0, conf))
                confs.append(conf)
            pass_conf = conf if not defects else None
        else:                                                     # detection model
            boxes = r.boxes
            xyxy = boxes.xyxy.cpu().numpy() if boxes is not None and len(boxes) else np.zeros((0, 4))
            cls = boxes.cls.cpu().numpy().astype(int) if len(xyxy) else []
            cf = boxes.conf.cpu().numpy() if len(xyxy) else []
            h, w = work.shape[:2]
            for (x1, y1, x2, y2), c, k in zip(xyxy, cf, cls):
                label = str(names.get(int(k), k))
                if label.lower() in GOOD_NAMES:
                    continue
                x, y = int(np.clip((x1 + ox) * scale, 0, w - 1)), int(np.clip((y1 + oy) * scale, 0, h - 1))
                bw, bh = int((x2 - x1) * scale), int((y2 - y1) * scale)
                defects.append(Defect(label.replace("_", " ").capitalize(), x, y, bw, bh, bw * bh, float(c)))
                confs.append(float(c))
            pass_conf = None
        defects.sort(key=lambda d: d.strength, reverse=True)
        conf = max(confs) if confs else (pass_conf or 0.95)

        mask = np.zeros_like(blur)
        for d in defects:
            if d.w and d.h:
                cv2.rectangle(mask, (d.x, d.y), (d.x + d.w, d.y + d.h), 255, -1)
        return VisionResult(not defects, defects, circle, float(conf), ms, "yolo",
                            work, blur, np.zeros_like(blur), mask)


# =============================================================================
# Drawing
# =============================================================================
def _label(img, text, x, y, color, scale=0.5):
    (tw, th), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
    y = max(th + 6, y)
    cv2.rectangle(img, (x, y - th - 6), (x + tw + 8, y + base - 2), color, -1)
    cv2.putText(img, text, (x + 4, y - 4), cv2.FONT_HERSHEY_SIMPLEX, scale, (20, 15, 11), 1, cv2.LINE_AA)


def annotate(res: VisionResult, header: bool = True) -> np.ndarray:
    """Working image with the inspection area, circles, defect boxes and a PASS / FAIL header."""
    img = draw_area(res.work.copy(), res.area)
    ring_colors = {"outer": (238, 211, 34), "flange inner": (246, 130, 59), "bottom": (250, 139, 167)}
    for x, y, r, lab in res.circles:
        cv2.circle(img, (int(x), int(y)), max(1, int(r)), ring_colors.get(lab, C_ROI), 1, cv2.LINE_AA)
    if res.circle is not None:
        x, y, r = res.circle
        cv2.circle(img, (int(x), int(y)), int(r), C_ROI, 1, cv2.LINE_AA)
    for d in res.defects:
        if not (d.w and d.h):
            continue
        cv2.rectangle(img, (d.x - 3, d.y - 3), (d.x + d.w + 3, d.y + d.h + 3), C_FAIL, 2, cv2.LINE_AA)
        _label(img, d.label, d.x - 3, d.y - 6, C_FAIL, 0.45)
    if header:
        color = C_PASS if res.passed else C_FAIL
        text = "PASS" if res.passed else f"FAIL - {res.summary()}"
        _label(img, text, 8, 26, color, 0.6)
        info = f"{res.method}  {res.ms:.0f} ms" + ("" if res.circle is not None or res.method in ("team", "yolo") else "  (can not found)")
        cv2.putText(img, info, (10, img.shape[0] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.42,
                    C_TEXT, 1, cv2.LINE_AA)
    return img


def exposure_stats(frame: np.ndarray) -> dict:
    """Brightness of a frame: mean (0-255), % blown-out and % black pixels."""
    g = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(g, (max(1, g.shape[1] // 4), max(1, g.shape[0] // 4)), interpolation=cv2.INTER_AREA)
    n = small.size
    return {"mean": float(small.mean()), "clipped": float((small >= 250).sum() * 100 / n),
            "black": float((small <= 5).sum() * 100 / n)}


_focus = {"hist": []}          # (time, sharpness) for the "best so far" marker


def focus_view(frame: np.ndarray) -> np.ndarray:
    """Setup helper: centre of the image at FULL resolution (no downscaling) with a sharpness
    score to maximise while turning the lens focus ring, plus a brightness check."""
    bgr = frame if frame.ndim == 3 else cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    h, w = bgr.shape[:2]
    cw, ch = min(w, 960), min(h, 720)
    x0, y0 = (w - cw) // 2, (h - ch) // 2
    crop = bgr[y0:y0 + ch, x0:x0 + cw].copy()
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    sharp = float(cv2.Laplacian(g, cv2.CV_64F).var())
    now = time.monotonic()
    hist = [(t, v) for t, v in _focus["hist"] if now - t < 20] + [(now, sharp)]
    _focus["hist"] = hist
    best = max(v for _, v in hist)
    ex = exposure_stats(frame)

    # panel
    pw = min(cw - 20, 640)
    cv2.rectangle(crop, (10, 10), (10 + pw, 160), (20, 15, 11), -1)
    cv2.putText(crop, "FOCUS - turn the lens ring slowly, make the bar longest", (22, 38),
                cv2.FONT_HERSHEY_SIMPLEX, 0.62, C_TEXT, 1, cv2.LINE_AA)
    frac = 0 if best <= 0 else min(1.0, sharp / best)
    cv2.rectangle(crop, (22, 52), (22 + pw - 24, 78), (60, 50, 40), -1)
    cv2.rectangle(crop, (22, 52), (22 + int((pw - 24) * frac), 78),
                  C_PASS if frac > 0.95 else (36, 165, 245) if frac > 0.75 else C_FAIL, -1)
    cv2.putText(crop, f"sharpness {sharp:.0f}   best in last 20 s {best:.0f}", (22, 106),
                cv2.FONT_HERSHEY_SIMPLEX, 0.62, C_TEXT, 1, cv2.LINE_AA)
    m = ex["mean"]
    verdict = ("TOO DARK - longer exposure / more light / open the aperture" if m < 60 else
               "OVEREXPOSED - shorter exposure / less light" if ex["clipped"] > 3 or m > 200 else "brightness OK")
    color = C_PASS if verdict == "brightness OK" else (36, 165, 245)
    cv2.putText(crop, f"brightness {m:.0f}/255  ({ex['clipped']:.1f}% white)  {verdict}", (22, 140),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1, cv2.LINE_AA)
    cv2.putText(crop, f"centre {cw}x{ch} of {w}x{h} at full resolution", (14, ch - 12),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, C_TEXT, 1, cv2.LINE_AA)
    return crop


def _area_work(p, frame):
    box = area_pixels(p.area, frame.shape)
    if box is None:
        return None
    sf = WORK_WIDTH / frame.shape[1]
    return tuple(int(v * sf) for v in box)


def render(vision: Vision, frame: np.ndarray, view: str, p: VisionParams):
    """Image for the live view. Returns (BGR image, VisionResult or None)."""
    if view == "raw":
        return draw_area(to_work(frame), _area_work(p, frame)), None
    if view == "focus":
        return focus_view(frame), None
    res = vision.analyze(frame, p, live=True)
    if view == "mask":
        heat = cv2.applyColorMap(cv2.convertScaleAbs(res.diff, alpha=3), cv2.COLORMAP_INFERNO)
        heat = draw_area(heat, res.area)
        contours, _ = cv2.findContours(res.mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(heat, contours, -1, C_ROI, 1, cv2.LINE_AA)
        return heat, res
    if view == "edges":
        edges = cv2.Canny(res.blur, 60, 150)
        out = np.zeros_like(res.work)
        out[edges > 0] = C_ROI
        return out, res
    return annotate(res), res
