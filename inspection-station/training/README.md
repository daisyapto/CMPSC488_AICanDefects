# Training the pass / fail can classifier

This is the team's **Yolo+CV** work (YOLO11n-cls, pass / fail), wired into the station:
the station collects and labels the images, this folder trains the model, and the station
runs it (Settings → Detection engine → **YOLO**).

```
station saves images ──► Review page: mark each one right / wrong ──► "Export pass / fail set"
        ▲                                                                       │
        │                                                                       ▼
  models/can_classifier.pt  ◄──── python train_yolo.py ◄──── can_dataset/ (unzip here)
```

## 1. Collect images on the station
* Settings → Camera → **Save images: All** (not just fails) while you run cans.
* Run good cans **and** damaged cans, under the real light, at the real belt position.
  Cans land in different places across the frame - that is fine, the model has to learn it.
* Aim for **200+ per class** for training and **50+ per class** for checking. The first model
  (`models/can_classifier.pt`) was trained on 13 images and checked on 5: its "100 %" says
  almost nothing yet.

## 2. Label them
Review page → open each image → **✓ station was right** or **✕ False reject / Missed defect**.
Only reviewed images are exported; the human verdict decides pass / fail, not the station's.
Then **Export pass / fail set** → `can_dataset_<date>.zip`
(`train/` 80 %, `val/` 20 %, each with `pass/` and `fail/`).

## 3. Train (laptop or PC, not the Pi)
```
cd inspection-station/training
python -m venv .venv  &&  .venv\Scripts\activate        (Windows)   /  source .venv/bin/activate
pip install -r requirements.txt
unzip the export here → training/can_dataset/   (merge new exports into it as you collect more)
python train_yolo.py                  # → ../models/can_classifier.pt
python evaluate.py --images can_dataset/val
```
`evaluate.py` decides exactly like the station (fail score ≥ threshold → FAIL), lists every
missed defect and false reject, and suggests a threshold. Keep a `can_dataset/test/` set of
images the model never trains on and check every new model on it.

Add `--ncnn` to also export `models/can_classifier_ncnn_model/` - the fastest format on the Pi CPU.

## 4. Use it on the station
1. Push `models/can_classifier.pt` (it is in git - small) and `git pull` on the Pi, or copy it there.
2. On the Pi once: `pip install --break-system-packages ultralytics`
3. Settings → Detection → engine **YOLO**, model `models/can_classifier.pt`,
   **Min confidence 60 %** (= the fail score that rejects; the old `FAIL_THRESHOLD = 0.60`),
   **Crop to the can first: off** (the model was trained on full frames).
4. The engine line shows the model, its classes and size, and the time per can.
   The last-inspected card shows the fail score of every can.

The Pi's CPU is fast enough for this small classifier (~tens of ms at 224 px).

## About the OpenCV roundness check (`inspect_can.py`)
It is left out on purpose. It looks for a near-perfect circle near the image centre, but this
camera sees the can from an angle (the rim is an ellipse) and cans stop anywhere across the
frame. Run on the 18 captured images it gave REVIEW for 12 and FAIL for the other 6 -
including both good cans in `val/pass` - so combined with YOLO every can would be FAIL or REVIEW.
It needs a top-down view (or an ellipse fit and calibrated limits) before it can help.
