# Can Inspection Station

Raspberry Pi 5 controller and operator dashboard for the Yards Brewing empty-can
inspection line (CMPSC 488). It reads the trigger sensor, inspects each can,
fires the reject air at the right moment, drives the stack light, and logs
every result.

## Editions

| | **inspection-station-yolo.zip** (full) | **inspection-station-lite.zip** |
|---|---|---|
| Detection engines | Built-in, Team, **YOLO** | Built-in, Team |
| Extra install | `pip install ultralytics` (+ `ncnn` on the Pi) | nothing |
| Pi cooling | active cooler recommended for YOLO | fine without |

Both come from the same source: edit the normal code, then
`python tools/build_editions.py` builds both zips. The edition is shown in
Settings → Heat & power. Without the active cooler, leave *Active cooler fitted*
off in the YOLO settings: YOLO then runs only on inspected cans, and the live
view shows the plain camera image.

## Run

```bash
python3 run.py                    # web dashboard: http://localhost:8080 (opens the browser)
python3 run.py --sim              # simulation - no GPIO is touched (safe demo on the Pi)
python3 run.py --kiosk            # Pi: fullscreen Chromium on the station screen
python3 run.py --remote-control   # let other devices on the network control the line
python3 run.py --tk               # the older desktop (Tkinter) dashboard (no recipes/review/reports)
```

On a laptop without the Pi hardware it starts in simulation automatically.
If a package is missing, `run.py` prints the exact install command for the
Python you are using.

**Laptop camera:** Settings → Camera → Source → *Webcam* → **Find cameras**
shows a preview of every camera on the computer (laptops often also have an IR
face-login camera, and phones / virtual cameras show up too). Click the right one;
the choice is saved.

### Install

```bash
# PC / laptop (Python 3.10+). Use "python -m pip" so the packages go to the
# same Python that runs the app (Windows: py -m pip ..., macOS: python3 -m pip ...)
python -m pip install -r requirements.txt

# Raspberry Pi (OS Bookworm)
sudo apt install python3-flask python3-opencv python3-numpy \
                 gir1.2-aravis-0.8 python3-gi aravis-tools
```

Camera features need NumPy and OpenCV; without them the app still runs and
only the camera is disabled. `gpiozero` (preinstalled on Raspberry Pi OS)
gives interrupt-driven sensor input; without it the sensor is polled with
`pinctrl`. If you use a Python venv on the Pi, create it with
`--system-site-packages` so it can see `gi` (Aravis) and the apt OpenCV.

### Web dashboard

* Works at any screen size - a 1080p monitor, a small 7-10" display, a
  laptop or a phone (the layout adapts).
* **The station screen** (the Pi itself) has full control.
* **Other devices** on the network open `http://<pi-ip>:8080` (the address
  is printed at start-up). They are **view-only**, except **E-stop** and
  **Stop**, which always work. Start the server with `--remote-control` to
  allow full control from other devices.
* Settings (gear icon) apply immediately and are saved on the station, so
  every screen stays in sync.
* Keyboard: **Esc** = E-stop (always), **Space** = simulate a can, **F** = fullscreen.
* Click a row in the Inspections table, or the last-can image, to open the
  saved image.

### Start automatically on boot (kiosk)

Create `~/.config/autostart/inspection.desktop` on the Pi:

```ini
[Desktop Entry]
Type=Application
Name=Can Inspection
Exec=python3 /home/pi/inspection-station/run.py --kiosk
```

(Adjust the path.) On the next login the station starts and the dashboard
opens fullscreen. Add `--screen display` to open the big line display instead.

## Line display (screen next to the line)

`http://<pi>:8080/display` (or the monitor icon in the dashboard's top bar) is a
read-only view made to be read from a few metres away:

* a big colour banner - **RUNNING** (green), **DEFECT WARNING** (yellow, flashing),
  the fault name (red), **EMERGENCY STOP** - matching the stack light;
* the last inspected can with PASS / DEFECT, big counters (inspected, rejected,
  yield, cans/min), the recent-cans strip;
* during a fault: **what to check**, step by step; otherwise the defect types.

Options: `?live=1` shows the live camera instead of the last can (more CPU and
heat), `?controls=1` adds big Start / Stop / Reset / Acknowledge / E-stop
buttons for a **touch screen on the station** (they only work on the station
itself unless `--remote-control`). The on-screen E-stop is a convenience, not a
safety device - the line still needs a real E-stop button.

Screens that work:

| Screen | How | Notes |
|---|---|---|
| Any HDMI monitor or TV | Pi 5 **micro-HDMI** → HDMI cable | cheapest; best for a demo audience |
| Raspberry Pi Touch Display 2 (7") | DSI ribbon cable | touch → use `?controls=1` |
| A laptop / tablet / TV browser | open `http://<pi-ip>:8080/display` | no cable to the Pi; view-only |

On the Pi: `python3 run.py --kiosk --screen display`, and turn off screen
blanking (`sudo raspi-config` → Display Options → Screen Blanking → No). The
display hides the mouse pointer after 3 s; **F** toggles fullscreen.

## Alarms, diagnostics and history

**Alarms** (Line page → *Alarms* tab): every fault and defect warning is
recorded with start time, how long it lasted and how it was cleared (Reset,
acknowledged, cleared itself), and comes with **what to check** steps. Click the
red/yellow message in the Stack light card to jump there. History survives
restarts (`data/alarms.jsonl`).

**Diagnostics** page - for setting up and after moving to a new conveyor:

* **Trigger sensor**: live BLOCKED / CLEAR, trigger count, how long the last
  can blocked the beam. Works while the line is stopped - block it by hand.
* **Belt speed & timing**: belt speed = can diameter ÷ time the can blocks the
  sensor (averaged over the last 10 cans), its variation, and the closest gap
  between cans. Enter the distances sensor → camera centre and sensor → air
  nozzle (measure them on the belt) and it recommends *Capture delay* and
  *Reject delay*, with an **Apply** button. It also checks that inspection fits
  in the time before the nozzle and that the air burst is shorter than the gap
  between cans.
* **Outputs**: **Run self-test** (each lamp 1 s, then one air burst), manual
  lamp switches, test burst. Line must be stopped.
* **Camera** status and **System & storage**: free space, size of saved images,
  and *Keep images* (default 30 days; older images are deleted hourly, but
  images someone labelled on the Review page are kept). Below 300 MB free the
  red light comes on.

**History** page: cans, yield, rejects and *not rejected* per day for 7 / 30 /
90 days (from the daily CSV logs), defect types over the period, best and worst
day, and a CSV per day.

## Moving to the Raspberry Pi (checklist)

What is different from the laptop, and what handles it:

| On the Pi | Why it matters | Handled by |
|---|---|---|
| Software comes from apt, not pip | pip OpenCV is slow/huge on the Pi; Aravis (camera) is apt-only | `sudo bash pi_setup.sh` step 1 |
| Relay pins at power-up | the relay board is active-low: a floating pin while the Pi boots can switch a relay - or the **air valve** - ON until the station starts | step 2: `gpio=5,6,23,26=op,dh` in config.txt (check after a reboot: relays stay off) |
| Camera network | with no DHCP server NetworkManager gives eth0 **no address** - the camera is invisible | step 3/4: eth0 = 169.254.100.1/16, bigger network buffers. Internet / dashboard access stays on Wi-Fi |
| No firewall problem | Raspberry Pi OS has no firewall, so the Windows bridge / firewall steps are not needed | - |
| Camera power | the Pi's Ethernet gives no power either | same PoE injector as on the laptop |
| Pi power | weak supply = under-voltage, resets, corrupted SD card | official 27 W supply; `pi_check.py` reads the supply and throttling |
| Heat | no Active Cooler yet | use the **lite** edition; `pi_check.py` shows temperature / fan |
| Clock | without internet (and without the RTC battery) the Pi's time can be wrong -> wrong timestamps in logs and reports | connect Wi-Fi once after power-up |
| Starts by itself, restarts after a crash | | step 6: `can-inspection` service (+ optional kiosk screen) |

Steps on the Pi:

```bash
# copy the unzipped inspection-station folder to the Pi (USB stick / scp), then:
cd ~/inspection-station
sudo bash pi_setup.sh          # answers default to "yes" - press Enter
sudo reboot
python3 pi_check.py            # everything that can be checked without moving parts
python3 pi_check.py --sensor --outputs   # live: block the sensor; watch each lamp; one air pulse
python3 run.py                 # or: sudo systemctl start can-inspection
```

`pi_check.py` only reads and reports. It checks the Pi model and power supply,
installed software, GPIO pins and boot safety, the relay switching speed, the
camera network, a real **8-second video test** (complete frames per second,
lost frames, packet size, camera temperature), free space, the clock and
autostart. Each problem comes with the fix. Stop the station before running it,
because the camera can only be used by one program at a time.

## Recipes, access, review and batch reports

### Recipes (can sizes)
Line control › **Recipe** switches every can-dependent setting at once:
capture and reject delay, exposure, gain, sensitivity, min defect size, air
burst - plus that recipe's own **good-can reference** (Teach good can saves
into the active recipe). Settings that belong to a recipe are marked **R** in
Settings. If you change one, Line control shows "Changed from recipe"; an
engineer saves it (Settings › Recipe › Save) or creates a new recipe from the
current settings. Recipes can't be switched while the line runs or a batch is
open. Stored in `recipes.json` and `data/recipes/`.

### Operator / engineer access
| | Viewer (other devices) | Operator (station, no login) | Engineer (name + PIN) |
|---|---|---|---|
| E-stop, Stop | ✓ | ✓ | ✓ |
| Start, Reset, Acknowledge, simulate, wake camera | | ✓ | ✓ |
| Choose recipe, start / end batch, live view mode | | ✓ | ✓ |
| Settings, teach reference, manual override, test burst, recipes, label images, export dataset, clear session | | | ✓ |

* Log in with the lock button in the top bar. Default PIN **1234** - change it
  in Settings › Security (a banner reminds you). The PIN is stored hashed.
* Engineers are logged out after **15 minutes** without activity. Five wrong
  PINs lock the login for 60 s.
* Permissions are enforced by the station, not just hidden in the page.

### Audit trail
Every command, setting change (old → new value), login, recipe and batch
action is recorded with who, role, time and device - **Audit** tab on the Line
page, and `data/audit.jsonl`.

### Review page (human check + AI dataset)
Every saved image (Settings › Detection › Save images: *Fails* or *All*) can be
reviewed: open it and answer **"Was the station right?"**

* Rejected can → *Real defect* (pick the type) or *False reject* (good can).
* Passed can → *Good can* or *Missed defect* (pick the type).
* Keys: **1** right, **2** wrong, **← →** next/previous.

The page shows the **false-reject rate** and missed defects - the key numbers
for judging a vision system. **Export dataset** downloads a ZIP with the
reviewed images sorted into class folders (`good/`, `dent/`, `scratch/`, ...)
plus `labels.csv` - ready for training the AI model. Labels are stored in
`data/review_labels.jsonl`.

### Batches & shift reports
Line control › **Start batch**: lot number, operator, optional note. Every can
until **End batch** belongs to that batch (it survives a restart). The
**Reports** page lists all batches; each has a printable report (Print / save
as PDF) with results, yield, defects by type, a throughput chart, warnings and
faults, temperatures, the settings used, review results and signature lines,
plus a CSV of every can. Stored in `data/batches/`.

## Project layout

| File | What it does | Who edits it |
|---|---|---|
| `inspection_station/config.py` | Wiring (GPIO pins) and default settings | when wiring changes |
| `inspection_station/hardware.py` | GPIO, relays, sensor, camera ping | rarely |
| `inspection_station/health.py` | Pi temperature, power / throttling flags | rarely |
| `inspection_station/recipes.py` | Recipes per can size (+ reference images) | rarely |
| `inspection_station/batches.py` | Batch / shift tracking | rarely |
| `inspection_station/report.py` | Printable batch report | report layout |
| `inspection_station/review.py` | Image review labels, dataset export | rarely |
| `inspection_station/audit.py` | Audit trail | rarely |
| `inspection_station/alarms.py` | Alarm history + "what to check" steps | troubleshooting text |
| `inspection_station/history.py` | Per-day totals for the History page | rarely |
| `team_vision.py` | **Can detection (default engine)**: rim circles, dents, out-of-round, flange, side wall, bottom | **vision team** |
| `inspection_station/syncan.py` | Realistic generated can-top images (Synthetic camera + tests) | - |
| `inspection_station/inspectors.py` | PASS/FAIL decision per can | AI model goes here |
| `inspection_station/vision.py` | Image processing: find can, detect + classify defects | tuning / vision work |
| `camera_bridge.py` | Windows / macOS: streams the Genie Nano through Aravis in MSYS2 to the dashboard | - |
| `pi_setup.sh` | One-time Raspberry Pi setup (software, boot-safe relays, camera network, autostart) | - |
| `pi_check.py` | Raspberry Pi readiness check with fixes | - |
| `find_camera.py` | Find the Genie Nano on the network, show / set its IP (no camera software needed) | - |
| `inspection_station/gige.py` | GigE Vision discovery / IP setting used by the above | rarely |
| `inspection_station/camera.py` | Camera sources (Genie Nano, webcam, synthetic, folder), image saving | rarely |
| `inspection_station/controller.py` | Line logic: queue, reject timing, alarms, logging | rarely |
| `inspection_station/web.py` | Web server: live updates, commands, camera stream | rarely |
| `inspection_station/static/` | Dashboard (`index.html`, `app.*`) and line display (`display.*`) - no build step | UI changes |
| `inspection_station/ui.py` | Older Tkinter dashboard (`--tk`) | - |
| `config.json` | Saved settings + hashed engineer PIN (created automatically) | the app |
| `recipes.json` | Recipes | the app |
| `data/` | daily CSV + event log, `images/`, `snapshots/`, `batches/`, `recipes/`, `audit.jsonl`, `review_labels.jsonl`, `exports/` | the app |

## Wiring

| Header pin | Signal | Connected to |
|---|---|---|
| 2 | 5V | Sensor power |
| 4 | 5V | Relay board VCC |
| 6 | GND | Sensor GND |
| 9 | GND | Relay board GND |
| 16 | GPIO23 | Relay 4 - reject solenoid |
| 22 | GPIO25 | Sensor signal (5V through a resistor divider to 3.3V) |
| 29 | GPIO5 | Relay 1 - green lamp |
| 31 | GPIO6 | Relay 2 - yellow lamp |
| 37 | GPIO26 | Relay 3 - red lamp |

Motor HAT pins: see **Motors: conveyor belt and camera height**.

The relay board is **active-low**: a LOW pin turns the relay ON. That includes
the reject solenoid on relay 4. Do not use the old `AirTest.py` with this
wiring; it treats LOW as "air off".

The Genie Nano camera connects to the Pi's Ethernet port - see **Camera
(Teledyne DALSA Genie Nano)** below for the network setup.

## Motors: conveyor belt and camera height

The **Motors** panel (belt button in the top bar, or press **C**) does what the
team's GTK motor program does, from the dashboard (also from a phone):

* **Conveyor** - speed 0-100 %, Move distance (steps), **◀ Reverse / Forward ▶**
  (move that many steps), **⇄ Auto** (back and forth), **Run continuous**, Stop.
  Pressing the active button again stops it. **Run with the line**: Start / Stop
  on the Line card also run / stop the belt.
* **Camera height** (only if the motor is fitted - Setup › Camera height motor) - ▲ Up / ▼ Down by the Move steps, presets **12 / 16 / 19.2 oz**
  (0 / 3300 / 6600 steps) after **Set 12 oz reference** (lower the camera to the
  12 oz height by hand first). The reference is lost when the station restarts.
* **Stop all** and **E-stop** stop both motors at once.

Hardware: Waveshare **Stepper Motor HAT (B)** (two HR8825 drivers).

The motors are driven by **the team's own C motor code** - `motor_c/` holds
`motor_control.c`, `HR8825.c`, `DEV_Config.c` and headers from the GTK motor
program, unchanged (only the step limit can be raised from the Makefile), plus
`motord.c`, a small command interface used instead of the GTK window. So speeds,
steps, timing and pins are exactly those of `./motor`. `pi_setup.sh` builds it
(`make -C motor_c`, needs `build-essential libglib2.0-dev liblgpio-dev`); the
station also builds it on first start. Setup › Motors can switch to the Python
port or to Simulated.

| Motor | EN | DIR | STEP | M0 / M1 / M2 |
|---|---|---|---|---|
| 1 - conveyor | 12 | 13 | 19 | 16 / 17 / 20 |
| 2 - camera height | 4 | 24 | 18 | 21 / 22 / 27 |

Step limit per command: `MAX_STEPS` in `motor_c/Makefile` (50000; the GTK program
used 9000), `MAX_STEPS` in `conveyor.py`, `conveyor_move_steps` in `web.py` and the
Move slider `max` in `static/index.html`.

**Close the GTK motor program before starting the station** - only one program
can drive the pins. **Never plug or unplug a motor while the HAT has power.**

## How a can is handled

1. The sensor edge creates a new can with a timestamp.
2. The inspection worker inspects cans one at a time, in arrival order.
3. On FAIL, the reject is scheduled for `detect time + reject delay`.
4. The air fires on time. If the decision came too late (the can is already
   past the nozzle) the can is logged as **MISSED** instead of firing at
   nothing.
5. Every can is written to `data/inspections_<date>.csv` with its reject
   status: `fired`, `missed`, `off` (reject disabled) or `locked` (E-stop /
   single-shot latch).

A FAIL that was not blown off the line is shown in **orange** as *not rejected*,
so escapes are never silent.

## Stack light

Follows the standard industrial meaning (IEC 60204-1): green = normal,
yellow = abnormal, red = fault. Only one lamp is lit at a time, by priority
red > yellow > green, so it reads clearly from across the line.

| Lamp | Meaning | Clears |
|---|---|---|
| **Red, solid** | Fault - something is wrong with the system | Live faults clear by themselves when fixed. E-stop and an escaped defect stay until **Reset** |
| **Yellow, flashing** | Unusual defect activity: N defects in a row, or the defect rate over the last 50 cans above the limit (both set in Settings) | **Acknowledge** |
| **Yellow, solid (1 s)** | A defective can was just found | automatically |
| **Green, solid** | Running, all systems OK | - |
| **Green, flashing** | Ready but stopped - cans are *not* being inspected | press **Start** |

Red faults:

* E-stop pressed *(latched)*
* A defective can passed without being rejected - air too late, rejector
  locked or reject turned off *(latched, so it cannot go unnoticed)*
* Camera not streaming while the Camera inspector is selected
* Trigger sensor cannot be read
* Inspection failing (errors in the last 5 s)
* Reject turned off while running
* Reject valve locked out (single-shot latch)

The dashboard lists the active reasons under the stack-light drawing and in
the top banner, and every fault and clear is written to the event log.

## Heat & power protection

### What the software does

| Raspberry Pi CPU | What happens |
|---|---|
| below 70 °C | normal |
| 70 °C (warm) | live view slowed to 5 fps |
| 78 °C (hot) | live view without detection (2 fps), camera max 10 fps, **red fault** - inspection keeps running |
| 84 °C (critical) | **inspection stops**, red until it has cooled **and** Reset is pressed |

The Pi 5 firmware itself starts throttling at about 80 °C, so the app backs
off before that. Levels clear only after cooling 4 °C below them (no
flickering). Thresholds are in `config.py`.

* **Power:** `vcgencmd get_throttled` is checked every 5 s. Under-voltage now
  is a red fault; under-voltage since boot is logged.
* **Camera temperature:** the Genie Nano's internal sensor (`DeviceTemperature`)
  is read every 5 s - warning at 60 °C, red fault at 65 °C. Check your Nano
  model's datasheet for its maximum case temperature and adjust `CAM_WARN_C` /
  `CAM_MAX_C` in `config.py`.
* **Camera standby:** when the line has been stopped for 10 min (Settings >
  Heat & power), the camera drops to 2 fps and the live view stops. **Start**
  or **Wake camera** resumes instantly.
* **Camera frame rate** while running defaults to 20 fps - plenty for one
  frame per can. Lower it if the Pi runs warm.
* Live detection only runs while someone is watching the camera view.
* A health line (temperatures) is written to the event log every 5 minutes,
  so you have data for the report.
* Temperatures are shown in the top bar and in Settings > Heat & power.

### Hardware checklist (this matters more than the software)

* **Pi 5 cooling:** use the official **Raspberry Pi Active Cooler** (or a case
  with a fan). A bare Pi 5 running vision all day *will* throttle.
* **Pi power:** the official **27 W (5 V 5 A) USB-C supply**. Weak supplies
  cause under-voltage, random resets and SD-card corruption.
* **Enclosure:** ventilated, not sealed, and not next to the air valve
  manifold or other heat sources.
* **Reject solenoid:** powered from its **own supply** (e.g. 24 V), never from
  the Pi. Put a **flyback diode** (DC coil) or suppressor across the valve
  coil - the voltage spike when it switches off can reset or damage the Pi.
* **Relay board:** if it has a JD-VCC jumper, power the relay coils from a
  separate 5 V supply so they do not load the Pi.
* **Trigger sensor:** measure the divider output with a multimeter **before**
  connecting it - it must be 3.3 V or less. 5 V on a GPIO pin can kill the Pi.
* **Genie Nano:** mount it on a **metal bracket** (it doubles as a heat sink),
  leave air around it, don't wrap or enclose it tightly. Use a proper PoE
  injector/switch or a 12-24 V supply.
* **Sensor input:** make sure the top bar shows *Sensor: gpiozero interrupt*.
  The pinctrl fallback polls 200 times a second and adds noticeable CPU load.
* **Storage:** "Save images: All" writes a lot; prefer "Fails", or use a USB
  SSD instead of the SD card for long runs.

## Camera (Teledyne DALSA Genie Nano)

The Genie Nano is a GigE Vision camera. It is read through **Aravis**, an
open-source GigE Vision library that runs on the Pi, so the Teledyne Sapera
SDK (Windows / x86 only) is not needed.

### 1. Power and cable

The Nano is powered by **PoE** (a PoE injector or PoE switch between camera
and Pi; the Pi 5 port does not supply PoE) or by **12-24 V** on its I/O
connector. The status LED turns solid blue when it has an IP and is ready.

### 2. Network (pick one)

Out of the box the Nano asks for DHCP and, when nobody answers, gives itself a
**link-local 169.254.x.x** address after a few seconds.

**Option A - simplest, direct cable:** let the Pi use link-local too.

```bash
sudo nmcli con mod "Wired connection 1" ipv4.method link-local ipv6.method disabled
sudo nmcli con up "Wired connection 1"
```

The app finds the camera on its own: if nothing answers at the configured IP
and exactly one camera is on the network, it uses that camera and saves its
IP.

**Option B - fixed IPs (recommended for the final station):** give the camera
a persistent IP once (Teledyne's *Network Configuration Tool* from Sapera LT on
any Windows PC, or `arv-tool-0.8`), e.g. camera `192.168.0.10`, and set the Pi
on the same subnet:

```bash
sudo nmcli con mod "Wired connection 1" ipv4.method manual ipv4.addresses 192.168.0.100/24
sudo nmcli con up "Wired connection 1"
```

### 3. Check it outside the app

```bash
arv-tool-0.8              # lists cameras: "Teledyne DALSA-Nano-...  (169.254.x.x)"
arv-viewer-0.8            # live picture - set focus and aperture here
```

Close arv-viewer before starting the app (only one program can control the
camera at a time).

### 4. In the app

Settings > Camera > Source **Genie Nano**, then Settings > Inspector **Camera**.
Color models (Nano-C...) stream BayerRG8 and are converted to color; mono
models (Nano-M...) stream Mono8. Exposure is in microseconds; gain on the
Nano is a multiplier (about 1x-8x) and is clamped to the camera's range.
The camera is free-running at up to 30 fps and the frame taken right after
`capture delay` is used for each can.

### Testing on a laptop (no Teledyne software needed)

The Genie Nano is a **GigE Vision network camera, not a webcam**: Windows and
macOS don't show it as a camera, so the *Webcam* source can never find it.

1. **Power** - a laptop's Ethernet port gives **no power**. Use a PoE injector
   or PoE switch (802.3af), or 10-36 V on the camera's 10-pin connector.
   Status LED: off = no power · red = starting · slow-flashing blue = no
   network link · **steady blue = has an IP, ready** · green = a program is connected.
2. **Cable** - straight into the laptop's Ethernet port. With no router, the
   laptop and the camera each pick a 169.254.x.x address; give it about a minute.
3. **Find it** - in the project folder: `python find_camera.py`
   (or in the dashboard: Settings > Camera > Source **Genie Nano** > **Find
   camera on the network**, or Diagnostics > Camera). It lists the camera's
   model, serial, MAC and **IP address**, and this computer's adapters.
   * "same network" -> click **Use** (or type the IP in Settings > Camera IP).
   * "different network" -> click **Give it ... (temporary)** (or
     `python find_camera.py --force-ip <ip> --mask <mask>`). That puts the
     camera on the laptop's network until it is power-cycled.
   * nothing found -> check the LED / power, wait a minute, and on Windows allow
     Python through the firewall on **Public** networks (a direct cable counts
     as public).
   * A fixed IP that stays after power-off: `python find_camera.py --persistent
     192.168.10.10 --mask 255.255.255.0` (the camera must be "same network" first).
4. **Live video** - the app streams from the Nano through Aravis. On the Pi /
   Linux it is installed with apt. On **Windows** use the bridge below.

### Setting up the picture (brightness and focus)

* **Too dark?** Camera card > **Auto exposure** (or Settings > Camera >
  *Auto-set exposure*). It raises the exposure, then the gain, until the picture
  is mid-grey. If it is still dark: open the lens **aperture** (the f-number
  ring - smaller number = more light) and add light. Long exposures blur moving
  cans, so on the running line prefer more light + a short exposure.
* **Focus**: the Genie Nano has **no autofocus** - the lens has a manual focus
  ring. Choose the **Focus** view on the Camera card: it shows the centre of the
  image at full resolution with a *sharpness* bar. Put a can (or printed text)
  where the cans will be, turn the ring slowly until the bar is longest, then
  **tighten the lens's locking screws** so vibration can't turn it. If it seems
  to "unfocus by itself", the ring is loose, or the camera / can height changed.
  If it never gets sharp, the can is closer than the lens's minimum focus
  distance (move the camera up or add a spacer ring) or a CS-mount lens is on
  this C-mount camera.

### Live video on Windows (Aravis bridge)

Aravis installs on Windows only into MSYS2's Python, so `camera_bridge.py` runs
there, talks to the camera, and hands the frames to the dashboard
(127.0.0.1:8765). One-time setup:

1. Install MSYS2 from <https://www.msys2.org>.
2. Start menu > **MSYS2 UCRT64**, then:
   `pacman -S --needed mingw-w64-ucrt-x86_64-aravis mingw-w64-ucrt-x86_64-python-gobject`

Each time:

3. In **MSYS2 UCRT64**: `cd /c/Users/<you>/.../inspection-station` (C:\ is
   `/c/`), then `python camera_bridge.py` - it prints the camera it found and
   "Bridge ready". If Windows Firewall asks, allow it on **Public** networks
   (the video arrives over UDP).
4. In a normal terminal: `python run.py` as usual. Settings > Camera > Source
   **Genie Nano** - the camera panel shows "(via bridge)". Exposure, gain and
   frame rate from Settings are passed to the camera.

Only one program can control the camera: close the bridge before using any
other camera tool. Stopping and restarting the bridge is fine - the dashboard
reconnects by itself.

To test the image processing on Windows meanwhile, put sample can photos in a
folder and use the **Image folder** source.

### Camera panel

* **Source**: Genie Nano, Webcam (laptop testing with a real can),
  Synthetic (generated cans with random defects), Image folder (replays
  images; one per simulated can).
* **View**: Detection (boxes + PASS/FAIL), Raw, Difference (heat map of what
  the detector sees), Edges.
* **Last inspected can**: the exact frame used for the decision. Click it to
  enlarge.
* **Teach good can**: put a good can under the camera and press it. Every can
  is then compared to that reference, which works much better on real cans
  (rings, embossing, printing) than the default local-contrast mode.
* **Exposure / Gain** go to the camera. **Capture delay** is the time from the
  sensor until the can is centred under the camera. **Sensitivity** and
  **Min defect** tune the detector.
* **Save**: Off / Fails / All. Images are written in the background to
  `data/images/<date>/<pass|fail>/`, with boxed copies in `annotated/`. This
  doubles as a labelled dataset for training the AI model. Saving pauses if
  free disk space drops under 500 MB.
* **Snapshot** saves the current full-resolution frame to `data/snapshots/`.

In the Inspections table, the IMG column marks cans with a saved image;
double-click a row to open it.

### Timing

The Diagnostics page measures belt speed and recommends both delays - see
*Alarms, diagnostics and history*.

The camera frame used is the first one taken `capture delay` ms after the
sensor. Inspection time (shown per can) must stay below the reject delay,
otherwise the can is logged as MISSED.

### Inspection area

Camera card → **Area** → drag a box around where the can sits → **Save area**.
Every detection engine then looks only inside that box; rails, bolts, the
next can and anything else outside it are ignored (shown dimmed in the live
view). **Whole view** removes it. Draw it with some margin around the can
(the can moves a little from can to can) but tight enough that the can fills
most of it - that also keeps more camera pixels for thin scratches.

### Can detection (`team_vision.py`, the default engine)

Settings → Detection → *Detection engine*: **Team** (default) runs
`detect(image)` in `team_vision.py`; **Built-in** is the older generic
spot detector in `vision.py`; **YOLO** a trained model.

`team_vision.py` builds on the team's `edgeDetection.py` / `edgeFaultDetection.py`
(Hough circle for the rim, circularity, size against a reference) and inspects
an open can from above in zones:

| Step | What it does | Finds |
|---|---|---|
| Find the can | Hough candidates inside the area, the one with the most complete rim edge; the outermost complete ring is the can (the bottom rim is a circle too) | "No can in area" (rejected) |
| Outer rim | 720 rays from the centre, sub-pixel edge on each, robust circle + ellipse fit | **Rim dent** (local bump/dip), **Out of round**, **Flange damage** (edge missing) , **Wrong size** (if a reference diameter is set) |
| Inner circles | flange inner edge and bottom rim found the same way | **Crushed / off-centre** (circles not concentric) |
| Surfaces | the can is "unrolled" around its centre; each spot is compared with the same radius at the angles around it, relative to the local brightness | **Flange damage**, **Side wall dent**, bottom: **Scratch**, **Contamination**, **Bottom dent** |

The live view draws the three circles (outer, flange inner, bottom); the Last
inspected can panel lists the measurements (diameter, roundness, oval, centre
offset, rim edge seen, time). Tune the numbers in **SETTINGS** at the top of
`team_vision.py` (tolerances in % of the radius, surface sensitivity in % of
brightness, minimum defect size, optional reference diameter / px per mm).

Tested on generated can images (`inspection_station/syncan.py`; the Synthetic
camera source uses them too) with uneven light, exposure changes, blur and
noise: all defect types caught, no false rejects, ~45 ms per can on a laptop
(expect 2-3x on the Pi). `python team_vision.py --demo` repeats the test;
`python team_vision.py folder/` runs it on real photos and writes `*_result.png`.
**Real cans and lighting will need tuning** - collect images with the station
(Save images: All), run them through `team_vision.py`, adjust SETTINGS.

The contract, if you replace it with your own code:

```python
def detect(image):            # BGR numpy array (inside the inspection area)
    return []                 # good can
    return [{"label": "Dent", "box": (x, y, w, h), "confidence": 0.93}]
    return {"defects": [...], "circles": [{"x", "y", "r", "label"}], "measurements": {"Diameter": "512 px"}}
```

If it raises, the can is **rejected** (fail-safe) and the error shows under the
engine selector. The file reloads automatically when saved. While a can is
being inspected the live view waits, so the live view never delays a reject.

### YOLO models

Settings → Detection → *Detection engine* → **YOLO** runs a trained
[Ultralytics](https://docs.ultralytics.com) model (`pip install ultralytics`).
Put the model in `models/` and enter its path (default `models/can_defects.pt`).

* **Detection model** (trained on boxes): every box is a defect; a class named
  `good` / `ok` / `pass` / `normal` is ignored.
* **Classification model** (`*-cls`): the top class decides; `good` etc. = PASS.
* **Formats:** `.pt`, `.onnx`, or an NCNN folder. On the Pi use NCNN - it is the
  fastest there: `yolo export model=best.pt format=ncnn` → enter `models/best_ncnn_model`.
* **Crop to the can first** (on by default) cuts the image down to the can before
  YOLO resizes it to the model size, so small dents and scratches don't vanish.
* **Timing:** the model is loaded and warmed up when it's selected / at start-up,
  so the first can isn't late. Inspections always get the model first; the live
  view only uses it when it's free (max 4 fps), so it can't delay a reject. Watch
  *last inference ... ms* under the engine selector: it must fit inside the
  reject delay - if not, lower *Model image size* or move the air nozzle further down the line.
* **Errors** (model missing, `ultralytics` not installed, inference crash) reject
  the can and show the error, like the Team engine.

**Training data:** Review page → *Export dataset* gives `dataset/train/<class>/`
and `dataset/val/<class>/` (80/20), ready for a classification model:
`yolo classify train data=dataset model=yolo11n-cls.pt imgsz=224`.
For a detection model, draw boxes on those images first (Roboflow, CVAT or
Label Studio) and export in YOLO format.

### Plugging in an AI model

In `inspectors.py`, `CameraInspector.inspect()` has one line marked for this:

```python
result = m.vision.analyze(frame, m.params)
```

Replace it with your model and return either a `vision.VisionResult` or an
`Outcome(passed, defect_label, confidence)` directly. The images saved under
`data/images/` are the training data.

## Test procedure

1. **Laptop or `--sim`:** in Settings select the Camera inspector and the
   Synthetic source, Start, press Simulate can (or Space). Check live detection, results, KPIs,
   saved images and the log. Try E-stop / Reset / Ack alarm.
2. **Pi, air supply OFF:**
   - At idle only yellow is on and the relay 4 LED is **off**.
   - Manual override: click each lamp in the stack-light drawing; check that
     the right relay and lamp switch.
   - Test reject burst: relay 4 clicks 3 times.
   - Wave a can at the sensor: the trigger sensor indicator shows CAN PRESENT.
   - Camera panel shows Online.
3. **Air ON:** turn on the single-shot latch, then run *Always reject* with real
   cans and tune **Reject delay** until the air hits the can.
4. **Safety:** during a run press E-stop (valve locked, solid red); close the
   app (all relays off).

