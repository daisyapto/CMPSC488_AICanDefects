"""
Recipes: one saved set of settings per can size / product.

A recipe holds the settings that change with the can (timing, camera, detection,
air burst) plus its own taught good-can reference image. Selecting a recipe
applies all of them at once, so a changeover is one click instead of re-tuning.

Stored in recipes.json (settings) and data/recipes/<name>.png (reference images).
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime

from .config import APP_DIR, DATA_DIR

RECIPES_FILE = APP_DIR / "recipes.json"
REF_DIR = DATA_DIR / "recipes"

RECIPE_FIELDS = {
    "camera_delay_ms": "Capture delay",
    "reject_delay_ms": "Reject delay",
    "exposure_us": "Exposure",
    "gain_db": "Gain",
    "vision_sensitivity": "Sensitivity",
    "vision_min_area": "Min defect size",
    "pulses": "Air pulses",
    "pulse_on_ms": "Pulse on",
    "pulse_off_ms": "Pulse off",
}
NAME_RE = re.compile(r"^[\w .,/()+-]{1,40}$")


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "recipe"


class RecipeBook:
    def __init__(self, settings, log=None):
        self.s = settings
        self.log = log or (lambda *a: None)
        self.lock = threading.Lock()
        self.recipes: dict[str, dict] = {}
        self.active = ""
        self._load()
        if not self.recipes:
            self.recipes["Default"] = self._new_record("Starting point - copy it for each can size")
            self.active = "Default"
            self.save()
        if self.active not in self.recipes:
            self.active = next(iter(self.recipes))

    # ------------------------------------------------------------- storage
    def _load(self):
        try:
            data = json.loads(RECIPES_FILE.read_text())
            self.recipes = {k: v for k, v in data.get("recipes", {}).items() if isinstance(v, dict)}
            self.active = data.get("active", "")
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            self.log("WARN", f"recipes.json could not be read ({e}) - starting with a default recipe")

    def save(self):
        with self.lock:
            tmp = RECIPES_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"active": self.active, "recipes": self.recipes}, indent=2))
            os.replace(tmp, RECIPES_FILE)

    def _new_record(self, description=""):
        return {"description": description, "values": self.current_values(),
                "reference": None, "updated": _now()}

    # --------------------------------------------------------------- query
    def current_values(self) -> dict:
        return {f: getattr(self.s, f) for f in RECIPE_FIELDS}

    def modified(self) -> list[str]:
        """Labels of settings that differ from the active recipe."""
        rec = self.recipes.get(self.active)
        if not rec:
            return []
        vals = rec["values"]
        return [label for f, label in RECIPE_FIELDS.items()
                if f in vals and vals[f] != getattr(self.s, f)]

    def summary(self) -> dict:
        return {
            "active": self.active,
            "modified": self.modified(),
            "list": [{"name": n, "description": r.get("description", ""),
                      "has_reference": bool(r.get("reference")), "updated": r.get("updated", "")}
                     for n, r in self.recipes.items()],
        }

    # -------------------------------------------------------------- changes
    def activate(self, name: str, vision=None):
        if name not in self.recipes:
            raise ValueError(f"unknown recipe '{name}'")
        for f, v in self.recipes[name]["values"].items():
            if f in RECIPE_FIELDS:
                setattr(self.s, f, v)
        self.active = name
        self.save()
        if vision is not None:
            self.load_reference(vision)

    def save_current(self):
        rec = self.recipes[self.active]
        rec["values"] = self.current_values()
        rec["updated"] = _now()
        self.save()

    def create(self, name: str, description: str = "", copy_reference_from: str | None = None):
        name = name.strip()
        if not NAME_RE.match(name):
            raise ValueError("name: 1-40 letters, numbers, spaces or . , / ( ) + -")
        if name in self.recipes:
            raise ValueError(f"a recipe called '{name}' already exists")
        rec = self._new_record(description.strip()[:120])
        src = self.recipes.get(copy_reference_from or "")
        if src and src.get("reference"):
            old = REF_DIR / src["reference"]["file"]
            if old.is_file():
                new_file = f"{_slug(name)}.png"
                (REF_DIR / new_file).write_bytes(old.read_bytes())
                rec["reference"] = dict(src["reference"], file=new_file)
        self.recipes[name] = rec
        self.save()

    def delete(self, name: str):
        if name not in self.recipes:
            raise ValueError("unknown recipe")
        if name == self.active:
            raise ValueError("cannot delete the active recipe - select another one first")
        ref = self.recipes[name].get("reference")
        del self.recipes[name]
        if ref:
            try:
                (REF_DIR / ref["file"]).unlink()
            except OSError:
                pass
        self.save()

    # ------------------------------------------------------ reference image
    def set_reference(self, vision):
        """Store the vision module's taught reference in the active recipe."""
        import cv2
        if vision.reference is None:
            return
        blur, circle = vision.reference
        REF_DIR.mkdir(parents=True, exist_ok=True)
        file = f"{_slug(self.active)}.png"
        cv2.imwrite(str(REF_DIR / file), blur)
        self.recipes[self.active]["reference"] = {
            "file": file, "circle": list(circle) if circle else None, "taught": _now()}
        self.save()

    def clear_reference(self):
        rec = self.recipes[self.active]
        ref = rec.get("reference")
        rec["reference"] = None
        self.save()
        if ref and not any(r.get("reference", {}) and r["reference"].get("file") == ref["file"]
                           for r in self.recipes.values()):
            try:
                (REF_DIR / ref["file"]).unlink()
            except OSError:
                pass

    def load_reference(self, vision):
        """Put the active recipe's reference (or none) into the vision module."""
        ref = self.recipes.get(self.active, {}).get("reference")
        if not ref:
            vision.clear_reference()
            return
        import cv2
        img = cv2.imread(str(REF_DIR / ref["file"]), cv2.IMREAD_GRAYSCALE)
        if img is None:
            self.log("WARN", f"Reference image for recipe '{self.active}' is missing - teach it again")
            vision.clear_reference()
            return
        vision.reference = (img, tuple(ref["circle"]) if ref.get("circle") else None)
        try:
            vision.reference_time = datetime.fromisoformat(ref["taught"]).timestamp()
        except (KeyError, ValueError):
            vision.reference_time = None
