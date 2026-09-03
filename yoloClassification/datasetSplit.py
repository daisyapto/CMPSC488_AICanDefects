# Code ref: Copilot
# How to create dataset if have a directory of pass images and of directory of fail images in 1 directory
from pathlib import Path
import shutil
import random

random.seed(42)

dataset_dir = Path("dataset")

for cls in ["pass", "fail"]:

    src_dir = dataset_dir / cls

    images = list(src_dir.glob("*"))
    random.shuffle(images)

    split_idx = int(len(images) * 0.8)

    train_images = images[:split_idx]
    test_images = images[split_idx:]

    train_dir = dataset_dir / "train" / cls
    test_dir = dataset_dir / "test" / cls

    train_dir.mkdir(parents=True, exist_ok=True)
    test_dir.mkdir(parents=True, exist_ok=True)

    for img in train_images:
        shutil.move(img, train_dir / img.name)

    for img in test_images:
        shutil.move(img, test_dir / img.name)

print("Done.")