"""
Batches (production lots / shifts).

The operator starts a batch with a lot number; every can inspected until the
batch ends is tagged with it. A batch keeps running totals, defects by type,
alarms, temperatures and run time, and writes:

    data/batches/<id>.json          summary (also the report's data)
    data/batches/<id>_results.csv   every can in the batch
    data/batches/active.json        pointer to the running batch (survives a restart)
"""
from __future__ import annotations

import csv
import json
import os
import threading
import time
from datetime import datetime

from .config import DATA_DIR

BATCH_DIR = DATA_DIR / "batches"
ACTIVE_FILE = BATCH_DIR / "active.json"
BUCKET_S = 300                       # report chart resolution: 5 minutes
ESCAPED = {"missed", "disabled", "locked"}


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


class BatchManager:
    def __init__(self, log=None):
        self.log = log or (lambda *a: None)
        self.lock = threading.Lock()
        self.active: dict | None = None
        self._dirty_at = 0.0
        self._resume()

    # ------------------------------------------------------------ lifecycle
    def _resume(self):
        try:
            bid = json.loads(ACTIVE_FILE.read_text())["id"]
            self.active = json.loads((BATCH_DIR / f"{bid}.json").read_text())
            self.active.setdefault("events", []).append(
                {"t": _now_iso(), "level": "WARN", "msg": "Station restarted during this batch"})
            self.log("WARN", f"Resumed batch {self.active['lot']} after a restart")
        except (OSError, ValueError, KeyError):
            self.active = None

    @property
    def active_id(self) -> str:
        return self.active["id"] if self.active else ""

    def start(self, lot: str, operator: str, recipe: str, settings: dict, note: str = "") -> dict:
        with self.lock:
            if self.active:
                raise ValueError(f"batch {self.active['lot']} is still running - end it first")
            lot = lot.strip()
            if not lot or len(lot) > 40:
                raise ValueError("enter a lot / batch number (max 40 characters)")
            bid = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.active = {
                "id": bid, "lot": lot, "operator": operator.strip()[:40] or "-", "recipe": recipe,
                "note": note.strip()[:200], "start": _now_iso(), "end": None,
                "settings": settings, "n": 0, "passed": 0, "failed": 0, "fired": 0, "escaped": 0,
                "ms_total": 0.0, "defects": {}, "buckets": {}, "events": [], "run_s": 0.0,
                "cpu_min": None, "cpu_max": None, "cam_min": None, "cam_max": None, "images": 0,
            }
            BATCH_DIR.mkdir(parents=True, exist_ok=True)
            self._save_locked()
            ACTIVE_FILE.write_text(json.dumps({"id": bid}))
            return self.active

    def end(self) -> dict:
        with self.lock:
            if not self.active:
                raise ValueError("no batch is running")
            self.active["end"] = _now_iso()
            done = self.active
            self._save_locked()
            self.active = None
            try:
                ACTIVE_FILE.unlink()
            except OSError:
                pass
            return done

    # --------------------------------------------------------------- update
    def add_result(self, r):
        """A finished can (reject status final). r is controller.InspectionResult."""
        with self.lock:
            b = self.active
            if not b or r.batch != b["id"]:
                return
            b["n"] += 1
            b["ms_total"] += r.inspect_ms
            key = str(int(r.timestamp.timestamp() // BUCKET_S * BUCKET_S))
            bucket = b["buckets"].setdefault(key, [0, 0])
            bucket[0] += 1
            if r.passed:
                b["passed"] += 1
            else:
                b["failed"] += 1
                bucket[1] += 1
                name = r.defect or "Unknown"
                b["defects"][name] = b["defects"].get(name, 0) + 1
                if r.reject == "fired":
                    b["fired"] += 1
                elif r.reject in ESCAPED:
                    b["escaped"] += 1
            if r.image:
                b["images"] += 1
            path = BATCH_DIR / f"{b['id']}_results.csv"
            try:
                new = not path.exists()
                with path.open("a", newline="") as f:
                    w = csv.writer(f)
                    if new:
                        w.writerow(["can_id", "timestamp", "result", "defect", "confidence",
                                    "inspect_ms", "inspector", "reject", "image"])
                    w.writerow([r.can_id, r.timestamp.isoformat(timespec="milliseconds"),
                                "PASS" if r.passed else "FAIL", r.defect, f"{r.confidence:.3f}",
                                f"{r.inspect_ms:.1f}", r.inspector, r.reject, r.image])
            except OSError:
                pass
            self._save_soon()

    def event(self, level: str, msg: str):
        with self.lock:
            if self.active and len(self.active["events"]) < 1000:
                self.active["events"].append({"t": _now_iso(), "level": level, "msg": msg})
                self._save_soon()

    def tick(self, running: bool, dt: float, cpu=None, cam=None):
        with self.lock:
            b = self.active
            if not b:
                return
            if running:
                b["run_s"] += dt
            for key, v in (("cpu", cpu), ("cam", cam)):
                if v is not None:
                    lo, hi = b[f"{key}_min"], b[f"{key}_max"]
                    b[f"{key}_min"] = v if lo is None else min(lo, v)
                    b[f"{key}_max"] = v if hi is None else max(hi, v)
            self._save_soon()

    def _save_soon(self):
        if time.monotonic() - self._dirty_at > 5:           # at most every 5 s
            self._dirty_at = time.monotonic()
            self._save_locked()

    def _save_locked(self):
        b = self.active
        if not b:
            return
        try:
            BATCH_DIR.mkdir(parents=True, exist_ok=True)
            tmp = BATCH_DIR / f"{b['id']}.tmp"
            tmp.write_text(json.dumps(b, indent=1))
            os.replace(tmp, BATCH_DIR / f"{b['id']}.json")
        except OSError as e:
            self.log("ERROR", f"Could not save batch: {e}")

    def flush(self):
        with self.lock:
            self._save_locked()

    # ---------------------------------------------------------------- query
    @staticmethod
    def summarize(b: dict) -> dict:
        n = b["n"]
        start = datetime.fromisoformat(b["start"])
        end = datetime.fromisoformat(b["end"]) if b.get("end") else datetime.now()
        return {"id": b["id"], "lot": b["lot"], "operator": b["operator"], "recipe": b["recipe"],
                "start": b["start"], "end": b.get("end"), "n": n, "passed": b["passed"],
                "failed": b["failed"], "escaped": b["escaped"],
                "yield": (b["passed"] / n * 100) if n else None,
                "duration_s": (end - start).total_seconds(), "active": not b.get("end")}

    def status(self):
        with self.lock:
            return self.summarize(self.active) if self.active else None

    def list(self) -> list[dict]:
        out = []
        if BATCH_DIR.exists():
            for p in BATCH_DIR.glob("*.json"):
                if p.name == "active.json":
                    continue
                try:
                    out.append(self.summarize(json.loads(p.read_text())))
                except (OSError, ValueError, KeyError):
                    continue
        if self.active:                                     # live numbers for the running one
            out = [x for x in out if x["id"] != self.active["id"]] + [self.status()]
        return sorted(out, key=lambda x: x["start"], reverse=True)

    def get(self, bid: str) -> dict | None:
        if self.active and self.active["id"] == bid:
            with self.lock:
                return json.loads(json.dumps(self.active))
        if not bid.replace("-", "").isdigit():
            return None
        try:
            return json.loads((BATCH_DIR / f"{bid}.json").read_text())
        except (OSError, ValueError):
            return None

    @staticmethod
    def results_csv(bid: str):
        p = BATCH_DIR / f"{bid}_results.csv"
        return p if bid.replace("-", "").isdigit() and p.is_file() else None
