# Code ref: Copilot
# Testing shows decent results,
# however since classifier,
# only works on identifying a frame as pass/fail but does not track object

# Results of classification:
# Total Images : 65
# Correct      : 64
# Incorrect    : 1
# Accuracy     : 98.46%

from ultralytics import YOLO
from pathlib import Path

model = YOLO("runs/classify/pass-fail-classifier/weights/best.pt")

metrics = model.val(
    data="dataset",
    split="test",
)

print(metrics)

test_dir = Path("dataset/test")

correct = 0
total = 0

for true_class_dir in test_dir.iterdir():

    if not true_class_dir.is_dir():
        continue

    true_class = true_class_dir.name

    for img_path in true_class_dir.glob("*"):

        if not img_path.is_file():
            continue

        result = model(str(img_path))[0]

        pred_idx = result.probs.top1
        pred_class = result.names[pred_idx]
        confidence = float(result.probs.top1conf)

        is_correct = pred_class == true_class

        if is_correct:
            correct += 1

        total += 1

        print(
            f"{img_path.name:<40} "
            f"Actual={true_class:<6} "
            f"Pred={pred_class:<6} "
            f"Conf={confidence:.4f} "
            f"{'✓' if is_correct else '✗'}"
        )

accuracy = correct / total if total > 0 else 0

print(f"Total Images : {total}")
print(f"Correct      : {correct}")
print(f"Incorrect    : {total - correct}")
print(f"Accuracy     : {accuracy:.2%}")