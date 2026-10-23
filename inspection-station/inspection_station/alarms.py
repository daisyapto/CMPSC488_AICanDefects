"""
Alarm history with troubleshooting guidance.

Every fault (red lamp) and defect warning (yellow flashing) is recorded with
when it started, when it cleared, how long it lasted and how it was cleared.
History survives restarts: data/alarms.jsonl (append-only; a "start" line when
an alarm is raised and an "end" line when it clears).

GUIDES holds the "what to check" steps shown next to each alarm.
"""
from __future__ import annotations

import json
import threading
from collections import OrderedDict
from datetime import datetime

from .config import DATA_DIR

ALARM_FILE = DATA_DIR / "alarms.jsonl"
KEEP = 500

# key -> (title, severity, steps). Keys match Controller.faults() plus "defect-warning".
GUIDES = {
    "estop": ("Emergency stop", "fault", [
        "Find out why it was pressed and make the line safe.",
        "Clear any jammed or fallen cans.",
        "Press Reset, then Start.",
    ]),
    "escape": ("Defective can not rejected", "fault", [
        "A can the station called defective went past the air nozzle. Find it and remove it by hand.",
        "\"air too late\": the decision came after the can passed the nozzle. Check Diagnostics > Timing: "
        "increase Reject delay, or move the nozzle further down the line, or make inspection faster.",
        "\"rejector locked\": the valve was locked out (E-stop or single-shot latch) - press Reset.",
        "\"reject off\": Settings > Reject > Reject enabled was off.",
        "Press Reset when the can has been removed.",
    ]),
    "sensor": ("Trigger sensor problem", "fault", [
        "Check the sensor has power (its LED) - 5V from header pin 2, GND pin 9.",
        "Check the signal wire to header pin 18 (GPIO24) and the resistor divider (5V -> 3.3V).",
        "Check the sensor is aligned with its reflector / the cans, and the lens is clean.",
        "Diagnostics > Sensor shows the live signal: block the beam by hand and watch it change.",
    ]),
    "camera": ("Camera problem", "fault", [
        "Genie Nano: is the power / status LED on? (PoE injector or 12-24V supply).",
        "Check the Ethernet cable at both ends.",
        "The Pi's Ethernet must have a static IP on the camera's subnet (README > Camera).",
        "Check Settings > Camera > Camera IP, then Diagnostics > Camera > Check camera.",
        "Outside the app: arv-tool-0.8 should list the camera.",
    ]),
    "inspect": ("Inspection failing", "fault", [
        "Every can is being rejected as a fail-safe until this is fixed.",
        "Read the error under Settings > Detection > Detection engine.",
        "Team engine: fix team_vision.py (test it with: python team_vision.py images/).",
        "YOLO engine: check the model file path and that ultralytics is installed.",
        "To keep the line running meanwhile, switch the engine to Built-in.",
    ]),
    "reject-off": ("Reject turned off", "fault", [
        "Defective cans pass through while reject is off.",
        "Turn it on in Settings > Reject > Reject enabled.",
    ]),
    "locked": ("Reject valve locked out", "fault", [
        "Caused by E-stop or by the single-shot latch (air testing).",
        "Press Reset to re-arm. Turn off Settings > Reject > Single-shot latch for production.",
    ]),
    "cpu-hot": ("Raspberry Pi hot", "fault", [
        "The station has reduced its load (live detection off, lower frame rate).",
        "Fit the Raspberry Pi 5 Active Cooler, or improve airflow around the case.",
        "Lower Settings > Heat & power > Camera frame rate.",
        "Without a cooler, use the lite edition (no YOLO) or turn off 'Active cooler fitted'.",
    ]),
    "overheat": ("Overheated - inspection stopped", "fault", [
        "The Pi reached a critical temperature, so inspection was stopped (results would be unreliable).",
        "Let it cool down, fix the cooling (see 'Raspberry Pi hot'), then press Reset.",
    ]),
    "power": ("Under-voltage", "fault", [
        "Use the official Raspberry Pi 27 W (5V 5A) USB-C power supply.",
        "Use a short, thick USB-C cable; avoid powering the relay board from the Pi if it draws a lot.",
    ]),
    "cam-hot": ("Camera hot", "fault", [
        "Mount the camera on metal (it uses its body as a heat sink) and keep air moving around it.",
        "Lower Settings > Heat & power > Camera frame rate; use standby when the line is stopped.",
    ]),
    "disk": ("Storage almost full", "fault", [
        "Inspection images fill the SD card / SSD.",
        "Lower Diagnostics > System > Keep images to fewer days, or set Save images to 'Fails'.",
        "Copy old data/images folders to a laptop, then delete them on the Pi.",
    ]),
    "defect-warning": ("Unusual defect activity", "warning", [
        "Many defects in a short time - usually a real upstream problem: check the cans coming in.",
        "If the cans look fine, it's false rejects: check the lighting hasn't changed and the camera "
        "hasn't moved; look at the latest images on the Review page.",
        "A changed can size needs its own recipe (Line control > Recipe).",
        "Press Acknowledge when handled.",
    ]),
}
DEFAULT_GUIDE = ("Fault", "fault", ["Read the message, fix the cause, then press Reset if it stays red."])


def guide(key: str) -> dict:
    title, severity, steps = GUIDES.get(key, DEFAULT_GUIDE)
    return {"title": title, "severity": severity, "steps": steps}


class AlarmLog:
    def __init__(self, post=None):
        self._post = post                     # controller.post - tells the web UI something changed
        self._lock = threading.Lock()
        self._items: OrderedDict[int, dict] = OrderedDict()
        self._open: dict[str, int] = {}       # key -> id of the alarm that is currently active
        self._next = 1
        self.version = 0                      # bumps on every change (UI refetches)
        self._load()

    # ------------------------------------------------------------------ disk
    def _load(self):
        if not ALARM_FILE.exists():
            return
        try:
            for line in ALARM_FILE.read_text().splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("ev") == "start":
                    self._items[e["id"]] = {k: e[k] for k in ("id", "key", "msg", "start")} | \
                        {"end": None, "cleared": ""}
                elif e.get("ev") == "end" and e.get("id") in self._items:
                    self._items[e["id"]].update(end=e["end"], cleared=e.get("cleared", ""))
                self._next = max(self._next, int(e.get("id", 0)) + 1)
        except OSError:
            return
        # anything still open from before a restart ended when the station stopped
        for a in self._items.values():
            if a["end"] is None:
                a["end"], a["cleared"] = a["start"], "station restarted"
        while len(self._items) > KEEP:
            self._items.popitem(last=False)
        self._compact()

    def _append(self, entry: dict):
        try:
            DATA_DIR.mkdir(exist_ok=True)
            with ALARM_FILE.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass                                  # never stop the line over a log file

    def _compact(self):
        """Rewrite the file with only the kept alarms (keeps it small)."""
        try:
            lines = []
            for a in self._items.values():
                lines.append(json.dumps({"ev": "start", "id": a["id"], "key": a["key"],
                                         "msg": a["msg"], "start": a["start"]}))
                if a["end"]:
                    lines.append(json.dumps({"ev": "end", "id": a["id"], "end": a["end"],
                                             "cleared": a["cleared"]}))
            DATA_DIR.mkdir(exist_ok=True)
            tmp = ALARM_FILE.with_suffix(".tmp")
            tmp.write_text("\n".join(lines) + ("\n" if lines else ""))
            tmp.replace(ALARM_FILE)
        except OSError:
            pass

    # ---------------------------------------------------------------- events
    def raised(self, key: str, msg: str):
        with self._lock:
            if key in self._open:
                return
            aid = self._next
            self._next += 1
            a = {"id": aid, "key": key, "msg": msg,
                 "start": datetime.now().isoformat(timespec="seconds"), "end": None, "cleared": ""}
            self._items[aid] = a
            self._open[key] = aid
            if len(self._items) > KEEP * 1.2:
                while len(self._items) > KEEP:
                    self._items.popitem(last=False)
                self._compact()
            else:
                self._append({"ev": "start", "id": aid, "key": key, "msg": msg, "start": a["start"]})
            self.version += 1
        self._notify()

    def cleared(self, key: str, how: str = "fixed"):
        with self._lock:
            aid = self._open.pop(key, None)
            if aid is None or aid not in self._items:
                return
            a = self._items[aid]
            a["end"] = datetime.now().isoformat(timespec="seconds")
            a["cleared"] = how
            self._append({"ev": "end", "id": aid, "end": a["end"], "cleared": how})
            self.version += 1
        self._notify()

    def _notify(self):
        if self._post:
            self._post("alarms", self.version)

    # ----------------------------------------------------------------- query
    @staticmethod
    def _view(a: dict) -> dict:
        start = datetime.fromisoformat(a["start"])
        end = datetime.fromisoformat(a["end"]) if a["end"] else datetime.now()
        return a | {"active": a["end"] is None, "duration_s": max(0, (end - start).total_seconds())} | guide(a["key"])

    def snapshot(self, limit: int = 200) -> dict:
        with self._lock:
            items = [self._view(a) for a in reversed(self._items.values())]
        active = [a for a in items if a["active"]]
        history = [a for a in items if not a["active"]][:limit]
        today = datetime.now().date().isoformat()
        counts: dict[str, int] = {}
        for a in items:
            if a["start"].startswith(today):
                counts[a["title"]] = counts.get(a["title"], 0) + 1
        return {"active": active, "history": history, "version": self.version,
                "today": sorted(counts.items(), key=lambda kv: -kv[1])}
