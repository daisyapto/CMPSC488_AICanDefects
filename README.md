# Yards Brewing Company - AI Can Inspection Station

CMPSC 488 capstone. A low-cost station that inspects empty cans on the Yards
Brewing line before filling, marks each can PASS / FAIL, drives a stack light
and buzzer, and logs every result.

This branch, **`yards-can-inspection`**, is the demonstration version: it holds
only the code the station runs. The full history, early prototypes and test
scripts stay on the other branches.

## Hardware

| Part | Connection |
|---|---|
| Raspberry Pi 5 | official 27 W USB-C supply (Active Cooler recommended for YOLO) |
| Teledyne DALSA Genie Nano G3-GC10-C1280 (GigE Vision) | own Ethernet cable to the Pi; PoE injector (802.3af) or 10-36 V on its 10-pin connector |
| Trigger sensor (5 V) | signal through a resistor divider (5 V -> 3.3 V) to GPIO25 (header pin 22) |
| 4-channel relay board (active-low) | relay 1 green (GPIO5, pin 29), relay 2 yellow (GPIO6, pin 31), relay 3 red (GPIO26, pin 37), relay 4 buzzer (GPIO23, pin 16) |
| Rejector | not wired yet - gets its own free GPIO when fitted |
| Conveyor | KEYENCE conveyor on its own controller (not driven by the Pi) |

## What is in this branch

```
inspection-station/
  run.py                 start the station (dashboard at http://localhost:8080)
  inspection_station/    line controller, camera, vision, accounts, audit, web dashboard
  team_vision.py         team OpenCV can detection (default engine)
  camera_bridge.py       Genie Nano in its own process (stable live video)
  find_camera.py         find the camera on the network, show / set its IP
  models/                trained YOLO11n-cls pass / fail classifier
  training/              scripts to retrain and evaluate the classifier
  pi_setup.sh            one-time Raspberry Pi setup
  pi_check.py            Raspberry Pi readiness check
```

Main features: trigger sensor -> camera capture -> inspection (team OpenCV,
built-in OpenCV or YOLO) -> PASS / FAIL, stack light and buzzer, web dashboard
for the station screen, a laptop or a phone, line display for a monitor,
recipes per can size, operator (PIN) and engineer (password) accounts with an
audit trail, image review and dataset export, batch reports, alarms with
troubleshooting steps, diagnostics and heat protection.

## Quick start

```bash
cd inspection-station
python run.py --sim        # laptop: simulation with a synthetic camera
```

On the Raspberry Pi:

```bash
cd ~/inspection-station
sudo bash pi_setup.sh      # once, then reboot
python3 pi_check.py        # checks power, GPIO, camera network and video
python3 run.py             # or: sudo systemctl start can-inspection
```

The first start asks for the first engineer account. See
`inspection-station/README.md` for wiring, camera setup, accounts and the test
procedure.
