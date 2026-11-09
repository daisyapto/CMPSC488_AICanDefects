#!/usr/bin/env python3
"""
reorganize_repo.py - sort CMPSC488_AICanDefects into folders.

Run it from anywhere inside the repo, on an up-to-date main with no uncommitted changes:

    git checkout main
    git pull
    python reorganize_repo.py

It creates a branch `reorganize-repo`, moves files with `git mv` (so their history is kept),
fixes the paths that the moves would break, updates the README, adds a .gitignore and
requirements.txt, and commits. Nothing is pushed: check the result, then
`git push -u origin reorganize-repo` and open a pull request so the team can review it.

Files it doesn't recognise (e.g. something added after this script was written) stay where they
are and are listed at the end.
"""

import re
import subprocess
import sys
from pathlib import Path

BRANCH = "reorganize-repo"

# old path -> new path (folders are moved whole)
MOVES = {
    # integrated station apps (PyQt + YOLO + sensor + stack light)
    "can_inspection_station.py": "station/can_inspection_station.py",
    "can_inspection_station_2.py": "station/can_inspection_station_2.py",
    # hardware + camera bring-up tests
    "StackLight.py": "hardware_tests/StackLight.py",
    "GPIOTest.py": "hardware_tests/GPIOTest.py",
    "AirTest.py": "hardware_tests/AirTest.py",
    "SensorToCam.py": "hardware_tests/SensorToCam.py",
    "module2.py": "hardware_tests/module2.py",
    # OpenCV (classic vision) detection
    "edgeDetection.py": "vision_opencv/edgeDetection.py",
    "edgeFaultDetection.py": "vision_opencv/edgeFaultDetection.py",
    "module1.py": "vision_opencv/module1.py",
    "defectDetection_module3.py": "vision_opencv/defectDetection_module3.py",
    # YOLO
    "train.py": "yolo/train.py",
    "pt_file_test3.py": "yolo/pt_file_test3.py",
    "best.pt": "yolo/models/best.pt",
    "val": "yolo/results/val",
    "yoloClassification": "yolo/classification",
    # data collection tools + sample data
    "framesCR.py": "data_tools/framesCR.py",
    "video_to_frames.py": "data_tools/video_to_frames.py",
    "captures": "data/captures",
    # browser prototypes (index.html stays at the root: GitHub Pages serves it)
    "Inspect_Simulator.html": "simulators/Inspect_Simulator.html",
    "ai_can_inspection_digital_twin.html": "simulators/ai_can_inspection_digital_twin.html",
    # superseded first versions
    "InspectionLine.py": "archive/InspectionLine.py",
    "testScript1.py": "archive/testScript1.py",
}
# applied after MOVES: give the training script its missing .py extension
RENAMES = {"yolo/classification/yoloTraining": "yolo/classification/yoloTraining.py"}
KEEP_AT_ROOT = {"README.md", "index.html", ".gitignore", "requirements.txt", Path(__file__).name}

GITIGNORE = """\
# Python
__pycache__/
*.pyc
.venv/
venv/

# Training output and large datasets (download datasets from the Teams channel)
runs/
dataset/
Dataset_annotated/
*_frames/

# OS / editor
.DS_Store
Thumbs.db
.vscode/
.idea/
"""

REQUIREMENTS = """\
# pip install -r requirements.txt
numpy
opencv-python
ultralytics        # YOLO (installs torch)
PyQt6              # station/ apps

# Not on pip - install separately:
#   PySpin  - Teledyne/FLIR Spinnaker SDK Python wheel (camera)
#   gpiod   - on the Raspberry Pi: sudo apt install python3-libgpiod
#   tkinter - comes with Python (sudo apt install python3-tk on the Pi)
"""

LAYOUT = """\
## Folder Layout

```
index.html            hosted prototype (GitHub Pages - keep at the root)
station/              integrated inspection station apps (PyQt + YOLO + sensor + stack light)
hardware_tests/       stack light, GPIO sensor, air reject and camera bring-up scripts
vision_opencv/        OpenCV edge/flange defect detection
yolo/                 YOLO training and testing
  models/best.pt      current trained model
  results/val/        validation plots (confusion matrix, PR/F1 curves)
  classification/     pass/fail classification scripts
data_tools/           frame capture and video-to-frames tools
data/captures/        sample Good / Defect can images
simulators/           browser prototypes of the inspection line
archive/              superseded first versions, kept for reference
```

Install the Python packages with `pip install -r requirements.txt` (see the file for PySpin and gpiod).

---

"""


def git(*args, check=True):
    return subprocess.run(["git", *args], check=check, text=True, capture_output=True).stdout.strip()


def die(msg):
    sys.exit(f"Stopped: {msg}")


def main():
    root = Path(git("rev-parse", "--show-toplevel", check=False) or ".").resolve()
    if not (root / ".git").exists():
        die("run this from inside the CMPSC488_AICanDefects repo.")
    import os
    os.chdir(root)

    if git("status", "--porcelain", "--untracked-files=no"):
        die("you have uncommitted changes - commit or stash them first.")
    if git("rev-parse", "--verify", "--quiet", BRANCH, check=False):
        die(f"branch '{BRANCH}' already exists - delete it or rename BRANCH in this script.")
    current = git("rev-parse", "--abbrev-ref", "HEAD")
    if current != "main":
        print(f"Note: you're on '{current}', not main - the new branch will start from '{current}'.")

    git("checkout", "-b", BRANCH)

    moved = {}
    for old, new in {**MOVES}.items():
        if not Path(old).exists():
            print(f"  skip (not found): {old}")
            continue
        Path(new).parent.mkdir(parents=True, exist_ok=True)
        git("mv", old, new)
        moved[old] = new
        print(f"  {old} -> {new}")
    for old, new in RENAMES.items():
        if Path(old).exists():
            git("mv", old, new)
            print(f"  {old} -> {new}")

    # station apps: default model path now points at yolo/models/best.pt, wherever you run them from
    for name in ("station/can_inspection_station.py", "station/can_inspection_station_2.py"):
        p = Path(name)
        if not p.exists():
            continue
        s = p.read_text(encoding="utf-8")
        if "from pathlib import Path" not in s:
            s = s.replace("import argparse\n", "import argparse\nfrom pathlib import Path\n", 1)
        s = s.replace(
            'parser.add_argument("--model", default="best.pt"',
            'parser.add_argument("--model", default=str(Path(__file__).resolve().parent.parent / "yolo" / "models" / "best.pt")',
        )
        s = s.replace("python can_inspection_station.py --model best.pt", "python station/" + p.name + " --model yolo/models/best.pt")
        p.write_text(s, encoding="utf-8")

    # README: prefix moved file names with their new folders and add a layout section
    readme = Path("README.md")
    if readme.exists():
        s = readme.read_text(encoding="utf-8")
        # skip generic folder names ("val", "captures") - the README also uses them for dataset folders
        readme_moves = {k: v for k, v in moved.items() if k not in ("val", "captures")}
        for old, new in {**readme_moves, "yoloClassification/": "yolo/classification/"}.items():
            old_name = old.rstrip("/")
            new_name = new if not old.endswith("/") else new.rstrip("/")
            s = re.sub(rf"`{re.escape(old_name)}(/?)`", lambda m: f"`{new_name}{m.group(1)}`", s)
        if "## Folder Layout" not in s:
            s = s.replace("## Repository Structure", LAYOUT + "## Repository Structure", 1)
        readme.write_text(s, encoding="utf-8")

    for name, text in ((".gitignore", GITIGNORE), ("requirements.txt", REQUIREMENTS)):
        if not Path(name).exists():
            Path(name).write_text(text, encoding="utf-8")

    git("add", "-A", "README.md", ".gitignore", "requirements.txt", "station")
    git("commit", "-m", "Organize repo into folders\n\nMove scripts into station/, hardware_tests/, "
        "vision_opencv/, yolo/, data_tools/, data/, simulators/ and archive/ (git mv keeps history). "
        "index.html stays at the root for GitHub Pages. Station apps default to yolo/models/best.pt; "
        "add .gitignore and requirements.txt; update README paths.")

    leftovers = sorted(p.name for p in Path(".").iterdir()
                       if not p.name.startswith(".") and p.name not in KEEP_AT_ROOT
                       and p.name not in {Path(n).parts[0] for n in list(MOVES.values()) + ["yolo"]})
    print(f"\nDone - committed on branch '{BRANCH}' (not pushed).")
    if leftovers:
        print("Not moved (new or unknown files - sort these by hand):", ", ".join(leftovers))
    print(f"Check it, then: git push -u origin {BRANCH}   and open a pull request.")


if __name__ == "__main__":
    main()
