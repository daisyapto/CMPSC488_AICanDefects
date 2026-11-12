"""
Can tracking with a camera detection zone ("Camera zone" capture mode).

Ported from Andrew Herman's can_inspection_station.py (PyQt6 / PySpin bench app). The matching
and zone logic is his; here it runs without Qt so the station's own camera, sensor, rejector and
web dashboard can use it.

    SENSOR (GPIO 25)          gives every can its Can ID: #1, #2, #3 ...
    CAMERA DETECTION ZONE     a narrow strip of the camera image acts as a second sensor and
                              confirms when that same can is under the camera
    INSPECTION                runs on the frame where the can was in the middle of the zone

How a can is followed
    1. Can passes the sensor          -> Can #N waits for the camera
    2. Can enters the camera zone     -> matched to its Can ID (cans can't overtake on a belt;
                                         once the travel time is known, the ID whose expected
                                         arrival is closest)
    3. Can leaves the camera zone     -> the frame from the middle of its time in the zone is
                                         inspected
       Never seen by the camera       -> MISSED (after the expected travel time)
       Camera saw a can with no ID    -> CAMERA ONLY (logged, nothing to reject)

The zone check runs on every camera frame in the camera thread and is cheap (a small, blurred
strip of the image), so a slow inspection never makes the zone miss the gap between two cans.
"""
from __future__ import annotations

import threading
from collections import deque

import cv2
import numpy as np

MAX_TRAVEL_S = 5.0          # a Can ID the camera hasn't seen within this time -> MISSED
MATCH_TOLERANCE_S = 0.15    # a sighting must be this close to a can's expected arrival
ZONE_OFF_RATIO = 0.6        # zone clears when the change drops below level * this
ZONE_MIN_FRAMES = 2         # frames in a row before a NEW can counts (clearing needs 1)
BACKGROUND_ADAPT = 0.02     # follow slow lighting drift while the zone is empty (0 = off)
MAX_ZONE_FRAMES = 90        # frames kept per can while it is in the zone
DEFAULT_ZONE = [0.47, 0.20, 0.53, 0.80]   # x0, y0, x1, y1 as fractions of the image


class CanMatcher:
    """Cans reach the camera in the same order they passed the sensor (FIFO)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._reset()

    def _reset(self):
        self.waiting = deque()               # [(can_id, sensor_time)] not yet seen by the camera
        self.travel = deque(maxlen=20)       # travel times of matched cans
        self.sensor_hist = deque(maxlen=200)
        self.camera_hist = deque(maxlen=30)
        self._tt = None                      # measured sensor -> camera travel time (s)
        self.sensor_count = self.camera_count = self.matched = self.missed = self.camera_only = 0

    def reset(self):
        with self._lock:
            self._reset()

    def typical_travel(self):
        return self._tt

    def _estimate_travel(self):
        """Travel time measured WITHOUT relying on the matching: compare every recent camera
        sighting with every recent sensor time; the true travel time is the gap that keeps
        repeating (wrong pairings scatter, because cans aren't evenly spaced). A can missed
        early on can therefore never poison the estimate."""
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
        if best_n < max(3, 0.6 * len(self.camera_hist)):  # most sightings must agree
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
        tt = self._tt
        return MAX_TRAVEL_S if tt is None else min(MAX_TRAVEL_S, tt * 1.5 + 0.5)

    def sensor_can(self, can_id: int, t: float):
        with self._lock:
            self.sensor_count += 1
            self.waiting.append((can_id, t))
            self.sensor_hist.append(t)

    def camera_can(self, t: float) -> dict:
        """Match a can arriving under the camera to its sensor Can ID. Older IDs skipped over
        were not seen by the camera -> missed, so one missed can never shifts the IDs of all
        the cans behind it."""
        with self._lock:
            self.camera_count += 1
            self.camera_hist.append(t)
            try:
                self._estimate_travel()
            except Exception:
                pass                                       # keep the last estimate
            if not self.waiting:
                self.camera_only += 1
                return {"id": None, "camera_t": t, "skipped": []}
            pick = 0
            if self._tt is not None:
                errs = [abs(t - (ts + self._tt)) for _, ts in self.waiting]
                pick = min(range(len(errs)), key=errs.__getitem__)
                if errs[pick] > self._tolerance():        # no can is due now: the sensor didn't count it
                    self.camera_only += 1
                    return {"id": None, "camera_t": t, "skipped": []}
            skipped = []
            for _ in range(pick):
                sid, sts = self.waiting.popleft()
                self.missed += 1
                skipped.append((sid, sts))
            can_id, ts = self.waiting.popleft()
            self.matched += 1
            self.travel.append(t - ts)
            return {"id": can_id, "sensor_t": ts, "camera_t": t, "travel": t - ts, "skipped": skipped}

    def check_timeouts(self, now: float) -> list:
        out = []
        with self._lock:
            limit = self.timeout()
            while self.waiting and now - self.waiting[0][1] > limit:
                can_id, ts = self.waiting.popleft()
                self.missed += 1
                out.append((can_id, ts))
        return out

    def drop_waiting(self) -> int:
        """Line stopped: forget cans still on their way (they are not inspected)."""
        with self._lock:
            n = len(self.waiting)
            self.waiting.clear()
            return n


class ZoneDetector:
    """'Can in zone' = the zone differs from the empty-belt background in EVERY thin slice
    across the belt. A gap narrower than the zone therefore still clears it."""

    def __init__(self, on_level: int = 25):
        self.background = None
        self.on_level = on_level
        self.present = False
        self._streak = 0
        self.score = 0.0
        self.capture_requested = True        # learn the background on the first frame

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
        """True / False when the zone state CHANGES, else None."""
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


def to_gray(frame):
    return frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


class CanTracker:
    """Sensor IDs + camera zone -> one frame per can. Thread-safe: the sensor thread calls
    on_sensor, the camera thread calls on_frame."""

    def __init__(self, zone=None, level: int = 25):
        self.matcher = CanMatcher()
        self.detector = ZoneDetector(level)
        self.zone = list(zone or DEFAULT_ZONE)
        self._lock = threading.Lock()
        self._cur = None                     # can in the zone: {"id", "sensor_t", "enter_t", "frames"}
        self.last_travel_ms = None

    # ---- settings
    def set_zone(self, zone):
        self.zone = list(zone)
        self.detector.capture_requested = True

    def set_level(self, level: int):
        self.detector.on_level = level

    def capture_background(self):
        self.detector.capture_requested = True

    def reset(self):
        self.matcher.reset()
        with self._lock:
            self._cur = None
        self.last_travel_ms = None

    # ---- events
    def on_sensor(self, can_id: int, t: float):
        self.matcher.sensor_can(can_id, t)

    def on_frame(self, frame, t: float, running: bool) -> list:
        """Zone check on one frame. Returns events:
            ("missed", can_id, sensor_t)       sensor counted it, camera never saw it
            ("camera_only", None, t)           camera saw a can with no Can ID
            ("seen", can_id, info)             can left the zone: info has the frame to inspect
        """
        change = self.detector.update(to_gray(frame), self.zone)
        events = []
        with self._lock:
            if change is True and running:
                ev = self.matcher.camera_can(t)
                events += [("missed", sid, sts) for sid, sts in ev["skipped"]]
                if ev["id"] is None:
                    events.append(("camera_only", None, t))
                else:
                    self.last_travel_ms = ev["travel"] * 1000
                    self._cur = {"id": ev["id"], "sensor_t": ev["sensor_t"], "enter_t": t, "frames": []}
            if self._cur is not None and self.detector.present:
                fr = self._cur["frames"]
                if len(fr) < MAX_ZONE_FRAMES:
                    fr.append((t, frame))
            if change is False and self._cur is not None:
                cur, self._cur = self._cur, None
                if cur["frames"]:
                    mid = (cur["enter_t"] + t) / 2          # can centred in the zone
                    ft, frm = min(cur["frames"], key=lambda f: abs(f[0] - mid))
                    events.append(("seen", cur["id"], {"sensor_t": cur["sensor_t"], "camera_t": ft,
                                                       "frame": frm, "frames": len(cur["frames"]),
                                                       "travel_ms": (ft - cur["sensor_t"]) * 1000}))
        if running:
            events += [("missed", sid, sts) for sid, sts in self.matcher.check_timeouts(t)]
        return events

    def check_timeouts(self, now: float) -> list:
        return [("missed", sid, sts) for sid, sts in self.matcher.check_timeouts(now)]

    def stats(self) -> dict:
        m = self.matcher
        tt = m.typical_travel()
        return {"present": self.detector.present, "score": round(self.detector.score, 1),
                "level": self.detector.on_level, "zone": self.zone,
                "sensor": m.sensor_count, "camera": m.camera_count, "matched": m.matched,
                "missed": m.missed, "camera_only": m.camera_only, "waiting": len(m.waiting),
                "travel_ms": round(tt * 1000) if tt is not None else None,
                "last_travel_ms": round(self.last_travel_ms) if self.last_travel_ms is not None else None}
