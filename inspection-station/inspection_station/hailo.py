"""
AI HAT+ (Hailo-8, 26 TOPS) detection engine.

Runs a YOLO detection model compiled for Hailo (.hef) on the AI HAT instead of
the Pi's CPU - the CPU stays free for the camera, the dashboard and the motors.

What it needs
  * HailoRT + its Python package (hailo_platform) and the PCIe driver.
    Raspberry Pi OS: sudo apt install hailo-all.  Ubuntu: HailoRT .deb files from
    the Hailo developer zone. Check: hailortcli fw-control identify
  * models/can_defects.hef - your trained YOLO model compiled with the Hailo
    Dataflow Compiler / Model Zoo WITH the NMS post-process (the default for YOLOv8).
  * models/can_defects.txt - class names, one per line, in training order
    (optional; a class named good / ok / pass is treated as a pass).

Uses the same settings as the YOLO engine: min confidence and "crop to the can".
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import cv2
import numpy as np

from .config import APP_DIR


class HailoDetector:
    def __init__(self):
        self.error = ""
        self.info = ""
        self.ms = None
        self._key = None
        self._lock = threading.Lock()
        self._ctx = None              # (vdevice, pipeline, activation, input name, (h, w), labels)

    # ------------------------------------------------------------------ load
    def _path(self, p) -> Path:
        path = Path(p.hailo_model).expanduser()
        return path if path.is_absolute() else APP_DIR / path

    def _close(self):
        ctx, self._ctx = self._ctx, None
        if not ctx:
            return
        vdev, pipeline, activation = ctx[0], ctx[1], ctx[2]
        for c in (activation, pipeline):
            try:
                c.__exit__(None, None, None)
            except Exception:
                pass
        try:
            vdev.release()
        except Exception:
            pass

    def _load(self, p):
        path = self._path(p)
        if not path.exists():
            self.error = f"model not found: {path} (compile your YOLO model to .hef for the AI HAT)"
            raise RuntimeError(self.error)
        key = (str(path), path.stat().st_mtime)
        if key == self._key and self._ctx is not None:
            return self._ctx
        if key == self._key:
            raise RuntimeError(self.error)
        self._close()
        self._key = key
        try:
            from hailo_platform import (HEF, ConfigureParams, FormatType, HailoStreamInterface,
                                        InferVStreams, InputVStreamParams, OutputVStreamParams, VDevice)
        except Exception as e:
            self.error = f"Hailo software not installed ({e}) - Raspberry Pi OS: sudo apt install hailo-all"
            raise RuntimeError(self.error) from e
        try:
            hef = HEF(str(path))
            vdev = VDevice()
            params = ConfigureParams.create_from_hef(hef=hef, interface=HailoStreamInterface.PCIe)
            group = vdev.configure(hef, params)[0]
            try:                                     # HailoRT >= 4.18
                ip = InputVStreamParams.make(group, format_type=FormatType.UINT8)
                op = OutputVStreamParams.make(group, format_type=FormatType.FLOAT32)
            except TypeError:                        # older HailoRT
                ip = InputVStreamParams.make(group, quantized=False, format_type=FormatType.UINT8)
                op = OutputVStreamParams.make(group, quantized=False, format_type=FormatType.FLOAT32)
            pipeline = InferVStreams(group, ip, op)
            pipeline.__enter__()
            activation = group.activate(group.create_params())
            activation.__enter__()
            inp = hef.get_input_vstream_infos()[0]
            h, w = int(inp.shape[0]), int(inp.shape[1])
        except Exception as e:
            self.error = f"AI HAT: could not load {path.name}: {e} (check: hailortcli fw-control identify)"
            raise RuntimeError(self.error) from e
        labels_file = path.with_suffix(".txt")
        labels = [l.strip() for l in labels_file.read_text().splitlines() if l.strip()] \
            if labels_file.exists() else []
        self._ctx = (vdev, pipeline, activation, inp.name, (h, w), labels)
        self.error = ""
        self.info = f"{path.name} on the AI HAT · input {w}x{h}" + \
                    (f" · {len(labels)} classes: " + ", ".join(labels[:8]) if labels else "")
        # warm-up so the first can isn't slow
        pipeline.infer({inp.name: np.zeros((1, h, w, 3), np.uint8)})
        return self._ctx

    def preload(self, p):
        with self._lock:
            try:
                self._load(p)
            except RuntimeError:
                pass

    # -------------------------------------------------------------- inference
    def analyze(self, frame, p, live: bool = False):
        from .vision import Busy
        if not self._lock.acquire(blocking=not live):
            raise Busy()
        try:
            ctx = self._load(p)
            return self._run(ctx, frame, p)
        finally:
            self._lock.release()

    @staticmethod
    def _letterbox(img, w, h):
        ih, iw = img.shape[:2]
        s = min(w / iw, h / ih)
        nw, nh = max(1, int(round(iw * s))), max(1, int(round(ih * s)))
        out = np.full((h, w, 3), 114, np.uint8)
        px, py = (w - nw) // 2, (h - nh) // 2
        out[py:py + nh, px:px + nw] = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
        return out, s, px, py

    @staticmethod
    def _per_class(out):
        """NMS output -> list (one entry per class) of arrays [k, 5]: y0, x0, y1, x1, score (0..1)."""
        if isinstance(out, (list, tuple)):
            first = out[0] if len(out) else []
            if isinstance(first, (list, tuple)):          # [batch][class] -> array
                return [np.asarray(a, np.float32).reshape(-1, 5) for a in first]
            return [np.asarray(a, np.float32).reshape(-1, 5) for a in out]
        arr = np.asarray(out, np.float32)
        if arr.ndim == 4 and arr.shape[2] == 5:            # (batch, classes, 5, max)
            return [c.T for c in arr[0]]
        if arr.ndim == 3 and arr.shape[-1] == 5:           # (classes, max, 5)
            return list(arr)
        raise RuntimeError(f"unexpected model output {arr.shape} - compile the .hef with the NMS post-process")

    def _run(self, ctx, frame, p):
        from .vision import GOOD_NAMES, Defect, Vision, VisionResult, _blur, to_work
        _, pipeline, _, in_name, (mh, mw), labels = ctx
        image = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR) if frame.ndim == 2 else \
            cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR) if frame.shape[2] == 4 else frame
        work = to_work(image)
        blur = _blur(work)
        scale = work.shape[1] / image.shape[1]

        circle, ox, oy, crop = None, 0, 0, image
        if p.yolo_crop:                                     # crop to the can so small defects stay visible
            circle = Vision.find_can(blur)
            if circle is not None:
                cx, cy, r = (v / scale for v in circle)
                half = int(r * 1.08)
                x0, y0 = max(0, int(cx - half)), max(0, int(cy - half))
                x1, y1 = min(image.shape[1], int(cx + half)), min(image.shape[0], int(cy + half))
                if x1 - x0 > 32 and y1 - y0 > 32:
                    ox, oy, crop = x0, y0, image[y0:y1, x0:x1]

        inp, s, px, py = self._letterbox(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB), mw, mh)
        t0 = time.perf_counter()
        try:
            res = pipeline.infer({in_name: inp[None]})
            per_class = self._per_class(next(iter(res.values())))
        except Exception as e:
            self.error = f"AI HAT inference failed: {e}"
            raise RuntimeError(self.error) from e
        self.ms = (time.perf_counter() - t0) * 1000
        self.error = ""

        h, w = work.shape[:2]
        defects, confs = [], []
        for k, dets in enumerate(per_class):
            label = labels[k] if k < len(labels) else f"defect {k}"
            if label.lower() in GOOD_NAMES:
                continue
            for y0, x0, y1, x1, score in dets:
                if score < p.yolo_conf:
                    continue
                # model input (letterboxed) -> crop -> full image -> work image
                bx0 = ((x0 * mw - px) / s + ox) * scale
                by0 = ((y0 * mh - py) / s + oy) * scale
                bx1 = ((x1 * mw - px) / s + ox) * scale
                by1 = ((y1 * mh - py) / s + oy) * scale
                x, y = int(np.clip(bx0, 0, w - 1)), int(np.clip(by0, 0, h - 1))
                bw, bh = max(1, int(bx1 - bx0)), max(1, int(by1 - by0))
                defects.append(Defect(label.replace("_", " ").capitalize(), x, y, bw, bh, bw * bh, float(score)))
                confs.append(float(score))
        defects.sort(key=lambda d: d.strength, reverse=True)
        conf = max(confs) if confs else 0.95
        mask = np.zeros_like(blur)
        for d in defects:
            cv2.rectangle(mask, (d.x, d.y), (d.x + d.w, d.y + d.h), 255, -1)
        return VisionResult(not defects, defects, circle, float(conf), self.ms, "hailo",
                            work, blur, np.zeros_like(blur), mask)
