# Yard's Brewing Company AI Camera Inspection System

## Prototype

- Hosted prototype (Eric) (index.html -- version 3) https://daisyapto.github.io/CMPSC488_AICanDefects/
- Another version (Andrew): simulators/ai_can_inspection_digital_twin.html
- Another version (Anatoly): simulators/Inspector_Simulator.html

## Project Overview

This project focuses on developing a **low-cost AI camera inspection solution** for **Yard's Brewing Company** to automatically **detect and reject defective cans** on the production line.

The current budget-friendly approach leverages **OpenCV image processing techniques** to analyze can flanges and identify defects. The system converts camera frames into binary images and applies various edge detection and measurement methods to compare the detected can flange against the profile of a **perfectly round can flange**. Any significant deviation may indicate a defect such as dents, deformation, or damage. This solution will be compared to an advanced, industrial solution using Keyence hardware.

The budget station runs on a **Raspberry Pi 5** with a **Teledyne DALSA Genie Nano** (GigE) camera, a trigger sensor, a green/yellow/red stack light and a pneumatic reject valve (see *Hardware* below). The `inspection-station/` folder (branch `inspection-station`) ties all of these together into one line controller with a web dashboard. The branch `final` is the most-updated, current version of the system using can_inspection_station_x.py

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

## Folder Layout

```
index.html                  hosted prototype (GitHub Pages - keep at the root)
reorganize_repo.py          script that restructured the repo's directory organization
archive/                    old, unused scripts kept for reference
  InspectionLine.py           old, unused script UI
  testScript1.py              old YOLO loading with a live webcam view
data/captures/              sample images from the small test conveyor
  Defect/Defect/              sample defective can images
  Good/Good/                  sample good can images
data_tools/                 frame capture and video-to-frames tools
  framesCR.py                 saves frames from a live timed feed
  video_to_frames.py          converts an .mp4 into individual frames
hardware_tests/             stack light, GPIO sensor, air reject and camera bring-up scripts
  AirTest.py                  old photoelectric sensor test (previous GPIO references)
  GPIOTest.py                 small unit test of GPIO 17
  SensorToCam.py              opens the camera when GPIO 24 is active (old GPIO assignment)
  StackLight.py               small UI that controls the stack light through GPIO
  module2.py                  small test scripts (in comments) for the camera live view
simulators/                 browser prototypes of the inspection line (module 2 design phase)
  Inspector_Simulator.html
  ai_can_inspection_digital_twin.html
station/                    integrated inspection station UIs
  can_inspection_station_1.py original working UI
  can_inspection_station_2.py second working UI, adds a live log feed
vision_opencv/              OpenCV defect detection scripts
  defectDetection_module3.py
  edgeDetection.py
  edgeFaultDetection.py
  module1.py
yolo/                       YOLO training and testing
  classification/             original pass/fail classification scripts
    datasetSplit.py
    yoloTesting.py
    yoloTraining.py
  models/best.pt              current trained YOLOv11 object detection model
  results/val/                training/validation images
  pt_file_test3.py            tests best.pt on unseen images
  train.py                    trained best.pt
```

Install the Python packages with `pip install -r requirements.txt` (see the file for PySpin and gpiod).

---

## Repository Structure

### `station/`

The integrated inspection station UIs, which bring the live camera feed, trained YOLO detections, sensor and stack light together.

#### `station/can_inspection_station_1.py`
The original working UI. It shows a live feed with trained **YOLOv11** detections, allows automatic or manual control of the stack light, and shows the photoelectric sensor value.

#### `station/can_inspection_station_2.py`
The second working UI version. It adds a live log feed.

---

### `inspection-station/` (branch `inspection-station`)

The integrated line controller and operator dashboard. It combines the hardware test scripts (`hardware_tests/StackLight.py`, `hardware_tests/GPIOTest.py`, `hardware_tests/AirTest.py`) with the camera and image processing into one application:

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

### `hardware_tests/`

#### `hardware_tests/StackLight.py`
Small UI that controls the stack light relays (green / yellow / red) by hand through GPIO (`pinctrl`).

#### `hardware_tests/GPIOTest.py`
Small unit test of GPIO 17. It monitors the input with `pinctrl get` and prints its state changes.

#### `hardware_tests/AirTest.py`
Old script with previous GPIO references, used to test that the photoelectric sensor is working properly. It watches the trigger sensor and fires a short pneumatic burst (3 x 40 ms) on the reject solenoid.
**Note:** the reject valve is now on relay 4 of the active-low relay board, where LOW = valve open - this script's "LOW = safe" assumption no longer holds. Use `inspection-station` (or invert the levels) for air tests.

#### `hardware_tests/SensorToCam.py`
Opens the camera when GPIO 24 is active from the sensor (old GPIO assignment). It waits for the trigger sensor (via `gpiod`) and captures a frame from the camera with PySpin.

#### `hardware_tests/module2.py`
Small test scripts (in comments) to connect to the Teledyne GigE Nano Genie (budget solution) camera and test its live view.

---

### `yolo/`

#### `yolo/models/best.pt`

The current trained model. It is a **YOLOv11 object detection** model (not classification), trained on bounding-box annotated data from the industrial conveyor belt: around 550 training images (not in the repo) captured via SpinView and converted to frames with `data_tools/video_to_frames.py`.

A trained model can be run on the line by the inspection station's **YOLO** engine (Settings > Detection; put the model file in `inspection-station/models/`).

#### `yolo/train.py`
Trained `best.pt`.

#### `yolo/pt_file_test3.py`
Tests multiple images by drawing bounding boxes on the defects found in unseen images.

#### `yolo/results/val/`
Shows all the images from training/validation.

#### `yolo/classification/`

This directory contains the scripts and resources for the initial **YOLO-based classification model**. This approach goes off an entire frame, so it is not suitable for our needs and was replaced by the object detection model above.

##### Dataset Requirements
The scripts rely on a `dataset` directory containing the following subdirectories:

- `pass/` - Images of acceptable cans
- `fail/` - Images of defective cans

Due to the large dataset size, these folders are not stored in GitHub and must be manually downloaded from the project's **Teams2 channel**.

The inspection station's Review page can also export labelled images from the line (`train/` and `val/` folders per class) for training.

##### Scripts

###### `datasetSplit.py`
Organizes the dataset given in `data/captures` and prepares it by creating training and testing splits for YOLO classification.

**Note:** This initial implementation is designed for images containing **a single can per frame**.

###### `yoloTraining.py`
Trains the original YOLO classification model using the prepared dataset and generates the trained model file.

###### `yoloTesting.py`
Tests the classification YOLO model on the testing dataset and reports performance metrics such as classification accuracy and prediction results.

---

### `vision_opencv/`

#### `vision_opencv/edgeDetection.py`

One of our original OpenCV detection scripts, using multiple stacked OpenCV tools. It is also quite sensitive.

This script demonstrates the complete OpenCV image-processing pipeline by displaying multiple visualization windows that show how a live camera frame is transformed throughout each processing stage.

**Current limitations:**
- Highly sensitive to lighting and image conditions
- Bounding boxes around detected defects are not consistently accurate
- Additional tuning is required for production deployment

#### `vision_opencv/edgeFaultDetection.py`

Builds upon the edge detection approach by comparing against a baseline reference to do live defect detection with OpenCV, adding inspection-related functionality, including:

- Defect measurement calculations
- Inspection annotations
- Pass/Fail determination
- Live camera overlays and visual feedback

This script represents an evolution toward a complete real-time inspection application. Its detection can be moved into `inspection-station/team_vision.py` (a `detect(image)` function that returns the defects found) to run it on the line with the real trigger, reject and stack light.

#### `vision_opencv/defectDetection_module3.py`

Simple live defect detection using Sobel imaging and contour measurement, with the results displayed on the live feed. Not very accurate.

#### `vision_opencv/module1.py`

Similar to `edgeDetection.py`, with some simplifications to the stacked tools. It was adapted to work with the PySpin/Spinnaker library to connect to the Teledyne GigE Nano Genie (budget solution) camera.

Its primary purpose is to establish communication with the **Teledyne GigE Camera**, open a live camera feed, run image processing, and reach an output. **NOT YET WORKING**

Some OpenCV tools are initially tested here but they need further adaption with thresholds and testing to ensure a working budget solution.

The inspection station connects to the Genie Nano through Aravis instead (tested with live video on a laptop).

---

### `data_tools/`

#### `data_tools/framesCR.py`

Captures and saves image frames from a live, timed camera feed (approximately **7 seconds**).

The collected images are primarily used for:

- Building datasets for **RoboFlow**
- Training the YOLO models
- General data collection and testing

#### `data_tools/video_to_frames.py`

Converts an `.mp4` video into individual frames, with a choice of image formats and of saving every N frames.

---

### `data/captures/`

Sample images from the small test conveyor (not true training images from the industrial conveyor):

- `Defect/Defect/` - sample images of defective cans
- `Good/Good/` - sample images of good cans

---

### `simulators/`

Browser prototypes of the inspection line, other versions of our prototype from the module 2 design phase:

- `Inspector_Simulator.html`
- `ai_can_inspection_digital_twin.html`

---

### `archive/`

Old, unused scripts kept for reference.

#### `archive/InspectionLine.py`
An old, unused script UI. First single-file version of the integrated controller (Tkinter dashboard), superseded by `station/` and `inspection-station/`.

#### `archive/testScript1.py`
An old YOLO loading script with a live webcam view.

---

### Root files

- `index.html` - the hosted prototype
- `reorganize_repo.py` - restructured the directory organization of the repo so old, unused files were properly sorted and new files had somewhere to be added

---


## Current Development Direction

The project is in the last week of the module 3 development phase. Current focus:

- Wrap up connecting the components into the UI script by working on `can_inspection_station_3.py`
- Work with the rejector system: add it to the UI script and determine the best way to access it
- Fix the sensor detecting separate cans and counting them as separate objects even when they are very close together
- Using final branch as current/most-updated version

The project is also exploring a primary inspection approach:

1. **Traditional Computer Vision (OpenCV) & YOLO**
   - Edge detection
   - Binary thresholding
   - Geometric measurements
   - Flange comparison against a known good can
   - Pass/Fail image classification
   - Dataset-driven defect detection
   - Performance evaluation using labeled can images

**Integration:** the budget solution's hardware (Raspberry Pi, Genie Nano, sensor, stack light, reject valve) runs from `inspection-station/`. The OpenCV detection (`team_vision.py`) and YOLO models plug into it, so each approach can be tested on the real line and compared on the same labelled images.


References / Important Links:
- https://www.etechnophiles.com/raspberry-pi-5-gpio-pinout/
- Aravis (GigE Vision library used for the Genie Nano): https://github.com/AravisProject/aravis
- Genie Nano user manual (LED meanings, IP setup, power): https://www.1stvision.com/cameras/dalsa/dataman/Nano_Series_User.pdf
