#!/usr/bin/env python3
"""
Genie Nano video on Windows / macOS - the Aravis bridge.

Aravis (the open-source GigE Vision library the station uses on the Pi) only
installs into MSYS2's Python on Windows, not the normal Windows Python that
runs the dashboard. This small program runs in MSYS2, talks to the camera with
Aravis, and passes the frames to the dashboard over a local connection
(127.0.0.1:8765). The dashboard picks it up automatically when the source is
"Genie Nano" and Aravis isn't available in its own Python.

One-time setup (Windows):
  1. Install MSYS2 from https://www.msys2.org
  2. Open "MSYS2 UCRT64" from the Start menu and run:
       pacman -S --needed mingw-w64-ucrt-x86_64-aravis mingw-w64-ucrt-x86_64-python-gobject
Each time:
  3. In MSYS2 UCRT64:   cd /c/path/to/inspection-station   (C:\\ -> /c/)
                        python camera_bridge.py
  4. Start the dashboard as usual (normal Windows Python): python run.py
     Settings > Camera > Source: Genie Nano.

Only uses Aravis + the Python standard library (no numpy / OpenCV needed here).
"""
import argparse
import json
import os
import socket
import subprocess
import struct
import sys
import threading
import time

PORT = 8765
PREFERRED = ["BayerRG8", "BayerBG8", "BayerGB8", "BayerGR8", "Mono8", "RGB8", "BGR8",
             "RGB8Packed", "BGR8Packed", "Mono10", "Mono12", "Mono16"]
MAX_FPS = 30


def load_aravis():
    try:
        import gi
        gi.require_version("Aravis", "0.8")
        from gi.repository import Aravis
        return Aravis
    except (ImportError, ValueError) as e:
        sys.exit(f"Aravis not available in this Python ({e}).\n"
                 "Run this in 'MSYS2 UCRT64' after:\n"
                 "  pacman -S --needed mingw-w64-ucrt-x86_64-aravis mingw-w64-ucrt-x86_64-python-gobject")


class Bridge:
    def __init__(self, Ar, ip, port, fps, packet_size=None):
        self.Ar, self.want_ip, self.port, self.fps = Ar, ip, port, fps
        self.fixed_packet = packet_size
        self.cam = self.stream = None
        self.info = {}
        self.latest = None                      # (seq, w, h, fmt, bytes)
        self.seq = 0
        self.cond = threading.Condition()
        self.stop = threading.Event()
        self.problem = ""                       # shown in the dashboard when no video arrives
        self.payload = 0

    # ------------------------------------------------------------- camera
    def open_camera(self):
        Ar = self.Ar
        Ar.update_device_list()
        devs = [(Ar.get_device_id(i), Ar.get_device_address(i)) for i in range(Ar.get_n_devices())]
        if not devs:
            raise RuntimeError("no GigE camera found (check power / cable; run find_camera.py)")
        if self.want_ip:
            pick = [d for d in devs if d[1] == self.want_ip] or (devs if len(devs) == 1 else [])
            if not pick:
                raise RuntimeError(f"no camera at {self.want_ip}; found: {devs}")
        else:
            pick = devs[:1]
        dev_id, ip = pick[0]
        cam = Ar.Camera.new(dev_id)
        try:
            if cam.is_gv_device():
                if self.fixed_packet:
                    cam.gv_set_packet_size(int(self.fixed_packet))
                else:
                    cam.gv_auto_packet_size()
        except Exception:
            pass
        try:
            avail = list(cam.dup_available_pixel_formats_as_strings())
        except Exception:
            avail = []
        for f in PREFERRED:
            if f in avail:
                try:
                    cam.set_pixel_format_from_string(f)
                    break
                except Exception:
                    pass
        try:
            cam.set_string("TriggerMode", "Off")
        except Exception:
            pass
        for name in ("turboTransferEnable", "TurboTransferEnable", "turboDriveEnable"):
            try:                                     # Teledyne compression - Aravis can't unpack it
                if cam.get_boolean(name):
                    cam.set_boolean(name, False)
                    print(f"TurboDrive compression turned off ({name})")
                break
            except Exception:
                continue
        cam.set_acquisition_mode(Ar.AcquisitionMode.CONTINUOUS)
        self.cam = cam
        self.set_fps(self.fps)
        payload = cam.get_payload()
        self.stream = cam.create_stream(None, None)
        linux = sys.platform.startswith("linux")
        # On the Pi the stream only works reliably with Aravis' own defaults (same as
        # arv-camera-test / pi_check, which get every frame); the extra tuning below is
        # for laptop network ports on Windows / macOS.
        props = (("packet-resend", "GvStreamPacketResend", "ALWAYS"),) + (
            () if linux else (("socket-buffer", "GvStreamSocketBuffer", "AUTO"),))
        for prop, enum, value in props:
            try:
                self.stream.set_property(prop, getattr(getattr(Ar, enum), value))
            except Exception:
                pass
        for prop, value in (() if linux else (("frame-retention", 500_000), ("packet-timeout", 40_000))):
            try:                                     # more patience for a laptop network port
                self.stream.set_property(prop, value)
            except Exception:
                pass
        try:
            print(f"Network packet size: {cam.gv_get_packet_size()} bytes")
        except Exception:
            pass
        self.payload = payload
        for _ in range(10):
            self.stream.push_buffer(Ar.Buffer.new_allocate(payload))
        cam.start_acquisition()
        self.info = {"vendor": cam.get_vendor_name(), "model": cam.get_model_name(),
                     "serial": cam.get_device_serial_number(), "ip": ip,
                     "fmt": cam.get_pixel_format_as_string()}
        print(f"Camera: {self.info['vendor']} {self.info['model']} at {ip} - {self.info['fmt']}")

    def set_fps(self, fps):
        """Change the frame rate. The Genie Nano stops sending frames if the rate is
        changed while it streams, so pause acquisition around the change."""
        try:
            lo, hi = self.cam.get_frame_rate_bounds()
            want = float(max(lo, min(fps, hi, MAX_FPS)))
            if abs(self.cam.get_frame_rate() - want) < 0.05:
                return
            running = self.stream is not None
            if running:
                self.cam.stop_acquisition()
            self.cam.set_frame_rate(want)
            if running:
                self.cam.start_acquisition()
            print(f"  frame rate set to {want:.1f} fps")
        except Exception as e:
            print(f"  (could not set the frame rate: {e})")

    def apply(self, s):
        cam, Ar = self.cam, self.Ar
        if "fps" in s:
            self.set_fps(float(s["fps"]))
        for key, avail, auto, bounds, setter in (
                ("exposure_us", cam.is_exposure_time_available, cam.set_exposure_time_auto,
                 cam.get_exposure_time_bounds, cam.set_exposure_time),
                ("gain_db", cam.is_gain_available, cam.set_gain_auto, cam.get_gain_bounds, cam.set_gain)):
            if key not in s:
                continue
            try:
                if avail():
                    try:
                        auto(Ar.Auto.OFF)
                    except Exception:
                        pass
                    lo, hi = bounds()
                    setter(float(min(max(float(s[key]), lo), hi)))
            except Exception:
                pass

    def temperature(self):
        try:
            return float(self.cam.get_float("DeviceTemperature"))
        except Exception:
            return None

    def grab_loop(self):
        Ar = self.Ar
        ok, bad = 0, {}
        t0 = start = time.monotonic()
        ever_ok = small_tried = False
        while not self.stop.is_set():
            buf = self.stream.timeout_pop_buffer(1_000_000)
            if buf is not None:
                try:
                    st = buf.get_status()
                    data = bytes(buf.get_data()) if st == Ar.BufferStatus.SUCCESS else b""
                    w, h = (buf.get_image_width(), buf.get_image_height()) if data else (0, 0)
                    need = w * h * (3 if self.info["fmt"].startswith(("RGB8", "BGR8")) else
                                    2 if self.info["fmt"] in ("Mono10", "Mono12", "Mono16") else 1)
                    if data and len(data) < need:
                        bad["cut short"] = bad.get("cut short", 0) + 1
                        if not getattr(self, "_short_told", False):
                            self._short_told = True
                            print(f"  Incomplete frame: {len(data):,} of {need:,} bytes - skipped "
                                  f"(packet size {self.packet_size()} bytes). If this keeps happening, restart "
                                  f"with: python camera_bridge.py --packet-size 1440")
                        st = None
                    if st == Ar.BufferStatus.SUCCESS:
                        frame = (w, h, self.info["fmt"], data)
                        with self.cond:
                            self.seq += 1
                            self.latest = (self.seq,) + frame
                            self.cond.notify_all()
                        ok += 1
                        ever_ok = True
                    else:
                        name = getattr(st, "value_nick", str(st))
                        bad[name] = bad.get(name, 0) + 1
                finally:
                    self.stream.push_buffer(buf)
            now = time.monotonic()
            if now - t0 < 5:
                continue
            secs = now - t0
            if ok:
                self.problem = ""
                extra = f", {sum(bad.values())} incomplete" if bad else ""
                print(f"  {ok / secs:.1f} fps from the camera{extra}")
            elif not small_tried and not sys.platform.startswith("linux"):
                small_tried = True
                why = ", ".join(f"{n} {k}" for k, n in bad.items()) or "nothing at all"
                print(f"  No complete frames in {secs:.0f} s (received: {why}).")
                print("  Trying smaller network packets (1440 bytes)...")
                self.small_packets()
            else:
                self.report_no_video(bad)
            ok, bad, t0 = 0, {}, time.monotonic()

    def packet_size(self):
        try:
            return self.cam.gv_get_packet_size()
        except Exception:
            return "?"

    def small_packets(self):
        """Laptop network ports often drop the camera's big packets - use standard-size ones."""
        try:
            self.cam.stop_acquisition()
            self.cam.gv_set_packet_size(1440)
            self.cam.start_acquisition()
        except Exception as e:
            print(f"  (could not change the packet size: {e})")

    def report_no_video(self, bad):
        if bad:
            why = ", ".join(f"{n} {k}" for k, n in bad.items())
            self.problem = (f"frames arrive incomplete ({why}) - packets are being lost: use a direct cable, "
                            "plug the laptop into power, lower the frame rate")
            print(f"  ! {self.problem}")
            return
        self.problem = ("the camera is sending, but no video packets reach this computer - Windows Firewall "
                        "is blocking them. Allow MSYS2's python.exe on Public networks (see the bridge window)")
        if getattr(self, "_told", False):
            print("  ! still no video packets - see the firewall fix above")
            return
        self._told = True
        exe = self.windows_exe()
        print("  ! NO video packets reach this computer. The camera is controlled fine, so this is almost")
        print("    always Windows Firewall blocking incoming video (UDP) for this program. Fix - either:")
        print("    a) Windows Security > Firewall & network protection > Allow an app through firewall >")
        print(f"       Change settings > Allow another app > Browse: {exe}  > tick Private AND Public")
        print("    b) or in PowerShell 'Run as administrator':")
        print(f'       netsh advfirewall firewall add rule name="Aravis camera bridge" dir=in action=allow '
              f'program="{exe}" enable=yes profile=any')
        print("    Then restart this bridge. (Quick test: turn the firewall off for Public networks for a minute.)")
        print("    Also turn off VPN software on the Ethernet adapter.")

    @staticmethod
    def windows_exe():
        exe = sys.executable
        if exe.startswith("/"):
            try:
                exe = subprocess.run(["cygpath", "-w", exe], capture_output=True, text=True).stdout.strip() or exe
            except OSError:
                pass
        return exe.replace("/", "\\") if len(exe) > 1 and exe[1] == ":" else exe

    # ------------------------------------------------------------- serving
    @staticmethod
    def _msg(kind: bytes, payload: bytes) -> bytes:
        return kind + struct.pack(">I", len(payload)) + payload

    def serve_client(self, conn):
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        alive = threading.Event()
        alive.set()

        def reader():                                   # settings from the dashboard (JSON lines)
            f = conn.makefile("r")
            try:
                for line in f:
                    try:
                        self.apply(json.loads(line))
                    except (ValueError, TypeError):
                        pass
            except OSError:
                pass
            alive.clear()
        threading.Thread(target=reader, daemon=True).start()

        sent, last_info = 0, 0.0
        try:
            while alive.is_set() and not self.stop.is_set():
                if time.monotonic() - last_info > (1 if self.problem else 5):
                    last_info = time.monotonic()
                    conn.sendall(self._msg(b"ARVI", json.dumps(self.info | {"temp": self.temperature(),
                                                                             "problem": self.problem}).encode()))
                with self.cond:
                    self.cond.wait_for(lambda: self.latest and self.latest[0] != sent, timeout=1.0)
                    latest = self.latest
                if not latest or latest[0] == sent:
                    continue
                seq, w, h, fmt, data = latest
                head = struct.pack(">II16s", w, h, fmt.encode()[:16])
                conn.sendall(self._msg(b"ARVF", head + data))   # always the newest frame; older ones are dropped
                sent = seq
        except OSError:
            pass
        finally:
            conn.close()

    def run(self):
        self.open_camera()
        threading.Thread(target=self.grab_loop, daemon=True).start()
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", self.port))
        except OSError:
            sys.exit(f"Port {self.port} is busy - is the bridge already running in another window? Close it first.")
        srv.listen(1)
        print(f"Bridge ready on 127.0.0.1:{self.port} - start the dashboard and choose the Genie Nano source.")
        print("Stop with Ctrl+C.")
        try:
            while True:
                conn, _ = srv.accept()
                print("Dashboard connected")
                self.serve_client(conn)
                print("Dashboard disconnected - waiting")
        except KeyboardInterrupt:
            pass
        finally:
            self.stop.set()
            try:
                self.cam.stop_acquisition()
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser(description="Stream the Genie Nano to the dashboard (Windows / macOS)")
    ap.add_argument("--ip", help="camera IP (default: the first camera found)")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--fps", type=float, default=20)
    ap.add_argument("--packet-size", type=int, help="e.g. 1440 if frames keep arriving incomplete")
    a = ap.parse_args()
    Bridge(load_aravis(), a.ip, a.port, a.fps, a.packet_size).run()


if __name__ == "__main__":
    main()
