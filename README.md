# Yard's Brewing Company AI Camera Inspection System

## Project Overview

This project focuses on developing a **low-cost AI camera inspection solution** for **Yard's Brewing Company** to automatically **detect and reject defective cans** on the production line.

The current budget-friendly approach leverages **OpenCV image processing techniques** to analyze can flanges and identify defects. The system converts camera frames into binary images and applies various edge detection and measurement methods to compare the detected can flange against the profile of a **perfectly round can flange**. Any significant deviation may indicate a defect such as dents, deformation, or damage. This solution will be compare to an advanced, industrial solution using Keyence hardware.

---

## Repository Structure

### `yoloClassification/`

This directory contains the scripts and resources required for training and evaluating an initial **YOLO-based classification model**.

#### Dataset Requirements
The scripts rely on a `dataset` directory containing the following subdirectories:

- `pass/` - Images of acceptable cans
- `fail/` - Images of defective cans

Due to the large dataset size, these folders are not stored in GitHub and must be manually downloaded from the project's **Teams2 channel**.

#### Scripts

##### `datasetSplit.py`
Prepares the dataset by creating training and testing splits for YOLO classification.

**Note:** This initial implementation is designed for images containing **a single can per frame**.

##### `yoloTraining.py`
Trains the YOLO classification model using the prepared dataset and generates the trained model file.

##### `yoloTesting.py`
Evaluates the trained YOLO model on the testing dataset and reports performance metrics such as classification accuracy and prediction results.

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

This script represents an evolution toward a complete real-time inspection application.

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
