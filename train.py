"""
Train YOLOv11 using the existing Roboflow data.yaml and report validation metrics.

Expected layout:
    Dataset_annotated/
        data.yaml        <- already provided by Roboflow
        train/...
        valid/...

Install:  pip install ultralytics
Run:      python train_yolo11.py
"""

import shutil
from pathlib import Path

import torch
from ultralytics import YOLO

# ----------------------------- CONFIG ---------------------------------
DATA_YAML = Path("Dataset_annotated/data.yaml").resolve()
BASE_WEIGHTS = "yolo11n.pt"       # n / s / m / l / x  (bigger = more accurate, slower)
EPOCHS = 100
IMG_SIZE = 640
BATCH = 16                        # lower this if you run out of GPU memory
PATIENCE = 20                     # early stop if no improvement for N epochs
OUTPUT_MODEL = "yards_yolov11.pt"
# ----------------------------------------------------------------------


def main():
    device = 0 if torch.cuda.is_available() else "cpu"
    print(f"Using device: {device}")
    print(f"Using dataset config: {DATA_YAML}")

    # ---- Train ----
    model = YOLO(BASE_WEIGHTS)
    results = model.train(
        data=str(DATA_YAML),
        epochs=EPOCHS,
        imgsz=IMG_SIZE,
        batch=BATCH,
        patience=PATIENCE,
        device=device,
        project="runs",
        name="yolo11_train",
        exist_ok=True,
    )

    # ---- Save best weights as yolov11.pt ----
    best_weights = Path(results.save_dir) / "weights" / "best.pt"
    shutil.copy(best_weights, OUTPUT_MODEL)
    print(f"\nSaved trained model to: {Path(OUTPUT_MODEL).resolve()}")

    # ---- Validate on the validation set ----
    best_model = YOLO(OUTPUT_MODEL)
    metrics = best_model.val(data=str(DATA_YAML), split="val", imgsz=IMG_SIZE, device=device)

    print("\n=========== VALIDATION METRICS ===========")
    print(f"Precision     : {metrics.box.mp:.4f}")
    print(f"Recall        : {metrics.box.mr:.4f}")
    print(f"mAP@0.50      : {metrics.box.map50:.4f}")
    print(f"mAP@0.50:0.95 : {metrics.box.map:.4f}")

    print("\nPer-class results (P / R / mAP50 / mAP50-95):")
    for i, cls_idx in enumerate(metrics.box.ap_class_index):
        p, r, ap50, ap = metrics.box.class_result(i)
        print(f"  {metrics.names[cls_idx]:<20} {p:.3f} / {r:.3f} / {ap50:.3f} / {ap:.3f}")

    print(f"\nPlots (confusion matrix, PR curve, results.png) are in: {results.save_dir}")


if __name__ == "__main__":
    main()