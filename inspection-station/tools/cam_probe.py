"""Read the Genie Nano exactly the way the station does, without the web server.

    python3 tools/cam_probe.py            (stop the station first)

Prints, every second: frames received, empty reads, and how long the slowest
call took. If this runs smoothly but the dashboard freezes, the problem is in
the station; if this freezes too, it is in how the camera is opened/read.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from inspection_station.camera import GigECamera   # noqa: E402
from inspection_station.config import Settings     # noqa: E402

s = Settings.load()
cam = GigECamera()
print(f"opening camera at {s.camera_ip} ...")
cam.open(s)
print("opened:", cam.info, "|", cam.note or "")
try:
    print("exposure", cam.cam.get_exposure_time(), "us   gain", cam.cam.get_gain(),
          "   frame rate", round(cam.cam.get_frame_rate(), 1))
except Exception as e:
    print("could not read exposure/gain:", e)

t_end = time.monotonic() + 20
sec = time.monotonic()
frames = empty = 0
slowest = ("", 0.0)
temp_at = 0
while time.monotonic() < t_end:
    if time.monotonic() - temp_at > 5:
        t0 = time.monotonic()
        t = cam.temperature()
        dt = time.monotonic() - t0
        temp_at = time.monotonic()
        print(f"  temperature {t}  ({dt * 1000:.0f} ms)")
    t0 = time.monotonic()
    try:
        f = cam.read()
    except Exception as e:
        print("  READ ERROR:", e)
        break
    dt = time.monotonic() - t0
    if dt > slowest[1]:
        slowest = ("read", dt)
    if f is None:
        empty += 1
    else:
        frames += 1
    if time.monotonic() - sec >= 1:
        try:
            stats = cam.stream.get_statistics()
        except Exception:
            stats = "?"
        print(f"{frames:3d} frames  {empty:2d} empty  slowest {slowest[1] * 1000:.0f} ms  "
              f"stream (ok, failed, no buffer) = {stats}")
        frames = empty = 0
        slowest = ("", 0.0)
        sec = time.monotonic()
cam.close()
print("done")
