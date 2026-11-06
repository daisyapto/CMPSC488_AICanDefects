"""
Entry point.

    python3 run.py                    web dashboard on http://localhost:8080 (opens the browser)
    python3 run.py --kiosk            Pi: fullscreen Chromium on the station screen
    python3 run.py --sim              simulation - no GPIO is touched (safe demo on the Pi)
    python3 run.py --remote-control   let other devices on the network control the line
    python3 run.py --tailscale        let devices on your Tailscale network log in and control
"""
from __future__ import annotations

import argparse
import queue
import signal

from .config import Settings, check_pin_conflicts
from .controller import Controller
from .hardware import make_io


def main():
    ap = argparse.ArgumentParser(description="Can inspection station")
    ap.add_argument("--sim", action="store_true", help="simulation mode: do not touch GPIO")
    ap.add_argument("--port", type=int, default=8080, help="web dashboard port (default 8080)")
    ap.add_argument("--host", default="0.0.0.0",
                    help="address to listen on (default all; 127.0.0.1 = this computer only)")
    ap.add_argument("--kiosk", action="store_true", help="open fullscreen Chromium (Pi station screen)")
    ap.add_argument("--screen", choices=["dashboard", "display"], default="dashboard",
                    help="what the station screen shows: the full dashboard or the big line display")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser")
    ap.add_argument("--remote-control", action="store_true",
                    help="allow other devices to control the line (default: view-only + E-stop/Stop)")
    ap.add_argument("--tailscale", action="store_true",
                    help="let devices on your Tailscale network log in and control (LAN stays view-only)")
    args = ap.parse_args()

    check_pin_conflicts()
    settings, warning = Settings.load()
    from .edition import EDITION, YOLO_AVAILABLE
    if not YOLO_AVAILABLE and settings.vision_engine == "yolo":
        settings.vision_engine = "builtin"
        warning = (warning + "; " if warning else "") + "YOLO is not in the lite edition - using Built-in detection"
    print(f"Can inspection station - {EDITION} edition")
    events: queue.Queue = queue.Queue()
    ctl = Controller(make_io(force_sim=args.sim), settings, events)
    if warning:
        ctl.log("WARN", warning)

    # systemd / kill -> clean shutdown (valve closed, relays off)
    signal.signal(signal.SIGTERM, lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))

    try:
        try:
            from .web import serve
        except ImportError as e:
            raise SystemExit(f"Flask is needed for the web dashboard ({e}).\n"
                             "  Laptop:       pip install flask\n"
                             "  Raspberry Pi: sudo apt install python3-flask")
        serve(ctl, host=args.host, port=args.port, open_browser=not args.no_browser,
              kiosk=args.kiosk, remote_control=args.remote_control, screen=args.screen,
              tailscale=args.tailscale)
    except KeyboardInterrupt:
        pass
    finally:
        ctl.shutdown()          # fail-safe: valve closed, relays off, settings saved


if __name__ == "__main__":
    main()
