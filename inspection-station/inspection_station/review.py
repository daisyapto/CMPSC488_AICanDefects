"""
Defect review: label saved images as right or wrong decisions.

  * Every saved image (data/images/<date>/<pass|fail>/*.jpg) can be reviewed.
  * A FAIL image is either a real defect (decision correct) or a false reject.
    A PASS image is either a good can (correct) or a missed defect.
  * Labels go to data/review_labels.jsonl (append-only; the latest label wins).
  * export_dataset() builds a ZIP sorted into class folders (good, dent, ...)
    for training the AI model.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import threading
import zipfile
import zlib
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR

IMAGES = DATA_DIR / "images"
LABELS_FILE = DATA_DIR / "review_labels.jsonl"
THUMBS = DATA_DIR / "thumbs"
EXPORTS = DATA_DIR / "exports"
DEFECT_TYPES = ["Dent", "Scratch", "Contamination", "Flange damage", "Crease / wrinkle",
                "Coating void", "Other"]
NAME_RE = re.compile(r"can(\d+)_(\d{2})(\d{2})(\d{2})(?:_(.+))?\.jpg$")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "defect"


class ReviewStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.labels: dict[str, dict] = {}
        try:
            with LABELS_FILE.open() as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        if rec.get("verdict"):
                            self.labels[rec["path"]] = rec
                        else:
                            self.labels.pop(rec["path"], None)
                    except (ValueError, KeyError):
                        pass
        except OSError:
            pass

    # ---------------------------------------------------------------- index
    def scan(self) -> list[dict]:
        items = []
        if not IMAGES.is_dir():
            return items
        for day in sorted(os.scandir(IMAGES), key=lambda e: e.name, reverse=True):
            if not day.is_dir():
                continue
            for result in ("fail", "pass"):
                folder = Path(day.path) / result
                if not folder.is_dir():
                    continue
                for e in os.scandir(folder):
                    m = NAME_RE.match(e.name)
                    if not m or not e.is_file():
                        continue
                    rel = f"images/{day.name}/{result}/{e.name}"
                    lab = self.labels.get(rel)
                    items.append({
                        "path": rel, "date": day.name, "result": result,
                        "id": int(m.group(1)), "time": f"{m.group(2)}:{m.group(3)}:{m.group(4)}",
                        "detected": (m.group(5) or "").replace("-", " ").capitalize(),
                        "verdict": lab["verdict"] if lab else None,
                        "label": lab.get("defect_type", "") if lab else "",
                        "by": lab.get("by", "") if lab else "",
                    })
        items.sort(key=lambda x: (x["date"], x["time"], x["id"]), reverse=True)
        return items

    def query(self, status="todo", result="fail", date="", offset=0, limit=60) -> dict:
        items = self.scan()
        stats = self.stats(items)
        if date:
            items = [i for i in items if i["date"] == date]
        if result in ("fail", "pass"):
            items = [i for i in items if i["result"] == result]
        if status == "todo":
            items = [i for i in items if not i["verdict"]]
        elif status == "wrong":
            items = [i for i in items if i["verdict"] == "wrong"]
        elif status == "correct":
            items = [i for i in items if i["verdict"] == "correct"]
        dates = sorted({d.name for d in IMAGES.iterdir() if d.is_dir()}, reverse=True) if IMAGES.is_dir() else []
        return {"items": items[offset:offset + limit], "total": len(items), "stats": stats, "dates": dates}

    @staticmethod
    def stats(items) -> dict:
        fails = [i for i in items if i["result"] == "fail"]
        passes = [i for i in items if i["result"] == "pass"]
        fr = [i for i in fails if i["verdict"]]
        pr = [i for i in passes if i["verdict"]]
        false_rej = sum(1 for i in fr if i["verdict"] == "wrong")
        missed = sum(1 for i in pr if i["verdict"] == "wrong")
        return {"images": len(items), "reviewed": len(fr) + len(pr), "todo": len(items) - len(fr) - len(pr),
                "fails_reviewed": len(fr), "false_rejects": false_rej,
                "false_reject_rate": (false_rej / len(fr) * 100) if fr else None,
                "passes_reviewed": len(pr), "missed": missed}

    def stats_for_paths(self, paths: list[str]) -> dict:
        """Review numbers for one batch (paths relative to data/)."""
        items = [i for i in self.scan() if i["path"] in set(paths)]
        return self.stats(items)

    # ---------------------------------------------------------------- label
    def set(self, path: str, verdict: str | None, defect_type: str, who: str) -> dict:
        if verdict not in ("correct", "wrong", None):
            raise ValueError("verdict must be correct, wrong or none")
        if not (DATA_DIR / path).resolve().is_relative_to(IMAGES.resolve()) or not (DATA_DIR / path).is_file():
            raise ValueError("image not found")
        if defect_type and defect_type not in DEFECT_TYPES:
            raise ValueError("unknown defect type")
        rec = {"path": path, "verdict": verdict, "defect_type": defect_type or "",
               "by": who, "t": datetime.now().isoformat(timespec="seconds")}
        with self.lock:
            with LABELS_FILE.open("a") as f:
                f.write(json.dumps(rec) + "\n")
            if verdict:
                self.labels[path] = rec
            else:
                self.labels.pop(path, None)
        return rec

    # ------------------------------------------------------------ thumbnails
    @staticmethod
    def thumbnail(path: str) -> Path | None:
        import cv2
        src = (DATA_DIR / path).resolve()
        if not src.is_relative_to(IMAGES.resolve()) or not src.is_file():
            return None
        annotated = src.parent / "annotated" / src.name
        if annotated.is_file():
            src = annotated
        out = THUMBS / path
        if out.is_file() and out.stat().st_mtime >= src.stat().st_mtime:
            return out
        img = cv2.imread(str(src))
        if img is None:
            return None
        h, w = img.shape[:2]
        s = 320 / w
        img = cv2.resize(img, (320, max(1, int(h * s))), interpolation=cv2.INTER_AREA)
        out.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out), img, [cv2.IMWRITE_JPEG_QUALITY, 82])
        return out

    # ---------------------------------------------------------------- export
    def class_of(self, item) -> str | None:
        v = item["verdict"]
        if not v:
            return None
        if item["result"] == "fail":
            return "good" if v == "wrong" else _slug(item["label"] or item["detected"] or "defect")
        return "good" if v == "correct" else _slug(item["label"] or "defect")

    def export_dataset(self) -> Path:
        items = [i for i in self.scan() if i["verdict"]]
        if not items:
            raise ValueError("no reviewed images yet")
        EXPORTS.mkdir(parents=True, exist_ok=True)
        out = EXPORTS / f"dataset_{datetime.now():%Y%m%d_%H%M%S}.zip"
        rows = io.StringIO()
        w = csv.writer(rows)
        w.writerow(["file", "split", "class", "station_result", "detected", "verdict", "reviewed_by", "source"])
        with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:     # JPEGs are already compressed
            for i in items:
                cls = self.class_of(i).replace(" / ", "_").replace(" ", "_")
                name = f"{i['date']}_{Path(i['path']).name}"
                # stable 80 / 20 split (same image always lands in the same half)
                split = "val" if zlib.crc32(name.encode()) % 5 == 0 else "train"
                z.write(DATA_DIR / i["path"], f"dataset/{split}/{cls}/{name}")
                w.writerow([f"{split}/{cls}/{name}", split, cls, i["result"], i["detected"], i["verdict"],
                            i["by"], i["path"]])
            z.writestr("dataset/labels.csv", rows.getvalue())
            z.writestr("dataset/README.txt",
                       "Can inspection dataset exported from the inspection station.\n"
                       "train/ and val/ (80 / 20) each hold one folder per class (good + defect types),\n"
                       "labelled by a human reviewer. This is the layout YOLO classification expects:\n"
                       "    yolo classify train data=dataset model=yolo11n-cls.pt imgsz=224\n"
                       "For a YOLO DETECTION model, draw boxes on these images first\n"
                       "(Roboflow, CVAT or Label Studio) and export in YOLO format.\n"
                       "labels.csv lists every image with the station's decision and the review.\n")
        return out


def cleanup_images(keep_days: int, log=None) -> int:
    """Delete saved inspection images older than keep_days (by folder date).
    Images a reviewer has labelled are KEPT - they are training data.
    Returns the number of files deleted."""
    if keep_days <= 0 or not IMAGES.is_dir():
        return 0
    from datetime import date, timedelta
    labelled = set(ReviewStore().labels)
    cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
    removed = 0
    for day in os.scandir(IMAGES):
        if not day.is_dir() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day.name) or day.name >= cutoff:
            continue
        root = Path(day.path)
        for f in sorted(root.rglob("*"), reverse=True):        # files first, then empty folders
            rel = f"images/{day.name}/{f.relative_to(root).as_posix()}"
            try:
                if f.is_file():
                    keep = rel in labelled or rel.replace("/annotated/", "/") in labelled
                    if not keep:
                        f.unlink()
                        removed += 1
                        (THUMBS / rel).unlink(missing_ok=True)
                elif f.is_dir() and not any(f.iterdir()):
                    f.rmdir()
            except OSError:
                pass
        try:
            if not any(root.iterdir()):
                root.rmdir()
        except OSError:
            pass
    if removed and log:
        log("INFO", f"Image clean-up: deleted {removed} files older than {keep_days} days (labelled images kept)")
    return removed
