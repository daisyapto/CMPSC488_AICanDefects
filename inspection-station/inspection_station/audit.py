"""
Audit trail: who did what, when, from where.

Every command, setting change, login, recipe and batch action is appended to
data/audit.jsonl (one JSON object per line) and shown in the dashboard.
"""
from __future__ import annotations

import json
import threading
from collections import deque
from datetime import datetime

from .config import DATA_DIR

AUDIT_FILE = DATA_DIR / "audit.jsonl"


class Audit:
    def __init__(self, post=None):
        self.post = post or (lambda *a: None)
        self.lock = threading.Lock()
        self.recent: deque = deque(maxlen=300)
        try:
            with AUDIT_FILE.open() as f:
                for line in deque(f, maxlen=300):
                    try:
                        self.recent.append(json.loads(line))
                    except ValueError:
                        pass
        except OSError:
            pass

    def record(self, who: str, role: str, action: str, detail: str = "", source: str = "station"):
        entry = {"t": datetime.now().isoformat(timespec="seconds"), "who": who or "-",
                 "role": role, "action": action, "detail": detail, "source": source}
        with self.lock:
            self.recent.append(entry)
            try:
                DATA_DIR.mkdir(exist_ok=True)
                with AUDIT_FILE.open("a") as f:
                    f.write(json.dumps(entry) + "\n")
            except OSError as e:
                print(f"[audit] cannot write {AUDIT_FILE}: {e}")
        self.post("audit", entry)
        return entry
