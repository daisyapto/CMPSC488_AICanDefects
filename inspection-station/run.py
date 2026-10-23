#!/usr/bin/env python3
"""Start the can inspection station.

    python run.py --sim         # on a PC / laptop (simulation - no GPIO)
    python run.py               # on the Pi: real hardware
    python run.py --help        # all options
"""
import importlib.util
import sys

NEEDED = [  # (import name, pip package)
    ("flask", "flask"),
    ("numpy", "numpy"),
    ("cv2", "opencv-python"),
]


def check_python():
    if sys.version_info < (3, 10):
        sys.exit(f"Python 3.10 or newer is needed - this is {sys.version.split()[0]}.\n"
                 "Install a newer Python from https://www.python.org/downloads/")
    missing = [pkg for mod, pkg in NEEDED if importlib.util.find_spec(mod) is None]
    if missing:
        # Use THIS Python's pip - a plain "pip install" often goes to a different Python.
        cmd = f'"{sys.executable}" -m pip install -r requirements.txt'
        sys.exit("Missing Python packages: " + ", ".join(missing) + "\n\n"
                 "Install them with exactly this command (run it in the inspection-station folder):\n\n"
                 f"    {cmd}\n")


if __name__ == "__main__":
    check_python()
    from inspection_station.app import main
    main()
