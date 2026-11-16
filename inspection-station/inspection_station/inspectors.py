"""
Inspectors decide PASS / FAIL for each can.

Rules for any inspector:
  * inspect(can_id, t_detect) runs on the inspection worker thread, one can at
    a time. t_detect is the sensor time (time.monotonic()).
  * It must return an Outcome.
  * If it raises, the can is REJECTED (fail-safe) and the error is logged.
  * It should finish well within the reject delay, or the can is past the air
    nozzle before the decision is made (it is then logged as MISSED).

The Camera inspector grabs the frame taken just after the can reached the
camera, runs the image processing in vision.py, and saves the image.
To use a trained model instead, replace the vision.analyze(...) call in
CameraInspector.inspect() with your model - see the comment there.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass

DEFECT_TYPES = ["Dent", "Scratch", "Flange damage", "Contamination",
                "Crease / wrinkle", "Coating void"]


@dataclass
class Outcome:
    passed: bool
    defect: str = ""            # empty when passed
    confidence: float = 1.0     # 0..1
    image: str = ""             # saved image path, if any


class Inspector:
    name = "Base"
    description = ""

    def inspect(self, can_id: int, t_detect: float, frame=None) -> Outcome:
        raise NotImplementedError


class SimulatedInspector(Inspector):
    name = "Simulated"
    description = "Random results at the set rate"

    def __init__(self, defect_rate: float = 0.10):
        self.defect_rate = defect_rate

    def inspect(self, can_id, t_detect, frame=None):
        time.sleep(random.uniform(0.010, 0.040))       # fake capture + inference time
        if random.random() < self.defect_rate:
            return Outcome(False, random.choice(DEFECT_TYPES), random.uniform(0.75, 0.99))
        return Outcome(True, "", random.uniform(0.90, 0.999))


class AlwaysPassInspector(Inspector):
    name = "Always pass"
    description = "Sensor / counting test"

    def inspect(self, can_id, t_detect, frame=None):
        return Outcome(True, "", 1.0)


class AlwaysRejectInspector(Inspector):
    name = "Always reject"
    description = "Air timing test - blows every can"

    def inspect(self, can_id, t_detect, frame=None):
        return Outcome(False, "Forced reject", 1.0)


class CameraInspector(Inspector):
    name = "Camera"
    description = "Camera + image processing"

    def __init__(self, manager, settings, import_error: str = ""):
        self.m = manager                # camera.CameraManager or None
        self.s = settings
        self.import_error = import_error

    def inspect(self, can_id, t_detect, frame=None):
        m, s = self.m, self.s
        if m is None:
            raise RuntimeError(f"camera support not installed ({self.import_error})")
        if not m.ok:
            raise RuntimeError(f"camera not ready - {m.status}")
        from .vision import annotate

        if frame is None:                          # fixed-delay capture; camera-zone mode passes the frame
            # wait at least 3 frame times: at a low frame rate (standby, heat) frames are far apart
            wait = max(0.5, 3.0 / max(1, m.fps_limit or 1))
            frame, _ = m.wait_frame_after(t_detect + s.camera_delay_ms / 1000, timeout=wait)

        # ---- decision. To use a trained model, replace this line with code that
        # ---- returns a vision.VisionResult (or build an Outcome directly).
        result = m.vision.analyze(frame, m.params)

        annotated = annotate(result)
        path = None
        if s.save_images == "all" or (s.save_images == "fails" and not result.passed):
            path = m.store.plan(can_id, result.passed, result.label)
            m.store.save(path, frame, annotated if s.save_annotated else None)
        m.publish(can_id, result, annotated, path)
        return Outcome(result.passed, result.label, result.confidence, str(path) if path else "")

    def inspect_frames(self, can_id, t_detect, frames, min_fail: int):
        """Frames per can (the team's bench-app vote): inspect several frames of the same can and
        FAIL it only if at least min_fail of them show a defect. One frame is saved and shown:
        the most confident defect frame, or the middle frame when the can passes."""
        m, s = self.m, self.s
        if m is None:
            raise RuntimeError(f"camera support not installed ({self.import_error})")
        from .vision import annotate

        results = [(m.vision.analyze(f, m.params), f) for f in frames]
        bad = [(r, f) for r, f in results if not r.passed]
        passed = len(bad) < max(1, min(min_fail, len(results)))
        if passed:
            good = [rf for rf in results if rf[0].passed]
            result, frame = good[len(good) // 2]
            label = ""
            if bad:
                label = f"{len(bad)}/{len(results)} frames showed a defect"
        else:
            result, frame = max(bad, key=lambda rf: rf[0].confidence)
            label = f"{result.label} ({len(bad)}/{len(results)} frames)"
        annotated = annotate(result)
        path = None
        if s.save_images == "all" or (s.save_images == "fails" and not passed):
            path = m.store.plan(can_id, passed, result.label if not passed else "")
            m.store.save(path, frame, annotated if s.save_annotated else None)
        m.publish(can_id, result, annotated, path)
        return Outcome(passed, label if not passed else "", result.confidence, str(path) if path else "")


def build_inspectors(camera_manager, settings, import_error: str = "") -> dict[str, Inspector]:
    items = [SimulatedInspector(), AlwaysPassInspector(), AlwaysRejectInspector(),
             CameraInspector(camera_manager, settings, import_error)]
    return {i.name: i for i in items}
