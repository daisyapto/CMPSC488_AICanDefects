"""
Train the pass / fail can classifier (YOLO11n-cls) - the team's Yolo+CV training script,
set up to feed the inspection station.

    pip install -r requirements.txt
    python train_yolo.py                       # uses ./can_dataset
    python train_yolo.py --data path/to/can_dataset --epochs 150 --ncnn

Dataset layout (the station's Review page -> "Export pass / fail set" makes exactly this):
    can_dataset/train/pass  can_dataset/train/fail
    can_dataset/val/pass    can_dataset/val/fail
    can_dataset/test/...    (optional, never used for training)

Result: ../models/can_classifier.pt (+ ../models/can_classifier_ncnn_model with --ncnn).
Copy it to the same place on the Pi, then Settings -> Detection engine -> YOLO.
"""
import argparse
import shutil
from pathlib import Path

from ultralytics import YOLO

ROOT = Path(__file__).resolve().parent
STATION_MODELS = ROOT.parent / "models"
EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}

ap = argparse.ArgumentParser()
ap.add_argument("--data", default=str(ROOT / "can_dataset"))
ap.add_argument("--epochs", type=int, default=100)
ap.add_argument("--imgsz", type=int, default=224, help="model input size; the station runs the model at this size")
ap.add_argument("--batch", type=int, default=16)
ap.add_argument("--device", default="cpu", help="cpu, or 0 for a CUDA GPU")
ap.add_argument("--name", default="can_classifier", help="output name in ../models")
ap.add_argument("--ncnn", action="store_true", help="also export NCNN (fastest on the Raspberry Pi CPU)")
args = ap.parse_args()

data = Path(args.data).resolve()


def count(folder: Path) -> int:
    return sum(p.suffix.lower() in EXTS for p in folder.iterdir()) if folder.exists() else 0


small = False
for split in ("train", "val"):
    for label in ("pass", "fail"):
        n = count(data / split / label)
        print(f"{split}/{label}: {n} images")
        if n == 0:
            raise SystemExit(f"Add labelled images to {data / split / label}")
        small |= n < (50 if split == "train" else 15)
if small:
    print("\nWARNING: very few images. Accuracy measured on a handful of val images means little -\n"
          "aim for 200+ per class in train and 50+ in val, taken on the line under the real light.\n")

model = YOLO("yolo11n-cls.pt")          # downloaded automatically the first time
model.train(
    data=str(data),
    epochs=args.epochs,
    imgsz=args.imgsz,
    batch=args.batch,
    patience=20,
    project=str(ROOT / "runs_can"),
    name="yards_classifier",
    exist_ok=True,
    device=args.device,
)

best = ROOT / "runs_can" / "yards_classifier" / "weights" / "best.pt"
if not best.exists():
    raise SystemExit("Training did not produce best.pt")

STATION_MODELS.mkdir(exist_ok=True)
out = STATION_MODELS / f"{args.name}.pt"
shutil.copy2(best, out)
print("\nModel saved to:", out)

if args.ncnn:
    folder = Path(YOLO(str(out)).export(format="ncnn", imgsz=args.imgsz))
    target = STATION_MODELS / f"{args.name}_ncnn_model"
    shutil.rmtree(target, ignore_errors=True)
    shutil.move(str(folder), target)
    print("NCNN model saved to:", target)

if any((data / "test").rglob("*.*")):
    metrics = YOLO(str(out)).val(data=str(data), split="test", imgsz=args.imgsz, device=args.device)
    print(f"Test accuracy: {metrics.top1:.4f}")
else:
    print("No test images - run evaluate.py on images the model has never seen.")
