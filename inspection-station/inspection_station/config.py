"""
Configuration for the can inspection station.

Two kinds of configuration live here:

  * WIRING   - fixed by how the Pi is wired. Change only if the wiring changes.
  * Settings - operator-tunable values (reject timing, camera IP, ...). They are
               edited from the dashboard and saved to config.json, so they
               survive restarts.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = APP_DIR / "config.json"
DATA_DIR = APP_DIR / "data"

# =============================================================================
# WIRING (BCM GPIO numbers; physical header pins in the comments)
# =============================================================================
SENSOR_PIN = 24            # header pin 18 <- sensor signal (5V -> resistor divider -> 3.3V)
SENSOR_ACTIVE_LOW = True   # signal goes LOW while a can blocks the sensor
SENSOR_PULL = "pn"         # no internal pull - the divider defines the level

RELAY_ACTIVE_LOW = True    # 4-channel relay board: input LOW = relay ON

LIGHT_PINS = {
    "green": 17,           # header pin 11 -> relay 1
    "yellow": 27,          # header pin 13 -> relay 2
    "red": 22,             # header pin 15 -> relay 3
}
REJECT_PIN = 23            # header pin 16 -> relay 4 -> reject solenoid valve

WIRING = [  # (physical pin, signal, connected to)
    (2, "5V", "Sensor power"),
    (4, "5V", "Relay board VCC"),
    (6, "GND", "Relay board GND"),
    (9, "GND", "Sensor GND"),
    (11, "GPIO17", "Relay 1 - green lamp"),
    (13, "GPIO27", "Relay 2 - yellow lamp"),
    (15, "GPIO22", "Relay 3 - red lamp"),
    (16, "GPIO23", "Relay 4 - reject solenoid"),
    (18, "GPIO24", "Sensor signal (through divider)"),
]

# =============================================================================
# TIMING CONSTANTS
# =============================================================================
POLL_INTERVAL_S = 0.005          # sensor polling period when gpiozero is unavailable
LATE_TOLERANCE_MS = 20           # a reject later than this is skipped (can is past the nozzle)
DEFECT_LAMP_S = 1.0              # yellow lamp on per defective can
CAMERA_CHECK_INTERVAL_S = 5      # how often the camera IP is pinged

# Heat protection (°C). Pi 5 firmware throttles from about 80 °C and hard-limits at 85 °C.
CPU_WARM_C = 70                  # live view slows down
CPU_HOT_C = 78                   # live detection off, camera fps reduced, red fault
CPU_CRITICAL_C = 84              # inspection stopped (results unreliable), latched until Reset
CAM_WARN_C = 60                  # camera warm - logged, chip turns amber
CAM_MAX_C = 65                   # camera too hot - red fault (check the Genie Nano datasheet limit)
TEMP_HYSTERESIS_C = 4            # must cool this much below a level before it clears
STANDBY_FPS = 2                  # camera frame rate in standby
DISK_MIN_MB = 300                # red fault below this much free storage


def check_pin_conflicts():
    pins = [SENSOR_PIN, REJECT_PIN, *LIGHT_PINS.values()]
    dup = sorted({p for p in pins if pins.count(p) > 1})
    if dup:
        raise SystemExit(f"GPIO conflict: pin(s) {dup} used more than once - fix config.py")


# =============================================================================
# OPERATOR SETTINGS (persisted to config.json)
# =============================================================================
@dataclass
class Settings:
    inspector: str = "Simulated"
    sim_defect_rate: int = 10          # percent, for the simulated inspector
    camera_ip: str = "192.168.0.10"    # Ethernet camera; the Pi needs a static IP on the same subnet
    reject_enabled: bool = True
    single_shot: bool = False          # lock the rejector after one fire (air testing)
    reject_delay_ms: int = 150         # sensor -> air nozzle travel time
    pulses: int = 3                    # air burst profile
    pulse_on_ms: int = 40
    pulse_off_ms: int = 30
    alarm_after: int = 3               # N defects in a row -> yellow flashing warning
    warn_rate_pct: int = 20            # defect rate (%) over the last cans -> yellow flashing warning
    warn_window: int = 50              # how many recent cans the defect rate looks at
    min_can_gap_ms: int = 50           # debounce: ignore sensor re-triggers closer than this
    sensor_backend: str = "auto"       # "auto" (gpiozero, else pinctrl) | "gpiozero" | "pinctrl"
    log_to_disk: bool = True           # write data/inspections_YYYY-MM-DD.csv + event log

    # camera + image processing
    camera_backend: str = "synthetic"  # "gige" (Teledyne via Aravis) | "webcam" | "synthetic" | "folder"
    webcam_index: int = 0
    image_folder: str = ""
    camera_delay_ms: int = 30          # sensor -> can centred under the camera
    exposure_us: int = 2000
    gain_db: int = 0
    vision_engine: str = "builtin"     # "builtin" (vision.py) | "team" (team_vision.py) | "yolo"
    yolo_model: str = "models/can_defects.pt"   # .pt / .onnx / *_ncnn_model folder (relative to the project)
    yolo_conf: int = 50                # % - detections below this confidence are ignored
    yolo_imgsz: int = 640              # model input size (smaller = faster, misses small defects)
    yolo_crop: bool = True             # crop to the can before YOLO (small defects stay visible)
    active_cooler: bool = False        # Pi 5 active cooler fitted? Without it, YOLO runs only on inspected cans
    vision_sensitivity: int = 50       # 1..100
    vision_min_area: int = 40          # px at 640 px image width
    save_images: str = "fails"         # "off" | "fails" | "all"
    save_annotated: bool = True        # also save a copy with boxes drawn

    # conveyor geometry (Diagnostics > Timing) - distances from the trigger sensor beam
    can_diameter_mm: int = 66          # 12 oz standard can (211 diameter) = 66 mm
    camera_distance_mm: int = 0        # sensor beam -> centre of the camera view (0 = not measured)
    nozzle_distance_mm: int = 0        # sensor beam -> air nozzle (0 = not measured)

    # storage
    keep_images_days: int = 30         # delete saved images older than this (labelled ones kept); 0 = keep all

    # heat & power
    camera_fps: int = 20               # camera frame rate while running (lower = cooler)
    standby_after_min: int = 10        # line stopped this long -> camera standby (0 = never)

    # access control (engineer PIN, stored hashed - set it from the dashboard)
    engineer_pin_hash: str = ""
    engineer_pin_salt: str = ""

    @property
    def burst_ms(self) -> int:
        return self.pulses * (self.pulse_on_ms + self.pulse_off_ms)

    @classmethod
    def load(cls, path: Path = CONFIG_FILE):
        """Return (settings, warning-or-None). Unknown or mistyped keys are ignored."""
        s = cls()
        if not path.exists():
            return s, None
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            return s, f"config.json could not be read ({e}) - using defaults"
        for f in fields(cls):
            if f.name not in data:
                continue
            default, value = getattr(s, f.name), data[f.name]
            if isinstance(default, bool):
                ok = isinstance(value, bool)
            elif isinstance(default, int):
                ok = isinstance(value, int) and not isinstance(value, bool)
            else:
                ok = isinstance(value, type(default))
            if ok:
                setattr(s, f.name, value)
        return s, None

    def save(self, path: Path = CONFIG_FILE):
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        os.replace(tmp, path)   # atomic: never leaves a half-written config
