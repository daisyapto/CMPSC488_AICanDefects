"""
Raspberry Pi health: CPU temperature and power / throttling flags.

  * CPU temperature from /sys/class/thermal/thermal_zone0/temp
  * `vcgencmd get_throttled` flags:
        bit 0  under-voltage now          bit 16 under-voltage has occurred
        bit 1  ARM frequency capped now   bit 17 frequency capping has occurred
        bit 2  throttled now              bit 18 throttling has occurred
        bit 3  soft temperature limit now bit 19 soft temperature limit has occurred
On a laptop (not a Pi) nothing is reported and no heat actions are taken.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

THERMAL = Path("/sys/class/thermal/thermal_zone0/temp")
MODEL = Path("/proc/device-tree/model")


class SystemHealth:
    def __init__(self):
        try:
            self.model = MODEL.read_text(errors="ignore").strip("\x00\n ")
        except OSError:
            self.model = ""
        self.is_pi = "Raspberry Pi" in self.model
        self._vc = shutil.which("vcgencmd")
        self._flags_at = 0.0
        self.flags = 0
        self.fake_temp = None            # tests can set this to simulate heat

    def cpu_temp(self):
        if self.fake_temp is not None:
            return float(self.fake_temp)
        if not self.is_pi:
            return None
        try:
            return int(THERMAL.read_text()) / 1000
        except (OSError, ValueError):
            return None

    def read_flags(self) -> int:
        """Throttle flags, refreshed at most every 5 s."""
        if self._vc and time.monotonic() - self._flags_at > 5:
            self._flags_at = time.monotonic()
            try:
                out = subprocess.check_output([self._vc, "get_throttled"], text=True, timeout=2)
                self.flags = int(out.strip().split("=")[1], 16)
            except Exception:
                pass
        return self.flags

    @staticmethod
    def describe(flags: int) -> dict:
        return {
            "undervoltage_now": bool(flags & 0x1),
            "throttled_now": bool(flags & 0x6),
            "soft_limit_now": bool(flags & 0x8),
            "undervoltage_seen": bool(flags & 0x10000),
            "throttled_seen": bool(flags & 0x60000),
        }
