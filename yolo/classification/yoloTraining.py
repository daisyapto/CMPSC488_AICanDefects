# Code ref: Copilot
# Training a classification YOLO where a frame is identified as pass or fail
# Initial version -- needs to be changed to segementation or detection for tracking of object within frame
from ultralytics import YOLO
import cv2

print("Loading YOLO...")

# Load a small pretrained YOLO model
model = YOLO("yolo11n-cls.pt")

model.train(
    data="dataset",
    epochs=15,
    name='pass-fail-classifier',
    verbose=True
)
