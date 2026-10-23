#!/usr/bin/env python3
"""Build the two editions from this one source tree:

    inspection-station-yolo.zip   full edition: Built-in + Team + YOLO engines
    inspection-station-lite.zip   lite edition: Built-in + Team (no YOLO, no ultralytics)

    python tools/build_editions.py [output folder]     (default: the folder above the project)

Always edit the normal source; the lite edition is generated from it.
"""
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else SRC.parent
SKIP_DIRS = {"__pycache__", "data", ".git"}
SKIP_FILES = {"config.json", "recipes.json", "config.tmp"}

LITE_NOTE = """> **Lite edition** - no YOLO engine (Built-in + Team detection only). Lighter on the
> Pi's CPU and heat; use it while the Pi has no active cooler. The YOLO sections
> below apply to the full edition (inspection-station-yolo.zip).

"""


def copy_tree(dst: Path):
    for p in SRC.rglob("*"):
        rel = p.relative_to(SRC)
        if any(part in SKIP_DIRS for part in rel.parts) or p.name in SKIP_FILES or p.suffix == ".pyc":
            continue
        t = dst / rel
        if p.is_dir():
            t.mkdir(parents=True, exist_ok=True)
        else:
            t.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, t)


def make_lite(root: Path):
    ed = root / "inspection_station" / "edition.py"
    ed.write_text(re.sub(r'^EDITION = ".*"$', 'EDITION = "lite"', ed.read_text(), flags=re.M))
    shutil.rmtree(root / "models", ignore_errors=True)
    shutil.rmtree(root / "tools", ignore_errors=True)          # the build script lives in the full edition
    req = root / "requirements.txt"
    text = req.read_text()
    text = re.sub(r"# Optional - only for the YOLO.*?\n\n", "", text, flags=re.S)
    req.write_text(text)
    readme = root / "README.md"
    lines = readme.read_text().split("\n", 2)              # keep the title line first
    readme.write_text(lines[0] + "\n\n" + LITE_NOTE + "\n".join(lines[1:]))


def zip_dir(root: Path, out: Path):
    out.unlink(missing_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(root.rglob("*")):
            if p.is_file():
                z.write(p, Path("inspection-station") / p.relative_to(root))
    return out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        full, lite = Path(tmp, "full"), Path(tmp, "lite")
        copy_tree(full)
        copy_tree(lite)
        make_lite(lite)
        for name, root in (("inspection-station-yolo.zip", full), ("inspection-station-lite.zip", lite)):
            out = zip_dir(root, OUT / name)
            print(f"{out}  ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
