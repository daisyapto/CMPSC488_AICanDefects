# Yard's Brewing Company AI Camera Inspection System

## Prototype

- Hosted prototype (Eric) (index.html -- version 3) https://daisyapto.github.io/CMPSC488_AICanDefects/
- Another version (Andrew): ai_can_inspection_digital_twin.html
- Another version (Anatoly): Inspect_Simulator.html

## Project Overview

This project focuses on developing a **low-cost AI camera inspection solution** for **Yard's Brewing Company** to automatically **detect and reject defective cans** on the production line.

The current budget-friendly approach leverages **OpenCV image processing techniques** to analyze can flanges and identify defects. The system converts camera frames into binary images and applies various edge detection and measurement methods to compare the detected can flange against the profile of a **perfectly round can flange**. Any significant deviation may indicate a defect such as dents, deformation, or damage. This solution will be compared to an advanced, industrial solution using Keyence hardware.

The budget station runs on a **Raspberry Pi 5** with a **Teledyne DALSA Genie Nano** (GigE) camera, a trigger sensor, a green/yellow/red stack light and a pneumatic reject valve (see *Hardware* below). The `inspection-station/` folder (branch `inspection-station`) ties all of these together into one line controller with a web dashboard.

---

## Hardware (budget station)

| Part | Connection |
|---|---|
| Raspberry Pi 5 | official 27 W USB-C supply |
| Teledyne DALSA Genie Nano (GigE Vision) | Ethernet to the Pi; powered by a PoE injector (802.3af) or 10-36 V on its 10-pin connector |
| Trigger sensor (5 V) | signal through a resistor divider (5 V -> 3.3 V) to GPIO24 (header pin 18) |
| 4-channel relay board (active-low) | relay 1 green (GPIO17, pin 11), relay 2 yellow (GPIO27, pin 13), relay 3 red (GPIO22, pin 15), relay 4 reject solenoid (GPIO23, pin 16) |
| Camera rail | manually adjusted |

---

## Repository Structure

### `inspection-station/` (branch `inspection-station`)

The integrated line controller and operator dashboard. It combines the hardware test scripts (`StackLight.py`, `GPIOTest.py`, `AirTest.py`) with the camera and image processing into one application:

- trigger sensor -> camera capture -> inspection -> timed air reject (with fail-safe: a can that can't be inspected is rejected)
- stack light: green = running, yellow = defect (flashing = unusual defect rate), red = fault until fixed
- Genie Nano through **Aravis** (open-source GigE Vision library) - no Teledyne software needed; `find_camera.py` finds the camera and its IP, `camera_bridge.py` gives live video on Windows
- detection engines: built-in OpenCV pipeline, **`team_vision.py`** (a slot for the team's own OpenCV code - e.g. the edge detection below), or a trained **YOLO** model
- web dashboard (works on the Pi screen, a laptop or a phone), big line display for a monitor next to the line, recipes per can size, operator/engineer access, image review + dataset export for training, batch reports, alarms with troubleshooting, diagnostics (belt speed, timing, self-test), production history
- `pi_setup.sh` / `pi_check.py` to set up and verify the Raspberry Pi

Two editions are built from the same code: **lite** (no YOLO, fine without the Pi's active cooler) and **full** (with YOLO). See `inspection-station/README.md` for running it on a laptop, wiring, camera setup and moving to the Pi.

```bash
cd inspection-station
python run.py --sim        # on a laptop: simulation, opens the dashboard at http://localhost:8080
```

---

### Hardware test scripts

#### `StackLight.py`
Tkinter control panel to switch the stack light relays (green / yellow / red) by hand through `pinctrl`.

#### `GPIOTest.py`
Monitors a GPIO input with `pinctrl get` and prints its state changes - used to test the trigger sensor wiring.

#### `AirTest.py`
Watches the trigger sensor and fires a short pneumatic burst (3 x 40 ms) on the reject solenoid.
**Note:** the reject valve is now on relay 4 of the active-low relay board, where LOW = valve open - this script's "LOW = safe" assumption no longer holds. Use `inspection-station` (or invert the levels) for air tests.

#### `InspectionLine.py`
First single-file version of the integrated controller (Tkinter dashboard). Superseded by `inspection-station/`.

#### `SensorToCam.py`
Waits for the trigger sensor (via `gpiod`) and captures a frame from the camera with PySpin.

---

### `yoloClassification/`

This directory contains the scripts and resources required for training and evaluating an initial **YOLO-based classification model**.

#### Dataset Requirements
The scripts rely on a `dataset` directory containing the following subdirectories:

- `pass/` - Images of acceptable cans
- `fail/` - Images of defective cans

Due to the large dataset size, these folders are not stored in GitHub and must be manually downloaded from the project's **Teams2 channel**.

The inspection station's Review page can also export labelled images from the line (`train/` and `val/` folders per class) for training.

#### Scripts

##### `datasetSplit.py`
Prepares the dataset by creating training and testing splits for YOLO classification.

**Note:** This initial implementation is designed for images containing **a single can per frame**.

##### `yoloTraining.py`
Trains the YOLO classification model using the prepared dataset and generates the trained model file.

##### `yoloTesting.py`
Evaluates the trained YOLO model on the testing dataset and reports performance metrics such as classification accuracy and prediction results.

A trained model can be run on the line by the inspection station's **YOLO** engine (Settings > Detection; put the model file in `inspection-station/models/`).

---

### `edgeDetection.py`

One of the most successful early prototype solutions for defect detection.

This script demonstrates the complete OpenCV image-processing pipeline by displaying multiple visualization windows that show how a live camera frame is transformed throughout each processing stage.

**Current limitations:**
- Highly sensitive to lighting and image conditions
- Bounding boxes around detected defects are not consistently accurate
- Additional tuning is required for production deployment

---

### `edgeFaultDetection.py`

Builds upon the edge detection approach by adding inspection-related functionality, including:

- Defect measurement calculations
- Inspection annotations
- Pass/Fail determination
- Live camera overlays and visual feedback

This script represents an evolution toward a complete real-time inspection application. Its detection can be moved into `inspection-station/team_vision.py` (a `detect(image)` function that returns the defects found) to run it on the line with the real trigger, reject and stack light.

---

### `framesCR.py`

Captures and saves image frames from the camera over a period of approximately **7 seconds**.

The collected images are primarily used for:

- Building datasets for **RoboFlow**
- Training the YOLO classification model
- General data collection and testing

---

### `testScript1.py`

The first proof-of-concept script developed for the project.

Its primary purpose was to establish communication with the **ELS camera system**, open a live camera feed, and verify camera operation.

Some early OpenCV tools and experiments were initially tested here before being migrated into `edgeDetection.py` as the project evolved.

---

### `module1.py`

The edgeDetection.py adapted to work with PySpin/Spinnaker library to connect to Teledyne GigE Nano Genie (budget solution) camera.

Its primary purpose is to establish communication with the **Teledyne GigE Camera**, open a live camera feed, run image processing, and reach an output. **NOT YET WORKING**

Some OpenCV tools are initially tested here but they need further adaption with thresholds and testing to ensure a working budget solution.

The inspection station connects to the Genie Nano through Aravis instead (tested with live video on a laptop).

---

### `module2.py`

The sample test script(s) to connect to Teledyne GigE Nano Genie (budget solution) camera with a live camera feed.

---


## Current Development Direction

The project is currently exploring two primary inspection approaches:

1. **Traditional Computer Vision (OpenCV) & YOLO**
   - Edge detection
   - Binary thresholding
   - Geometric measurements
   - Flange comparison against a known good can
   - Pass/Fail image classification
   - Dataset-driven defect detection
   - Performance evaluation using labeled can images

2. **AI-Based Keyence**
   - Pass/Fail image classification & object detection
   - Advanced measurement on can flanges
   - Minimal dataset-driven defect detection
   - Performance evaluation using labeled can images

The OpenCV-based approach currently serves as the primary budget solution, while Keyence continues to be evaluated as a complementary AI-driven inspection method.

**Integration:** the budget solution's hardware (Raspberry Pi, Genie Nano, sensor, stack light, reject valve) runs from `inspection-station/`. The OpenCV detection (`team_vision.py`) and YOLO models plug into it, so each approach can be tested on the real line and compared on the same labelled images.


References / Important Links:
- https://www.etechnophiles.com/raspberry-pi-5-gpio-pinout/
- Aravis (GigE Vision library used for the Genie Nano): https://github.com/AravisProject/aravis
- Genie Nano user manual (LED meanings, IP setup, power): https://www.1stvision.com/cameras/dalsa/dataman/Nano_Series_User.pdf
