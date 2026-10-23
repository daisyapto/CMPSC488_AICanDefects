"""
Multi-day history from the daily inspection logs (data/inspections_YYYY-MM-DD.csv).

Each day is summarised once and cached until its file changes, so the History
page stays fast even with months of data.
"""
from __future__ import annotations

import csv
import re
import threading
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from .config import DATA_DIR

FILE_RE = re.compile(r"inspections_(\d{4}-\d{2}-\d{2})(?:_v\d+)?\.csv$")
ESCAPED = {"missed", "disabled", "locked"}


class History:
    def __init__(self):
        self._cache: dict[str, tuple] = {}     # path -> (mtime, size, summary)
        self._lock = threading.Lock()

    def files_by_day(self) -> dict[str, list[Path]]:
        days: dict[str, list[Path]] = {}
        if DATA_DIR.is_dir():
            for p in DATA_DIR.iterdir():
                m = FILE_RE.match(p.name)
                if m:
                    days.setdefault(m.group(1), []).append(p)
        return days

    def _summarise(self, path: Path) -> dict:
        st = path.stat()
        with self._lock:
            hit = self._cache.get(str(path))
            if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
                return hit[2]
        n = passed = escaped = 0
        ms = 0.0
        defects: Counter = Counter()
        hours = [[0, 0] for _ in range(24)]          # [cans, defects] per hour
        try:
            with path.open(newline="") as f:
                for row in csv.DictReader(f):
                    n += 1
                    ok = row.get("result") == "PASS"
                    passed += ok
                    try:
                        ms += float(row.get("inspect_ms") or 0)
                    except ValueError:
                        pass
                    try:
                        h = int(row.get("timestamp", "")[11:13])
                        hours[h][0] += 1
                        hours[h][1] += not ok
                    except ValueError:
                        pass
                    if not ok:
                        defects[row.get("defect") or "Unknown"] += 1
                        if row.get("reject") in ESCAPED:
                            escaped += 1
        except OSError:
            pass
        out = {"n": n, "passed": passed, "failed": n - passed, "escaped": escaped,
               "ms_sum": ms, "defects": dict(defects), "hours": hours}
        with self._lock:
            self._cache[str(path)] = (st.st_mtime, st.st_size, out)
        return out

    def day(self, d: str, files: list[Path]) -> dict:
        tot = {"date": d, "n": 0, "passed": 0, "failed": 0, "escaped": 0, "ms_sum": 0.0,
               "defects": Counter(), "hours": [[0, 0] for _ in range(24)]}
        for p in files:
            s = self._summarise(p)
            for k in ("n", "passed", "failed", "escaped", "ms_sum"):
                tot[k] += s[k]
            tot["defects"].update(s["defects"])
            for h in range(24):
                tot["hours"][h][0] += s["hours"][h][0]
                tot["hours"][h][1] += s["hours"][h][1]
        n = tot["n"]
        tot["yield"] = tot["passed"] / n * 100 if n else None
        tot["avg_ms"] = tot["ms_sum"] / n if n else None
        tot["defects"] = tot["defects"].most_common()
        del tot["ms_sum"]
        return tot

    def summary(self, days: int = 30) -> dict:
        files = self.files_by_day()
        start = (date.today() - timedelta(days=days - 1)).isoformat()
        out = []
        for i in range(days):                       # every day in the range, including empty ones
            d = (date.today() - timedelta(days=days - 1 - i)).isoformat()
            out.append(self.day(d, files.get(d, [])) if d in files else
                       {"date": d, "n": 0, "passed": 0, "failed": 0, "escaped": 0, "yield": None,
                        "avg_ms": None, "defects": [], "hours": [[0, 0] for _ in range(24)]})
        tot = Counter()
        defects: Counter = Counter()
        for d in out:
            for k in ("n", "passed", "failed", "escaped"):
                tot[k] += d[k]
            defects.update(dict(d["defects"]))
        active = [d for d in out if d["n"]]
        best = max(active, key=lambda d: d["yield"]) if active else None
        worst = min(active, key=lambda d: d["yield"]) if active else None
        return {"days": out, "from": start, "to": date.today().isoformat(),
                "total": dict(tot), "yield": tot["passed"] / tot["n"] * 100 if tot["n"] else None,
                "defects": defects.most_common(), "active_days": len(active),
                "best": best and {"date": best["date"], "yield": best["yield"]},
                "worst": worst and {"date": worst["date"], "yield": worst["yield"]},
                "all_dates": sorted(files, reverse=True)}

    def day_files(self, d: str) -> list[Path]:
        return sorted(self.files_by_day().get(d, []))
