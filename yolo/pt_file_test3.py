"""
test_model.py - Basic check that the YOLO .pt model loads on the Raspberry Pi,
and print the training/validation info saved inside it.

Usage:
    python test_model.py                              (uses ~/Downloads/best.pt)
    python test_model.py best.pt some_can_photo.jpg   (optional: test on a real image)

It also runs the model on every image in the trained_test_2_small_frames_minibatch
folder (looked for in the current folder, then in ~/Downloads).
"""

import os
import sys

# ---- 1. Check that the libraries are installed ----
print("Checking libraries...")
try:
    import torch
    print(f"  torch        OK  (version {torch.__version__})")
except ImportError:
    sys.exit("  torch is missing -> run: pip install ultralytics")

try:
    import ultralytics
    from ultralytics import YOLO
    print(f"  ultralytics  OK  (version {ultralytics.__version__})")
except ImportError:
    sys.exit("  ultralytics is missing -> run: pip install ultralytics")

try:
    import cv2
    print(f"  opencv       OK  (version {cv2.__version__})")
except ImportError:
    print("  opencv missing (ultralytics normally installs it) -> pip install opencv-python")

import numpy as np


# Friendlier names for the metrics Ultralytics saves
METRIC_LABELS = {
    "metrics/precision(B)":  "Precision",
    "metrics/recall(B)":     "Recall",
    "metrics/mAP50(B)":      "mAP@50",
    "metrics/mAP50-95(B)":   "mAP@50-95",
    "metrics/accuracy_top1": "Top-1 accuracy",
    "metrics/accuracy_top5": "Top-5 accuracy",
    "fitness":               "Fitness score",
}


def print_model_info(model, model_path):
    """Print the general model info plus whatever training metrics are stored in the .pt."""
    ckpt = getattr(model, "ckpt", None) or {}
    train_args = ckpt.get("train_args") or {}

    print("\n================ MODEL INFO ================")
    print(f"  File        : {model_path}  ({os.path.getsize(model_path) / 1e6:.1f} MB)")
    print(f"  Task type   : {model.task}")
    print(f"  Num classes : {len(model.names)}")
    print(f"  Classes     : {model.names}")
    num_params = sum(p.numel() for p in model.model.parameters())
    print(f"  Parameters  : {num_params:,}")

    if ckpt.get("date"):
        print(f"  Trained on  : {ckpt['date'][:19].replace('T', ' ')}")
    if ckpt.get("version"):
        print(f"  Trained with: ultralytics {ckpt['version']}")

    # Training settings
    if train_args:
        print("\n  -- Training settings --")
        for key in ("model", "epochs", "imgsz", "batch", "optimizer", "patience"):
            if key in train_args:
                print(f"  {key:<11} : {train_args[key]}")

    # Final validation metrics (from the best epoch)
    metrics = ckpt.get("train_metrics") or {}
    if metrics:
        print("\n  -- Validation metrics (best model) --")
        for key, value in metrics.items():
            if key.startswith("val/"):
                continue  # losses are printed separately below
            label = METRIC_LABELS.get(key, key)
            print(f"  {label:<15}: {value:.4f}" + (f"  ({value * 100:.1f}%)" if value <= 1 else ""))

        losses = {k: v for k, v in metrics.items() if k.startswith("val/")}
        if losses:
            print("\n  -- Validation losses (lower is better) --")
            for key, value in losses.items():
                print(f"  {key.replace('val/', ''):<15}: {value:.4f}")
    else:
        print("\n  No validation metrics saved in this file.")
        print("  (Some exported/Roboflow weights don't include them.)")

    # How many epochs actually ran (training may stop early)
    results = ckpt.get("train_results") or {}
    if results.get("epoch"):
        print(f"\n  Epochs run  : {len(results['epoch'])}")

    print("============================================")


# ---- 2. Load the .pt file ----
model_path = os.path.expanduser("~/Downloads/best.pt")   # ~ = your home folder, e.g. /home/pi
if len(sys.argv) >= 2:
    model_path = sys.argv[1]

print(f"\nLoading model: {model_path}")
try:
    model = YOLO(model_path)
except Exception as e:
    sys.exit(f"  FAILED to load model: {e}")

print("  Model loaded!")
print_model_info(model, model_path)

# ---- 3. Run one prediction ----
if len(sys.argv) >= 3:
    source = sys.argv[2]
    print(f"\nRunning prediction on: {source}")
else:
    # No image given: use a blank gray image just to prove inference runs
    source = np.full((640, 640, 3), 128, dtype=np.uint8)
    print("\nRunning prediction on a blank test image...")

results = model.predict(source, verbose=False)
r = results[0]
print(f"  Inference time: {r.speed['inference']:.1f} ms")

if r.probs is not None:  # classification model
    top = r.probs.top1
    print(f"  Prediction: {model.names[top]} ({r.probs.top1conf:.2f} confidence)")
elif r.boxes is not None:  # detection model
    print(f"  Detections: {len(r.boxes)}")
    for box in r.boxes:
        name = model.names[int(box.cls)]
        print(f"    - {name}  ({float(box.conf):.2f})")


# ---- 4. Run the model on every frame in the test folder ----
TEST_DIR_NAME = "trained_test_2_small_frames_minibatch"
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp")

# Look for the folder where you run the script, then in ~/Downloads
test_dir = None
for candidate in (TEST_DIR_NAME, os.path.expanduser(f"~/Downloads/{TEST_DIR_NAME}")):
    if os.path.isdir(candidate):
        test_dir = candidate
        break

if test_dir is None:
    print(f"\nTest folder '{TEST_DIR_NAME}' not found (checked here and ~/Downloads). Skipping.")
else:
    frames = sorted(f for f in os.listdir(test_dir) if f.lower().endswith(IMAGE_EXTS))
    print(f"\n============ TESTING FRAMES ============")
    print(f"  Folder: {test_dir}  ({len(frames)} images)")

    class_counts = {name: 0 for name in model.names.values()}
    total_time = 0.0

    # Annotated frames (with boxes drawn) are saved here
    out_dir = test_dir.rstrip("/") + "_annotated"
    os.makedirs(out_dir, exist_ok=True)

    # Only try to open windows if the Pi has a screen/desktop (not plain SSH)
    show_window = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if show_window:
        print("  Showing frames: press any key for next frame, 'q' to stop showing")
    else:
        print("  No display found - saving annotated frames only")

    for i, filename in enumerate(frames, start=1):
        path = os.path.join(test_dir, filename)
        r = model.predict(path, verbose=False)[0]
        total_time += r.speed["inference"]

        if r.probs is not None:  # classification: one label per frame
            name = model.names[r.probs.top1]
            class_counts[name] += 1
            summary = f"{name} ({float(r.probs.top1conf):.2f})"
        else:  # detection: list every box found
            found = []
            for box in r.boxes:
                name = model.names[int(box.cls)]
                class_counts[name] += 1
                found.append(f"{name} ({float(box.conf):.2f})")
            summary = ", ".join(found) if found else "nothing detected"

        print(f"  [{i}/{len(frames)}] {filename}: {summary}")

        # Draw the boxes + labels + confidence onto the frame and save it
        annotated = r.plot()   # image (BGR) with boxes drawn on
        cv2.imwrite(os.path.join(out_dir, filename), annotated)

        if show_window:
            try:
                cv2.imshow("Can defect detection", annotated)
                key = cv2.waitKey(0) & 0xFF   # wait for a key press
                if key == ord("q"):
                    show_window = False
                    cv2.destroyAllWindows()
            except cv2.error:
                print("  (Can't open a window - saving frames only)")
                show_window = False

    if frames:
        label = "Frames per class" if model.task == "classify" else "Detections per class"
        print(f"\n  -- {label} --")
        for name, count in class_counts.items():
            print(f"  {name:<15}: {count}")
        print(f"\n  Avg inference time: {total_time / len(frames):.1f} ms per frame")
        print(f"  Annotated frames saved to: {out_dir}")
    cv2.destroyAllWindows()
    print("========================================")

print("\nAll good - the model works on this machine.")
