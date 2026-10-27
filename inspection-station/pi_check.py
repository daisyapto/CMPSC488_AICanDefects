#!/usr/bin/env python3
"""
Raspberry Pi readiness check - run this ON THE PI before the first real run.

    python3 pi_check.py              everything except moving hardware
    python3 pi_check.py --sensor     also: wait 15 s for you to block the trigger sensor
    python3 pi_check.py --outputs    also: click each relay (lamps, then ONE short air pulse)

It only reads and reports (nothing is changed); every problem comes with the fix.
Stop the dashboard first (Ctrl+C / sudo systemctl stop can-inspection) so the
camera and GPIO are free for the tests.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

G, Y, R, B, D, X = "\033[32m", "\033[33m", "\033[31m", "\033[1m", "\033[2m", "\033[0m"
if not sys.stdout.isatty():
    G = Y = R = B = D = X = ""
results = {"ok": 0, "warn": 0, "fail": 0}


def ok(msg):
    results["ok"] += 1
    print(f"  {G}✔{X} {msg}")


def warn(msg, fix=""):
    results["warn"] += 1
    print(f"  {Y}!{X} {msg}")
    if fix:
        print(f"    {D}fix: {fix}{X}")


def fail(msg, fix=""):
    results["fail"] += 1
    print(f"  {R}✘ {msg}{X}")
    if fix:
        print(f"    {D}fix: {fix}{X}")


def section(title):
    print(f"\n{B}{title}{X}")


def run(cmd, timeout=5):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return -1, str(e)


def read(path):
    try:
        return Path(path).read_text(errors="ignore").strip("\x00\n ")
    except OSError:
        return None


def boot_config():
    for p in ("/boot/firmware/config.txt", "/boot/config.txt"):
        if Path(p).exists():
            return Path(p)
    return None


# =============================================================================
def check_system():
    section("1. Raspberry Pi and system")
    model = read("/proc/device-tree/model") or ""
    if "Raspberry Pi 5" in model:
        ok(model)
    elif "Raspberry Pi" in model:
        warn(f"{model} - the station was built for a Raspberry Pi 5 (pinctrl, GPIO numbering)")
    else:
        fail(f"not a Raspberry Pi ({model or 'unknown'}) - run this on the Pi")
    osr = read("/etc/os-release") or ""
    m = re.search(r'PRETTY_NAME="([^"]+)"', osr)
    name = m.group(1) if m else "unknown OS"
    if "bookworm" in osr or "trixie" in osr:
        ok(name)
    else:
        warn(f"{name} - tested for Raspberry Pi OS Bookworm (or newer)")
    if sys.version_info >= (3, 10):
        ok(f"Python {sys.version.split()[0]}")
    else:
        fail(f"Python {sys.version.split()[0]} - 3.10 or newer needed")
    if os.geteuid() == 0:
        warn("running as root - run the station as your normal user (in the gpio group)")
    groups = run(["id", "-nG"])[1].split()
    if "gpio" in groups or os.geteuid() == 0:
        ok("user is in the gpio group")
    else:
        fail("user is not in the gpio group", f"sudo usermod -aG gpio {os.environ.get('USER', '$USER')} (then log out/in)")


def check_packages():
    section("2. Software")
    for mod, apt, why in (("flask", "python3-flask", "dashboard"),
                          ("numpy", "python3-numpy", "image processing"),
                          ("cv2", "python3-opencv", "image processing"),
                          ("gpiozero", "python3-gpiozero", "sensor interrupts")):
        try:
            m = __import__(mod)
            try:
                from importlib.metadata import version
                ver = version({"cv2": "opencv-python", "gpiozero": "gpiozero"}.get(mod, mod))
            except Exception:
                ver = getattr(m, "__version__", "") if mod != "flask" else ""
            ok(f"{mod} {ver}  ({why})")
        except Exception as e:
            fail(f"{mod} missing ({why}): {e}", f"sudo apt install {apt}   (or run: sudo bash pi_setup.sh)")
    try:
        import gi
        gi.require_version("Aravis", "0.8")
        from gi.repository import Aravis
        try:
            ver = f" {Aravis.get_major_version()}.{Aravis.get_minor_version()}.{Aravis.get_micro_version()}"
        except Exception:
            ver = ""
        ok(f"Aravis{ver} (Genie Nano)")
    except Exception as e:
        fail(f"Aravis missing - the Genie Nano can't stream: {e}",
             "sudo apt install gir1.2-aravis-0.8 python3-gi aravis-tools")
    for tool, fix in (("pinctrl", "comes with Raspberry Pi OS (raspi-utils)"), ("vcgencmd", "sudo apt install raspi-utils")):
        if shutil.which(tool):
            ok(f"{tool} found")
        else:
            fail(f"{tool} not found", fix)
    if shutil.which("chromium-browser") or shutil.which("chromium"):
        ok("Chromium found (station screen / kiosk)")
    else:
        warn("Chromium not found - needed only for a screen on the Pi", "sudo apt install chromium")
    try:
        from inspection_station.edition import EDITION
        ok(f"station edition: {EDITION}")
        if EDITION == "full":
            try:
                import ultralytics  # noqa: F401
                ok("ultralytics installed (YOLO)")
            except Exception:
                warn("YOLO edition, but ultralytics is not installed - fine unless you select the YOLO engine",
                     "pip install ultralytics ncnn  (in a venv made with --system-site-packages)")
    except Exception as e:
        fail(f"cannot import the station code ({e}) - run this from the inspection-station folder")


def check_power_heat():
    section("3. Power and heat")
    rc, out = run(["vcgencmd", "get_throttled"])
    if rc == 0 and "=" in out:
        f = int(out.split("=")[1], 16)
        if f & 0x1:
            fail("under-voltage RIGHT NOW", "use the official Raspberry Pi 27 W (5 V 5 A) USB-C supply")
        elif f & 0x10000:
            warn("under-voltage happened since boot", "use the official 27 W supply and a short USB-C cable")
        else:
            ok("power supply OK (no under-voltage since boot)")
        if f & 0x40000 or f & 0x4:
            warn("the CPU has been throttled since boot (heat or power)", "fit the Active Cooler; check the supply")
    mc = Path("/proc/device-tree/chosen/power/max_current")
    if mc.exists():
        try:
            ma = int.from_bytes(mc.read_bytes()[:4], "big")
            if ma >= 5000:
                ok(f"supply reports {ma / 1000:.0f} A (official 27 W supply)")
            else:
                warn(f"supply reports only {ma / 1000:.1f} A - USB ports are limited and heavy load can brown out",
                     "use the official 27 W (5 V 5 A) supply")
        except Exception:
            pass
    t = read("/sys/class/thermal/thermal_zone0/temp")
    if t:
        c = int(t) / 1000
        (ok if c < 65 else warn)(f"CPU temperature {c:.0f} °C at idle"
                                 + ("" if c < 65 else " - high for idle: check airflow / cooler"))
    fans = list(Path("/sys/devices/platform/cooling_fan").glob("hwmon/hwmon*/fan1_input"))
    rpm = read(fans[0]) if fans else None
    if rpm and rpm.isdigit() and int(rpm) > 0:
        ok(f"Active Cooler fan spinning ({rpm} rpm)")
    else:
        warn("no fan detected (or it is off because the Pi is cool) - without the Active Cooler use the lite "
             "edition / keep 'Active cooler fitted' off")


def check_gpio():
    section("4. GPIO, relays and boot safety")
    from inspection_station.config import LIGHT_PINS, MOTOR_PINS, REJECT_PIN, SENSOR_PIN, check_pin_conflicts
    try:
        check_pin_conflicts()
        ok(f"pins: green {LIGHT_PINS['green']}, yellow {LIGHT_PINS['yellow']}, red {LIGHT_PINS['red']}, "
           f"reject {REJECT_PIN}, sensor {SENSOR_PIN} - no duplicates")
    except SystemExit as e:
        fail(str(e))
    outs = sorted([*LIGHT_PINS.values(), REJECT_PIN])
    cfg = boot_config()
    text = cfg.read_text() if cfg else ""
    want = f"gpio={','.join(map(str, outs))}=op,dh"
    if re.search(r"^\s*gpio=([\d,\-]+)=op,dh", text, re.M) and all(
            str(p) in ",".join(re.findall(r"^\s*gpio=([\d,\-]+)=op,dh", text, re.M)) for p in outs):
        ok("relay pins are driven HIGH (= relays off) from the first second of boot")
    else:
        fail("relay pins float during boot - an active-low relay board can click ON (air valve!) "
             "until the station starts", f"add '{want}' to {cfg or '/boot/firmware/config.txt'} "
                                         "(or run: sudo bash pi_setup.sh), then reboot")
    risky = [l.strip() for l in text.splitlines()
             if l.strip().startswith("dtoverlay=") and re.search(r"w1-gpio.*gpiopin=(5|6|23|25|26)\b|"
                                                                 r"gpio-fan|pwm.*pin=(5|6|23|25|26)\b", l)]
    if risky:
        fail(f"config.txt enables something on the station's pins: {risky}", "remove those lines and reboot")
    else:
        ok("no device-tree overlay uses the station's pins")
    if not shutil.which("pinctrl"):
        return
    levels = {}
    for p in outs + [SENSOR_PIN]:
        rc, out = run(["pinctrl", "get", str(p)])
        levels[p] = out
    for p in outs:
        o = levels[p]
        off, on = ("relay off", "relay ON")
        if "op" in o and "hi" in o:
            ok(f"GPIO {p}: output HIGH ({off})")
        elif "op" in o and "lo" in o:
            warn(f"GPIO {p}: output LOW = {on} right now (station running?)")
        else:
            warn(f"GPIO {p}: not set up yet ({o.split('//')[0].strip()}) - the station sets it at start; "
                 "the config.txt line above makes it safe during boot")
    en = sorted(m["en"] for m in MOTOR_PINS.values())
    if re.search(r"^\s*gpio=" + ",".join(map(str, en)) + r"=op,dl", text, re.M):
        ok(f"motor drivers held off during boot (EN {', '.join(map(str, en))} low)")
    else:
        warn(f"motor driver EN pins {en} not set at boot - a driver can power its motor until the station "
             "starts", f"add 'gpio={','.join(map(str, en))}=op,dl' to config.txt (sudo bash pi_setup.sh)")
    s = levels[SENSOR_PIN]
    ok(f"sensor GPIO {SENSOR_PIN} now reads {'LOW' if '| lo' in s else 'HIGH' if '| hi' in s else '?'} "
       f"({'beam blocked' if '| lo' in s else 'clear'} for an active-low sensor)")
    t0 = time.perf_counter()
    for _ in range(10):
        run(["pinctrl", "get", str(SENSOR_PIN)])
    ms = (time.perf_counter() - t0) * 100
    (ok if ms < 15 else warn)(f"one relay switch takes about {ms:.1f} ms" +
                               ("" if ms < 15 else " - slow: air pulses will be less exact; avoid other heavy load"))


def check_sensor(seconds=15):
    section(f"4b. Trigger sensor live test ({seconds} s)")
    from inspection_station.config import SENSOR_PIN
    if not shutil.which("pinctrl"):
        warn("pinctrl missing - skipped")
        return
    print(f"    Block the sensor beam with your hand / a can a few times now...")
    last, edges, t_end = None, 0, time.monotonic() + seconds
    while time.monotonic() < t_end:
        out = run(["pinctrl", "get", str(SENSOR_PIN)])[1]
        lvl = "lo" if "| lo" in out else "hi" if "| hi" in out else None
        if last is not None and lvl != last:
            edges += 1
            print(f"    {'BLOCKED' if lvl == 'lo' else 'clear'}")
        last = lvl
        time.sleep(0.005)
    if edges >= 2:
        ok(f"sensor works ({edges // 2} block(s) seen)")
    else:
        fail("no change seen on the sensor", "check 5 V / GND to the sensor, the divider output (≤ 3.3 V) to "
                                            "header pin 22, and the sensor's own LED when blocked")


def check_outputs():
    section("4c. Relay click test")
    from inspection_station.config import LIGHT_PINS, REJECT_PIN
    if not shutil.which("pinctrl"):
        warn("pinctrl missing - skipped")
        return
    for name, pin in LIGHT_PINS.items():
        print(f"    {name} lamp ON for 1 s (relay on GPIO {pin})")
        run(["pinctrl", "set", str(pin), "op", "dl"])
        time.sleep(1)
        run(["pinctrl", "set", str(pin), "op", "dh"])
        time.sleep(0.3)
    input("    Air test next: keep hands clear of the nozzle, then press Enter (Ctrl+C to skip)...")
    run(["pinctrl", "set", str(REJECT_PIN), "op", "dl"])
    time.sleep(0.05)
    run(["pinctrl", "set", str(REJECT_PIN), "op", "dh"])
    ok("done - you should have seen green, yellow, red, and heard one short air pulse")


def check_network():
    section("5. Camera network")
    eth = Path("/sys/class/net/eth0")
    if not eth.exists():
        fail("no eth0 network port found")
        return
    carrier = read(eth / "carrier")
    if carrier == "1":
        sp = read(eth / "speed") or "?"
        ok(f"eth0 cable connected, link {sp} Mb/s")
        if sp.isdigit() and int(sp) < 1000:
            warn("link below 1000 Mb/s - check the cable (Cat5e/Cat6) and PoE injector (gigabit model)")
    else:
        fail("eth0 has no link", "camera powered? (PoE injector / 12-24 V) cable plugged into the Pi's Ethernet port?")
    from inspection_station.gige import discover
    r = discover()
    eth_if = [i for i in r["interfaces"] if i["name"] == "eth0"]
    if eth_if:
        ok(f"eth0 address {eth_if[0]['ip']}/{eth_if[0]['mask']}")
    else:
        fail("eth0 has no IPv4 address - with no DHCP server NetworkManager gives up",
             "sudo bash pi_setup.sh  (sets eth0 to 169.254.100.1/16 for the camera)")
    rc, out = run(["nmcli", "-t", "-f", "NAME,DEVICE,TYPE", "con", "show", "--active"])
    if rc == 0 and ":eth0:" in out:
        name = [l.split(":")[0] for l in out.splitlines() if ":eth0:" in l][0]
        _, meth = run(["nmcli", "-g", "ipv4.method,ipv4.never-default", "con", "show", name])
        ok(f"NetworkManager '{name}': {meth.replace(chr(10), ', never-default ')}")
    cams = r["cameras"]
    if not cams:
        fail("no GigE camera answered", "LED steady blue? power (PoE) on? same cable that worked on the laptop?")
        return None
    for c in cams:
        line = f"{c['manufacturer']} {c['model']} S/N {c['serial']} at {c['ip']} ({c['ip_mode']})"
        if c["reachable_via"]:
            ok(line + f" - same network as {c['reachable_via']['name'] or c['reachable_via']['ip']}")
        else:
            fail(line + " - on a DIFFERENT network than the Pi",
                 f"python3 find_camera.py --force-ip {c['suggested_ip']['ip']} --mask {c['suggested_ip']['mask']}"
                 if c.get("suggested_ip") else "put eth0 on the camera's subnet (pi_setup.sh)")
    try:
        cfg = json.loads((HERE / "config.json").read_text())
        ip = cfg.get("camera_ip")
        if ip in [c["ip"] for c in cams]:
            ok(f"config.json camera IP {ip} matches")
        else:
            warn(f"config.json camera IP is {ip}, camera is at {cams[0]['ip']} - the station switches to the only "
                 "camera found automatically, or set it in Settings > Camera")
    except (OSError, ValueError):
        warn("no config.json yet - created at first start (camera found automatically)")
    rm = read("/proc/sys/net/core/rmem_max")
    if rm and int(rm) >= 8 * 2**20:
        ok(f"network receive buffer {int(rm) // 2**20} MB")
    else:
        warn(f"network receive buffer only {int(rm or 0) // 1024} KB - video frames can arrive incomplete under load",
             "sudo bash pi_setup.sh  (sets net.core.rmem_max/rmem_default to 32 MB)")
    return cams


def check_stream(seconds=8):
    section(f"6. Camera video test ({seconds} s)")
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", 8080)) == 0:
            warn("the station is running and owns the camera - stop it and run this check again for the video test")
            return
    try:
        import gi
        gi.require_version("Aravis", "0.8")
        from gi.repository import Aravis as Ar
        from inspection_station.camera import disable_turbodrive, frame_bytes
    except Exception as e:
        fail(f"cannot test video: {e}")
        return
    try:
        Ar.update_device_list()
        if Ar.get_n_devices() == 0:
            fail("Aravis sees no camera")
            return
        cam = Ar.Camera.new(Ar.get_device_id(0))
        cam.gv_auto_packet_size()
        ps = cam.gv_get_packet_size()
        turbo = disable_turbodrive(cam)
        for f in ("BayerRG8", "BayerBG8", "Mono8"):
            try:
                cam.set_pixel_format_from_string(f)
                break
            except Exception:
                pass
        fmt = cam.get_pixel_format_as_string()
        try:
            cam.set_frame_rate(20.0)
        except Exception:
            pass
        cam.set_acquisition_mode(Ar.AcquisitionMode.CONTINUOUS)
        stream = cam.create_stream(None, None)
        try:
            stream.set_property("packet-resend", Ar.GvStreamPacketResend.ALWAYS)
        except Exception:
            pass
        payload = cam.get_payload()
        for _ in range(10):
            stream.push_buffer(Ar.Buffer.new_allocate(payload))
        cam.start_acquisition()
        good = bad = short = 0
        w = h = 0
        t_end = time.monotonic() + seconds
        while time.monotonic() < t_end:
            buf = stream.timeout_pop_buffer(1_000_000)
            if buf is None:
                continue
            if buf.get_status() == Ar.BufferStatus.SUCCESS:
                w, h = buf.get_image_width(), buf.get_image_height()
                if len(buf.get_data()) < frame_bytes(w, h, fmt):
                    short += 1
                else:
                    good += 1
            else:
                bad += 1
            stream.push_buffer(buf)
        cam.stop_acquisition()
        temp = None
        try:
            temp = cam.get_float("DeviceTemperature")
        except Exception:
            pass
        info = f"{cam.get_model_name()} {w}x{h} {fmt}, packet size {ps}" + (f", {turbo}" if turbo else "")
        fps = good / seconds
        if good and not bad and not short:
            ok(f"{fps:.1f} complete frames/s, none lost - {info}")
        elif good:
            warn(f"{fps:.1f} complete frames/s, {bad + short} incomplete - {info}",
                 "set the network buffer (pi_setup.sh); lower Settings > Camera frame rate; check the cable")
        else:
            fail(f"no complete frames ({bad} failed, {short} cut short) - {info}",
                 "pi_setup.sh network buffer; try a different cable / PoE injector")
        if temp is not None:
            (ok if temp < 60 else warn)(f"camera temperature {temp:.0f} °C")
    except Exception as e:
        fail(f"video test failed: {e}")


def check_storage_app():
    section("7. Storage, time and dashboard")
    du = shutil.disk_usage(HERE)
    free = du.free / 2**30
    (ok if free > 4 else warn if free > 1 else fail)(f"{free:.1f} GB free of {du.total / 2**30:.0f} GB")
    root = run(["findmnt", "-no", "SOURCE", "/"])[1]
    if "mmcblk" in root:
        warn("running from the SD card - fine for testing; for all-day image saving a USB SSD lasts longer")
    else:
        ok(f"system disk: {root or '?'}")
    try:
        (HERE / "data").mkdir(exist_ok=True)
        t = HERE / "data" / ".write_test"
        t.write_text("x")
        t.unlink()
        ok("data folder is writable")
    except OSError as e:
        fail(f"cannot write to data/: {e}", f"sudo chown -R $USER {HERE}")
    rc, out = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    if out == "yes":
        ok("clock synchronised (timestamps in logs and reports are right)")
    else:
        warn("clock not synchronised - log/report times may be wrong",
             "connect the Pi to Wi-Fi with internet once after power-up (the Pi 5 RTC keeps time only with "
             "its optional battery)")
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", 8080)) == 0:
            ok("the station is running on port 8080")
        else:
            ok("port 8080 free (station not running)")
    unit = Path("/etc/systemd/system/can-inspection.service")
    if unit.exists():
        _, st = run(["systemctl", "is-enabled", "can-inspection"])
        ok(f"starts automatically at boot (can-inspection.service: {st})")
    else:
        warn("does not start automatically at boot", "sudo bash pi_setup.sh  (optional autostart step)")
    if run(["sh", "-c", "grep -l connected /sys/class/drm/*HDMI*/status"])[0] == 0:
        ok("a screen is connected (HDMI)")


def main():
    ap = argparse.ArgumentParser(description="Raspberry Pi readiness check for the can inspection station")
    ap.add_argument("--sensor", action="store_true", help="live test: block the trigger sensor within 15 s")
    ap.add_argument("--outputs", action="store_true", help="click each relay, then one short air pulse")
    a = ap.parse_args()
    print(f"{B}Can inspection station - Raspberry Pi check{X}  ({HERE})")
    for fn in (check_system, check_packages, check_power_heat, check_gpio):
        try:
            fn()
        except Exception as e:
            fail(f"{fn.__name__} crashed: {e}")
    if a.sensor:
        check_sensor()
    if a.outputs:
        try:
            check_outputs()
        except KeyboardInterrupt:
            print("\n    air test skipped")
    try:
        if check_network():
            check_stream()
    except Exception as e:
        fail(f"network check crashed: {e}")
    try:
        check_storage_app()
    except Exception as e:
        fail(f"storage check crashed: {e}")
    print(f"\n{B}Result:{X} {G}{results['ok']} OK{X}, {Y}{results['warn']} warnings{X}, {R}{results['fail']} problems{X}")
    if results["fail"]:
        print("Fix the ✘ items first (most are done by: sudo bash pi_setup.sh), then run this again.")
    else:
        print("Ready. Next: python3 pi_check.py --sensor --outputs  for the live sensor / relay test.")
    return 1 if results["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
