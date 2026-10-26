"""
Camera sources, frame grabbing, live processing and image saving.

Sources (Settings.camera_backend)
  gige      Teledyne DALSA Genie Nano (any GigE Vision camera works) via Aravis.
            On the Pi:  sudo apt install gir1.2-aravis-0.8 python3-gi aravis-tools
  webcam    USB / laptop webcam via OpenCV - for testing away from the line.
  synthetic Generated top-view cans with random dents, scratches, contamination.
  folder    Replays images from a folder (saved images, sample defect photos).

Threads
  grab   reads frames continuously; keeps the latest frame + its timestamp
  live   runs detection on the latest frame for the Camera tab (only while visible)
  writer saves images to disk so inspection never waits on the SD card
"""
from __future__ import annotations

import base64
import os
import struct
import socket
import json
import queue
import random
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from .config import APP_DIR, DATA_DIR
from .vision import Busy, Vision, VisionParams, render

SOURCES = {
    "gige": "Genie Nano",
    "webcam": "Webcam",
    "synthetic": "Synthetic",
    "folder": "Image folder",
}


class AravisMissing(Exception):
    """Aravis isn't installed in this Python (Windows / macOS) - try the bridge."""


class CameraError(Exception):
    pass


# =============================================================================
# Sources - each has open(settings), read() -> frame | None, apply(settings),
#           on_trigger(), close(), and .info
# =============================================================================
class GigECamera:
    """
    Teledyne DALSA Genie Nano (or any GigE Vision camera) through Aravis.

    Genie Nano notes
      * Color models (Nano-C...) stream BayerRG8 -> converted to BGR here.
        Mono models (Nano-M...) stream Mono8 (Mono10/12 also handled).
      * Out of the box the Nano asks for DHCP and falls back to a link-local
        169.254.x.x address. If no camera answers at the configured IP but
        exactly one GigE camera is on the network, it is used automatically
        and its IP is saved.
      * Gain on the Nano is a multiplier (about 1x-8x, model dependent); values
        outside the camera's range are clamped.
    """
    PREFERRED_FORMATS = ["BayerRG8", "BayerBG8", "BayerGB8", "BayerGR8",
                         "RGB8", "BGR8", "RGB8Packed", "BGR8Packed", "Mono8",
                         "Mono10", "Mono12", "Mono16"]
    # GenICam Bayer names -> OpenCV codes (OpenCV names are offset by one row)
    BAYER = {"BayerRG8": cv2.COLOR_BayerBG2BGR, "BayerBG8": cv2.COLOR_BayerRG2BGR,
             "BayerGB8": cv2.COLOR_BayerGR2BGR, "BayerGR8": cv2.COLOR_BayerGB2BGR}
    MAX_FPS = 30          # plenty for live view; keeps Pi CPU and network load low
    N_BUFFERS = 10

    def __init__(self):
        self.cam = self.stream = self.Ar = None
        self.fmt = ""
        self.info = ""
        self.note = ""        # message for the event log (e.g. camera found at another IP)
        self.ip = ""

    def open(self, s):
        try:
            import gi
            gi.require_version("Aravis", "0.8")
            from gi.repository import Aravis
        except (ImportError, ValueError):
            raise AravisMissing()
        self.Ar = Aravis
        Aravis.update_device_list()
        found = [(Aravis.get_device_id(i), Aravis.get_device_address(i))
                 for i in range(Aravis.get_n_devices())]
        match = [(d, a) for d, a in found if a == s.camera_ip]
        if not match and len(found) == 1:
            match = found
            self.note = (f"No camera at {s.camera_ip}; using the only camera found, "
                         f"{found[0][0]} at {found[0][1]}")
        if not match:
            if found:
                listing = ", ".join(f"{d} at {a}" for d, a in found)
                raise CameraError(f"no camera at {s.camera_ip} - found: {listing}")
            raise CameraError("no GigE camera found - check cable, camera power (PoE / 12V) "
                              "and that the Pi is on the camera's subnet")
        dev_id, self.ip = match[0]
        try:
            cam = Aravis.Camera.new(dev_id)
        except Exception as e:
            raise CameraError(f"cannot open {dev_id}: {e} (is another program using it?)")

        try:
            if cam.is_gv_device():
                cam.gv_auto_packet_size()          # largest packet the network path allows
        except Exception:
            pass
        self._choose_format(cam)
        turbo = disable_turbodrive(cam)
        if turbo:
            self.note = f"{self.note}; {turbo}" if self.note else turbo
        for feature, value in (("TriggerMode", "Off"),):   # free-running; frame picked by time
            try:
                cam.set_string(feature, value)
            except Exception:
                pass
        cam.set_acquisition_mode(Aravis.AcquisitionMode.CONTINUOUS)
        try:
            lo, hi = cam.get_frame_rate_bounds()
            cam.set_frame_rate(float(min(self.MAX_FPS, hi)))
        except Exception:
            pass
        self.cam = cam
        self.apply(s)

        payload = cam.get_payload()
        self.stream = cam.create_stream(None, None)
        if self.stream is None:
            raise CameraError("could not create the image stream")
        for prop, value in (("packet-resend", "ALWAYS"),):   # Aravis defaults otherwise (see camera_bridge.py)
            try:                                    # fewer dropped frames on the Pi
                enum = Aravis.GvStreamPacketResend if prop == "packet-resend" else Aravis.GvStreamSocketBuffer
                self.stream.set_property(prop, getattr(enum, value))
            except Exception:
                pass
        for _ in range(self.N_BUFFERS):
            self.stream.push_buffer(Aravis.Buffer.new_allocate(payload))
        cam.start_acquisition()
        self.info = f"{cam.get_vendor_name()} {cam.get_model_name()} - {self.fmt}"

    def _choose_format(self, cam):
        try:
            available = list(cam.dup_available_pixel_formats_as_strings())
        except Exception:
            available = []
        for f in self.PREFERRED_FORMATS:
            if f in available:
                try:
                    cam.set_pixel_format_from_string(f)
                    break
                except Exception:
                    continue
        self.fmt = cam.get_pixel_format_as_string()

    def apply(self, s):
        cam, Ar = self.cam, self.Ar
        if cam is None:
            return
        try:
            if cam.is_exposure_time_available():
                try:
                    cam.set_exposure_time_auto(Ar.Auto.OFF)
                except Exception:
                    pass
                lo, hi = cam.get_exposure_time_bounds()
                cam.set_exposure_time(float(min(max(s.exposure_us, lo), hi)))
        except Exception:
            pass
        try:
            if cam.is_gain_available():
                try:
                    cam.set_gain_auto(Ar.Auto.OFF)
                except Exception:
                    pass
                lo, hi = cam.get_gain_bounds()
                cam.set_gain(float(min(max(s.gain_db, lo), hi)))
        except Exception:
            pass

    def read(self):
        buf = self.stream.timeout_pop_buffer(1_000_000)      # 1 s, in microseconds
        if buf is None:
            self._timeouts = getattr(self, "_timeouts", 0) + 1
            if self._timeouts >= 5:
                self._timeouts = 0
                raise CameraError(f"no frames for 5 s ({self._stream_stats()}) - "
                                  "check the network buffers / firewall; reconnecting")
            return None
        self._timeouts = 0
        try:
            status = buf.get_status()
            if status != self.Ar.BufferStatus.SUCCESS:
                return self._bad(status)
            self._bads = 0
            try:
                frame = self._convert(buf.get_data(), buf.get_image_width(), buf.get_image_height())
            except ShortFrame as e:
                return self._short(e)
            self._shorts = 0
            return frame
        finally:
            self.stream.push_buffer(buf)

    def _stream_stats(self) -> str:
        try:
            ok, fail, under = self.stream.get_statistics()
            return f"{ok} frames ok, {fail} failed, {under} no free buffer"
        except Exception:
            return "no statistics"

    def _bad(self, status):
        """A frame the camera sent but that did not arrive complete. Skipping it
        silently made the picture look frozen - count them and reconnect with a
        clear message instead (reconnecting also restarts the stream)."""
        self._bads = getattr(self, "_bads", 0) + 1
        name = getattr(status, "value_nick", None) or str(status)
        if self._bads == 10:
            self._slow_down()
        if self._bads >= 30:
            self._bads = 0
            raise CameraError(f"frames arrive incomplete ({name}; {self._stream_stats()}) - network buffers too small, "
                              "or lower the frame rate; run: sudo sysctl -w net.core.rmem_max=33554432")
        return None

    def _slow_down(self):
        """Space the packets out and use standard-size packets - helps a Pi that drops bursts."""
        for feature, value in (("GevSCPSPacketSize", 1400), ("GevSCPD", 2000)):
            try:
                self.cam.set_integer(feature, value)
            except Exception:
                pass

    def _short(self, e):
        """Skip an incomplete frame; only give up if it keeps happening."""
        self._shorts = getattr(self, "_shorts", 0) + 1
        if self._shorts >= 30:
            self._shorts = 0
            raise CameraError(f"{e} - every frame arrives incomplete: lower the frame rate, use a direct cable, "
                              "plug the laptop into power")
        return None

    def _convert(self, data, w, h):
        return convert_frame(data, w, h, self.fmt)

    def set_fps(self, fps: float):
        """Pause acquisition around the change - the Genie Nano stops sending frames
        if its frame rate is changed while streaming."""
        try:
            lo, hi = self.cam.get_frame_rate_bounds()
            want = float(max(lo, min(fps, hi, self.MAX_FPS)))
            if abs(self.cam.get_frame_rate() - want) < 0.05:
                return
            running = self.stream is not None
            if running:
                self.cam.stop_acquisition()
            self.cam.set_frame_rate(want)
            if running:
                self.cam.start_acquisition()
        except Exception:
            pass

    def temperature(self):
        """Camera's internal temperature in °C (GenICam DeviceTemperature), or None."""
        try:
            return float(self.cam.get_float("DeviceTemperature"))
        except Exception:
            return None

    def on_trigger(self):
        pass

    def close(self):
        try:
            if self.cam is not None:
                self.cam.stop_acquisition()
        except Exception:
            pass
        self.cam = self.stream = None


def _webcam_api():
    """The OS camera API: DirectShow on Windows (fast to open, stable numbering),
    AVFoundation on macOS, V4L2 on Linux."""
    if sys.platform.startswith("win"):
        return cv2.CAP_DSHOW
    if sys.platform == "darwin":
        return cv2.CAP_AVFOUNDATION
    return cv2.CAP_V4L2


def open_webcam(index: int):
    """Open camera <index>, or return None. Tries the OS API first, then OpenCV's default."""
    for api in (_webcam_api(), cv2.CAP_ANY):
        cap = cv2.VideoCapture(index, api)
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            return cap
        cap.release()
    return None


def thumb_data_url(frame, width=240) -> str:
    h, w = frame.shape[:2]
    small = cv2.resize(frame, (width, max(1, round(h * width / w))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode() if ok else ""


def scan_webcams(max_index=6, skip=None, skip_frame=None):
    """Find the cameras on this computer, with a preview of each so the right one
    can be picked (laptops often also have an IR face-login camera, phones and
    virtual cameras show up too). `skip` = index already open by the app."""
    found, misses = [], 0
    for i in range(max_index):
        if i == skip:
            item = {"index": i, "in_use": True}
            if skip_frame is not None:
                item.update(width=skip_frame.shape[1], height=skip_frame.shape[0], thumb=thumb_data_url(skip_frame))
            found.append(item)
            misses = 0
            continue
        cap = open_webcam(i)
        if cap is None:
            misses += 1
            if misses >= 2:
                break
            continue
        misses = 0
        frame = None
        try:
            for _ in range(8):                 # first frames are often black while exposure settles
                ok, f = cap.read()
                if ok and f is not None:
                    frame = f
        finally:
            cap.release()
        item = {"index": i, "in_use": False}
        if frame is not None:
            item.update(width=frame.shape[1], height=frame.shape[0], thumb=thumb_data_url(frame))
        found.append(item)
    return found


class WebcamCamera:
    def __init__(self):
        self.cap = None
        self.info = ""

    def open(self, s):
        self.cap = open_webcam(s.webcam_index)
        if self.cap is None:
            raise CameraError(f"no camera number {s.webcam_index} - use 'Find cameras' in Settings")
        w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.info = f"Camera {s.webcam_index} · {w}x{h}"

    def apply(self, s):
        pass        # webcam exposure control is driver-dependent; left automatic

    def read(self):
        ok, frame = self.cap.read()
        if not ok:
            time.sleep(0.05)
            return None
        return frame

    def on_trigger(self):
        pass

    def close(self):
        if self.cap is not None:
            self.cap.release()


class SyntheticCamera:
    """Top view of an empty can on a conveyor, with random defects."""
    W, H, FPS = 1024, 768, 30

    def __init__(self):
        self.info = f"Synthetic {self.W}x{self.H} mono"
        self.defect_rate = 0.1
        self.brightness = 1.0
        self.base = None
        self.truth = ""                # label of the defect drawn on the current can

    def open(self, s):
        rng = np.random.default_rng()
        self.yy, self.xx = np.mgrid[0:self.H, 0:self.W].astype(np.float32)
        self.noise = [rng.normal(0, 3, (self.H, self.W)).astype(np.int16) for _ in range(6)]
        self.apply(s)
        self._new_can(defective=False)
        self._k = 0
        self._next = time.monotonic()

    def apply(self, s):
        self.defect_rate = s.sim_defect_rate / 100
        self.brightness = (s.exposure_us / 2000) * 10 ** (s.gain_db / 20)

    def set_fps(self, fps: float):
        self.FPS = max(1, min(30, fps))

    def on_trigger(self):
        self._new_can(random.random() < self.defect_rate)

    def _new_can(self, defective: bool):
        """Next can from a pool of realistic can-top images (syncan.py), rendered in the background
        so a trigger never waits for drawing."""
        from .syncan import KINDS, render_can
        if not getattr(self, "_pool_started", False):
            self._pool_started = True
            self._pool = {"good": [], "bad": []}

            def fill():
                grid = (self.xx, self.yy)
                for i in range(40):
                    kind = None if i % 2 == 0 else KINDS[(i // 2) % len(KINDS)]
                    img, truth = render_can(kind, self.W, self.H, seed=random.randint(0, 10**9), grid=grid)
                    self._pool["bad" if kind else "good"].append((img.astype(np.int16), truth["kind"]))
            threading.Thread(target=fill, daemon=True, name="synthetic-cans").start()
        pool = self._pool["bad" if defective else "good"]
        if pool:
            self.base, self.truth = random.choice(pool)
            return
        kind = random.choice(KINDS) if defective else None           # pool not ready yet: draw one now
        img, truth = render_can(kind, self.W, self.H, seed=random.randint(0, 10**9), grid=(self.xx, self.yy))
        self.base, self.truth = img.astype(np.int16), truth["kind"]

    def read(self):
        now = time.monotonic()
        if now < self._next:
            time.sleep(self._next - now)
        self._next = max(self._next + 1 / self.FPS, time.monotonic())
        self._k = (self._k + 1) % len(self.noise)
        frame = np.clip(self.base + self.noise[self._k], 0, 255).astype(np.uint8)
        if abs(self.brightness - 1) > 0.01:
            frame = cv2.convertScaleAbs(frame, alpha=self.brightness)
        return frame

    def close(self):
        pass


class FolderCamera:
    """Shows one image from a folder; advances to the next on every can."""
    EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}

    def __init__(self):
        self.files, self.i, self.current, self.info = [], -1, None, ""

    def open(self, s):
        p = Path(s.image_folder) if s.image_folder else None
        if p is None or not p.is_dir():
            raise CameraError("choose an image folder")
        self.files = sorted(f for f in p.rglob("*") if f.suffix.lower() in self.EXTS)
        if not self.files:
            raise CameraError(f"no images in {p}")
        self.info = f"{len(self.files)} images in {p.name}"
        self.on_trigger()

    def apply(self, s):
        pass

    def on_trigger(self):
        for _ in range(len(self.files)):
            self.i = (self.i + 1) % len(self.files)
            img = cv2.imread(str(self.files[self.i]))
            if img is not None:
                self.current = img
                return

    def read(self):
        time.sleep(1 / 15)
        return self.current

    def close(self):
        pass


class ShortFrame(CameraError):
    """A frame with less data than its size needs (cut off / compressed) - skipped."""


def frame_bytes(w, h, fmt) -> int:
    if fmt.startswith(("RGB8", "BGR8")):
        return w * h * 3
    if fmt in ("Mono10", "Mono12", "Mono16"):
        return w * h * 2
    return w * h


TURBO_FEATURES = ("turboTransferEnable", "TurboTransferEnable", "turboDriveEnable")


def disable_turbodrive(cam) -> str:
    """Teledyne TurboDrive compresses frames; only Teledyne's own software can unpack them."""
    for name in TURBO_FEATURES:
        try:
            if cam.get_boolean(name):
                cam.set_boolean(name, False)
                return f"TurboDrive compression turned off ({name})"
            return ""
        except Exception:
            continue
    return ""


def convert_frame(data, w, h, fmt):
    """Raw GigE Vision frame -> OpenCV image."""
    need = frame_bytes(w, h, fmt)
    if len(data) < need:
        raise ShortFrame(f"frame cut short: {len(data):,} of {need:,} bytes ({w}x{h} {fmt})")
    a = np.frombuffer(data, np.uint8)
    if fmt in GigECamera.BAYER:
        return cv2.cvtColor(a[:w * h].reshape(h, w), GigECamera.BAYER[fmt])
    if fmt.startswith("RGB8"):
        return cv2.cvtColor(a[:w * h * 3].reshape(h, w, 3), cv2.COLOR_RGB2BGR)
    if fmt.startswith("BGR8"):
        return a[:w * h * 3].reshape(h, w, 3).copy()
    if fmt == "Mono8":
        return a[:w * h].reshape(h, w).copy()
    if fmt in ("Mono10", "Mono12", "Mono16"):
        a16 = np.frombuffer(data, np.uint16)[:w * h].reshape(h, w)
        return (a16 >> {"Mono10": 2, "Mono12": 4, "Mono16": 8}[fmt]).astype(np.uint8)
    raise CameraError(f"unsupported pixel format {fmt}")


class BridgeCamera:
    """Genie Nano frames from camera_bridge.py (Aravis running in MSYS2 on Windows / macOS)."""
    ADDR = ("127.0.0.1", 8765)

    def __init__(self):
        self.sock = None
        self.info = self.note = self.ip = ""
        self._temp = None
        self._fps = None

    def open(self, s):
        try:
            self.sock = socket.create_connection(self.ADDR, timeout=1.0)
        except OSError:
            hint = ("start camera_bridge.py in 'MSYS2 UCRT64' (README > Camera > Live video on Windows)"
                    if sys.platform.startswith("win") else
                    "install Aravis: sudo apt install gir1.2-aravis-0.8 python3-gi aravis-tools"
                    if sys.platform.startswith("linux") else "start camera_bridge.py (README > Camera)")
            raise CameraError(f"camera found by 'Find camera' can't stream yet - {hint}")
        self.sock.settimeout(3.0)
        kind, payload = self._recv()               # the bridge says hello with camera info first
        if kind == b"ARVI":
            self._info(payload)
        self.apply(s)

    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise CameraError("bridge closed the connection")
            buf += chunk
        return bytes(buf)

    def _recv(self):
        head = self._recv_exact(8)
        kind, n = head[:4], struct.unpack(">I", head[4:])[0]
        if kind not in (b"ARVF", b"ARVI") or n > 64 * 2**20:
            raise CameraError("unexpected data from the bridge")
        return kind, self._recv_exact(n)

    def _info(self, payload):
        try:
            d = json.loads(payload)
        except ValueError:
            return
        self.ip = d.get("ip", "")
        self.info = f"{d.get('vendor', '')} {d.get('model', '')} - {d.get('fmt', '')} (via bridge)".strip()
        self._temp = d.get("temp")
        if d.get("problem"):
            raise CameraError(f"camera connected, but no video: {d['problem']}")

    def _send(self, d):
        try:
            self.sock.sendall((json.dumps(d) + "\n").encode())
        except OSError:
            pass

    def apply(self, s):
        self._send({"exposure_us": s.exposure_us, "gain_db": s.gain_db})

    def set_fps(self, fps):
        if fps != self._fps:
            self._fps = fps
            self._send({"fps": fps})

    def temperature(self):
        return self._temp

    def read(self):
        try:
            while True:
                kind, payload = self._recv()
                if kind == b"ARVI":
                    self._info(payload)
                    continue
                w, h, fmt = struct.unpack(">II16s", payload[:24])
                try:
                    frame = convert_frame(payload[24:], w, h, fmt.split(b"\0", 1)[0].decode())
                except ShortFrame as e:
                    self._shorts = getattr(self, "_shorts", 0) + 1
                    if self._shorts >= 30:
                        self._shorts = 0
                        raise CameraError(f"{e} - every frame arrives incomplete (see the bridge window)")
                    continue
                self._shorts = 0
                return frame
        except socket.timeout:
            return None
        except OSError as e:
            raise CameraError(f"bridge connection lost: {e}")

    def on_trigger(self):
        pass

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass


class GigEAny:
    """Genie Nano source: Aravis in this Python if available (Pi / Linux), otherwise the bridge."""

    def __init__(self):
        self.impl = None

    _bridge_proc = None

    def _start_bridge(self, s):
        """Run camera_bridge.py as its own process (CAN_GIGE=bridge): the camera then
        streams in a separate program, away from the station's detection load."""
        import subprocess
        proc = GigEAny._bridge_proc
        if proc is not None and proc.poll() is None:
            return
        try:                                     # one left running from an earlier start
            socket.create_connection(BridgeCamera.ADDR, timeout=0.2).close()
            return
        except OSError:
            pass
        script = Path(__file__).resolve().parent.parent / "camera_bridge.py"
        GigEAny._bridge_proc = subprocess.Popen(
            [sys.executable, str(script), "--ip", s.camera_ip, "--fps", str(s.camera_fps)])
        import atexit
        atexit.register(GigEAny._bridge_proc.terminate)      # don't leave it running after the station
        for _ in range(50):                      # wait up to 10 s for it to listen
            try:
                socket.create_connection(BridgeCamera.ADDR, timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.2)

    def open(self, s):
        # Linux (Pi): run the camera in its own process by default - streaming inside the
        # station froze after the first frame on the Pi. CAN_GIGE=direct to opt out.
        mode = os.environ.get("CAN_GIGE", "").lower() or ("bridge" if sys.platform.startswith("linux") else "direct")
        if mode == "bridge":
            self._start_bridge(s)
            self.impl = BridgeCamera()
            self.impl.open(s)
            return
        try:
            self.impl = GigECamera()
            self.impl.open(s)
        except AravisMissing:
            if sys.platform.startswith("linux"):
                bridge = BridgeCamera()
                try:
                    bridge.open(s)
                except CameraError as e:
                    if str(e).startswith("camera connected"):
                        raise
                    raise CameraError("Aravis not installed (sudo apt install gir1.2-aravis-0.8 "
                                      "python3-gi aravis-tools)")
                self.impl = bridge
            else:
                self.impl = BridgeCamera()
                self.impl.open(s)

    def __getattr__(self, name):
        impl = self.__dict__.get("impl")
        if impl is None:
            raise AttributeError(name)
        return getattr(impl, name)


SOURCE_CLASSES = {"gige": GigEAny, "webcam": WebcamCamera,
                  "synthetic": SyntheticCamera, "folder": FolderCamera}


# =============================================================================
# Image saving
# =============================================================================
class ImageStore:
    """Saves inspection images as a dataset: data/images/<date>/<pass|fail>/..."""
    MIN_FREE_MB = 500

    def __init__(self, log):
        self.log = log
        self.root = DATA_DIR / "images"
        self._q: queue.Queue = queue.Queue(maxsize=60)
        self._low_disk = False
        self._thread = threading.Thread(target=self._writer, daemon=True, name="image-writer")
        self._thread.start()

    def plan(self, can_id: int, passed: bool, label: str) -> Path:
        now = datetime.now()
        tag = "pass" if passed else "fail"
        name = f"can{can_id:06d}_{now:%H%M%S}"
        if label:
            name += "_" + label.lower().replace(" / ", "-").replace(" ", "-")
        return self.root / f"{now:%Y-%m-%d}" / tag / f"{name}.jpg"

    def save(self, path: Path, raw, annotated=None) -> bool:
        try:
            self._q.put_nowait((path, raw, annotated))
            return True
        except queue.Full:
            self.log("WARN", "Image writer busy - image dropped")
            return False

    def _writer(self):
        while True:
            path, raw, annotated = self._q.get()
            if path is None:
                return
            try:
                if shutil.disk_usage(APP_DIR).free < self.MIN_FREE_MB * 1024 * 1024:
                    if not self._low_disk:
                        self.log("ERROR", f"Less than {self.MIN_FREE_MB} MB free - image saving paused")
                        self._low_disk = True
                    continue
                self._low_disk = False
                path.parent.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(str(path), raw, [cv2.IMWRITE_JPEG_QUALITY, 92])
                if annotated is not None:
                    ap = path.parent / "annotated" / path.name
                    ap.parent.mkdir(exist_ok=True)
                    cv2.imwrite(str(ap), annotated, [cv2.IMWRITE_JPEG_QUALITY, 88])
            except Exception as e:
                self.log("ERROR", f"Could not save image {path.name}: {e}")

    def snapshot(self, frame) -> Path:
        path = DATA_DIR / "snapshots" / f"snapshot_{datetime.now():%Y%m%d_%H%M%S_%f}"[:-3]
        path = path.with_suffix(".png")
        path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(path), frame)
        return path

    def stop(self):
        self._q.put((None, None, None))
        self._thread.join(timeout=2)


# =============================================================================
# Camera manager
# =============================================================================
class CameraManager:
    def __init__(self, settings, log):
        self.s = settings
        self.log = log
        self.vision = Vision()
        self.store = ImageStore(log)

        self.source = None
        self.ok = False
        self.status = "Starting..."
        self.info = ""
        self.fps = 0.0
        self.resolution = None

        self._cond = threading.Condition()
        self._frame = None
        self._ts = 0.0
        self._seq = 0
        self._stop = threading.Event()
        self._reopen = threading.Event()
        self._reopen.set()
        self._apply = False
        self._last_error = ""
        self.on_ip_change = None             # set by the controller

        # live view (Camera tab)
        self.live_visible = False
        self.live_paused = False
        self.live_mode = "normal"            # normal | reduced | raw | off  (set by heat / standby)

        # heat & power
        self.fps_limit = settings.camera_fps
        self._applied_fps = None
        self.cam_temp = None                 # °C from the camera, if it reports it
        self._temp_at = 0.0
        self.live_view = "detect"
        self._live = None                    # (image, result, seq)
        self._live_lock = threading.Lock()

        # last inspected can
        self.last = None                     # dict
        self.last_seq = 0

        self._threads = [threading.Thread(target=self._grab_loop, daemon=True, name="camera-grab"),
                         threading.Thread(target=self._live_loop, daemon=True, name="camera-live")]

    def start(self):
        for t in self._threads:
            t.start()
        self.preload_model()

    def preload_model(self):
        """Warm up the YOLO model in the background when it is the selected engine."""
        from .edition import YOLO_AVAILABLE
        if YOLO_AVAILABLE and self.s.vision_engine == "yolo":
            p = self.params
            self.log("INFO", f"Loading YOLO model {self.s.yolo_model} ...")
            threading.Thread(target=self.vision.yolo.preload, args=(p,), daemon=True,
                             name="yolo-load").start()

    @property
    def params(self) -> VisionParams:
        return VisionParams(self.s.vision_sensitivity, self.s.vision_min_area,
                            area=tuple(self.s.inspect_area or (0, 0, 1, 1)),
                            engine=self.s.vision_engine, yolo_model=self.s.yolo_model,
                            yolo_conf=self.s.yolo_conf / 100, yolo_imgsz=self.s.yolo_imgsz,
                            yolo_crop=self.s.yolo_crop)

    # ---------------------------------------------------------------- control
    def switch_source(self, name: str):
        self.s.camera_backend = name
        self._reopen.set()

    def reopen(self):
        self._reopen.set()

    def apply_settings(self):
        self._apply = True

    def on_trigger(self):
        src = self.source
        if src is not None:
            try:
                src.on_trigger()
            except Exception:
                pass

    def find_webcams(self):
        """List cameras on this computer with previews. The one in use is not reopened."""
        skip = frame = None
        if self.s.camera_backend == "webcam" and self.ok:
            skip = self.s.webcam_index
            frame, _ = self.latest()
        return scan_webcams(skip=skip, skip_frame=frame)

    def snapshot(self):
        frame, _ = self.latest()
        return None if frame is None else self.store.snapshot(frame)

    def teach(self) -> bool:
        frame, _ = self.latest()
        if frame is None:
            return False
        located = self.vision.teach(frame)
        self.log("INFO", "Reference (good can) taught" + ("" if located else " - can not located, alignment off"))
        return True

    def clear_reference(self):
        self.vision.clear_reference()
        self.log("INFO", "Reference cleared - using local contrast detection")

    # -------------------------------------------------------------- brightness
    def measure(self):
        """(mean brightness 0-255, % of pixels blown out) of the newest frame; cached for 0.5 s."""
        now = time.monotonic()
        if now - getattr(self, "_meas_at", 0) < 0.5:
            return getattr(self, "_meas", None)
        self._meas_at = now
        frame, _ = self.latest()
        if frame is None or not self.ok:
            self._meas = None
            return None
        h, w = frame.shape[:2]
        small = cv2.resize(frame, (160, max(1, round(160 * h / w))), interpolation=cv2.INTER_AREA)
        if small.ndim == 3:
            small = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        self._meas = (float(small.mean()), float((small >= 250).mean() * 100))
        return self._meas

    def _wait_new_frames(self, n=3, timeout=3.0):
        _, seq0 = self.latest()
        end = time.monotonic() + timeout
        with self._cond:
            while self._seq < seq0 + n and time.monotonic() < end:
                self._cond.wait(0.2)

    def auto_exposure(self, done=None, target=115.0):
        """One-shot: adjust exposure (then gain) until the image is mid-grey. Runs in the background;
        done(message, ok) is called at the end. The result is saved like a manual setting."""
        if getattr(self, "_ae_running", False):
            raise ValueError("auto exposure already running")
        if not self.ok:
            raise ValueError("no camera image")
        self._ae_running = True

        def run():
            s = self.s
            ok, msg = False, ""
            try:
                max_us = int(min(200_000, 0.9e6 / max(1, self.fps_limit)))     # must fit in one frame time
                for _ in range(10):
                    self._wait_new_frames()
                    self._meas_at = 0
                    m = self.measure()
                    if m is None:
                        msg = "no camera image"
                        break
                    mean, sat = m
                    if abs(mean - target) < 15 and sat < 2:
                        ok = True
                        break
                    if sat > 5 and mean > target * 0.8:
                        factor = 0.5                                  # blown out: come down fast
                    else:
                        factor = min(4.0, max(0.25, target / max(mean, 1.0)))
                    new = int(min(max_us, max(20, s.exposure_us * factor)))
                    if new == s.exposure_us and factor > 1:           # exposure maxed out -> gain
                        if s.gain_db >= 8:
                            break
                        s.gain_db = min(8, max(1, s.gain_db) * 2)
                    else:
                        s.exposure_us = new
                    self.apply_settings()
                m = self.measure() or (0, 0)
                msg = (f"Exposure {s.exposure_us:,} µs, gain {s.gain_db} - brightness {m[0] / 2.55:.0f}%"
                       + ("" if ok else " (still too dark: open the lens aperture or add light)"
                          if m[0] < target * 0.7 else ""))
                if s.exposure_us > 3000:
                    msg += (". Long exposure blurs moving cans - fine for a still test, but on the running "
                            "line use more light and a shorter exposure (see Diagnostics for belt speed)")
                ok = ok or m[0] >= target * 0.7
            except Exception as e:
                msg = f"auto exposure failed: {e}"
            finally:
                self._ae_running = False
                if done:
                    done(msg, ok)
        threading.Thread(target=run, daemon=True, name="auto-exposure").start()

    # ------------------------------------------------------------------ frames
    def latest(self):
        with self._cond:
            return self._frame, self._seq

    def wait_frame_after(self, t: float, timeout: float = 0.5):
        """First frame that arrived at or after monotonic time t."""
        end = time.monotonic() + timeout
        with self._cond:
            while self._frame is None or self._ts < t:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("no camera frame in time")
                self._cond.wait(remaining)
            return self._frame, self._ts

    def live_frame(self):
        with self._live_lock:
            return self._live

    def publish(self, can_id, result, annotated, path):
        self.last = {"can_id": can_id, "result": result, "image": annotated,
                     "path": path, "time": datetime.now()}
        self.last_seq += 1

    # ------------------------------------------------------------------ loops
    def _open(self) -> bool:
        if self.source is not None:
            try:
                self.source.close()
            except Exception:
                pass
            self.source = None
        self.ok, self.fps, self.resolution, self.info = False, 0.0, None, ""
        self._applied_fps, self.cam_temp = None, None
        name = self.s.camera_backend if self.s.camera_backend in SOURCE_CLASSES else "synthetic"
        label = SOURCES[name]
        self.status = f"Opening {label}..."
        src = SOURCE_CLASSES[name]()
        try:
            src.open(self.s)
        except Exception as e:
            self.status = f"{label}: {e}"
            if str(e) != self._last_error:
                self.log("WARN", f"Camera - {label}: {e}")
                self._last_error = str(e)
            return False
        self._last_error = ""
        self.source, self.ok, self.info = src, True, src.info
        self.status = "Streaming"
        self.log("INFO", f"Camera source: {label} ({src.info})")
        if getattr(src, "note", ""):
            self.log("WARN", src.note)
            if src.ip:
                self.s.camera_ip = src.ip           # remember where the camera really is
                if self.on_ip_change:
                    self.on_ip_change(src.ip)
        return True

    def _grab_loop(self):
        last = time.monotonic()
        while not self._stop.is_set():
            if self._reopen.is_set():
                self._reopen.clear()
                if not self._open():
                    if self._stop.wait(3):           # retry every 3 s (camera unplugged etc.)
                        break
                    if not self._reopen.is_set():
                        self._reopen.set()
                    continue
            if self._apply:
                self._apply = False
                try:
                    self.source.apply(self.s)
                except Exception:
                    pass
            src = self.source
            if self.fps_limit != self._applied_fps:
                self._applied_fps = self.fps_limit
                if hasattr(src, "set_fps"):
                    src.set_fps(self.fps_limit)
            if hasattr(src, "temperature") and time.monotonic() - self._temp_at > 5:
                self._temp_at = time.monotonic()
                self.cam_temp = src.temperature()
            try:
                frame = src.read()
            except Exception as e:
                self.ok = False
                self.status = f"Read error: {e}"
                self.log("ERROR", f"Camera read failed: {e} - reconnecting")
                self._stop.wait(1)
                self._reopen.set()
                continue
            if frame is None:
                if time.monotonic() - last > 2:
                    self.fps = 0.0                   # don't keep showing a stale frame rate
                continue
            now = time.monotonic()
            with self._cond:
                self._frame, self._ts = frame, now
                self._seq += 1
                self._cond.notify_all()
            dt = now - last
            last = now
            if dt > 0:
                self.fps = 1 / dt if self.fps == 0 else self.fps * 0.9 + 0.1 / dt
            self.resolution = (frame.shape[1], frame.shape[0])
            if not hasattr(src, "set_fps"):              # webcam / folder: limit by waiting
                wait = 1 / max(1, self.fps_limit) - (time.monotonic() - now)
                if wait > 0:
                    self._stop.wait(wait)
        if self.source is not None:
            self.source.close()

    def _live_loop(self):
        shown = -1
        failed = False
        while not self._stop.is_set():
            mode = self.live_mode
            if not self.live_visible or self.live_paused or mode == "off":
                self._stop.wait(0.1)
                continue
            frame, seq = self.latest()
            if frame is None or seq == shown:
                self._stop.wait(0.02)
                continue
            shown = seq
            t0 = time.monotonic()
            try:
                view = "raw" if mode == "raw" else self.live_view     # hot: no live detection
                if self.s.vision_engine == "yolo" and not self.s.active_cooler and view != "raw":
                    view = "raw"         # no cooler: YOLO only on inspected cans (Last can panel)
                img, res = render(self.vision, frame, view, self.params)
                failed = False
            except Busy:                         # model busy with an inspection - skip this frame
                self._stop.wait(0.02)
                continue
            except Exception as e:
                if not failed:
                    self.log("ERROR", f"Live processing failed: {e}")
                failed = True
                continue
            with self._live_lock:
                self._live = (img, res, seq)
            cap = {"normal": 15, "reduced": 5, "raw": 2}.get(mode, 15)   # live view fps by heat level
            if self.s.vision_engine in ("yolo", "team") and view not in ("raw", "focus"):
                cap = min(cap, 4)            # heavy engines: inspections first, keep the Pi cool
            spare = 1 / cap - (time.monotonic() - t0)
            if spare > 0:
                self._stop.wait(spare)

    def stop(self):
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        for t in self._threads:
            if t.is_alive():
                t.join(timeout=2)
        self.store.stop()
