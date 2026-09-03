import cv2
import os
import time
from datetime import datetime

# --------------------------------------------------
# SETTINGS
# --------------------------------------------------

CAMERA_INDEX = 1

WIDTH = 1920
HEIGHT = 1080
FPS = 30

CAPTURE_DURATION = 7  # seconds

OUTPUT_FOLDER = "can_frames"


# --------------------------------------------------
# CREATE FOLDER FOR THIS RUN
# --------------------------------------------------

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

session_folder = os.path.join(
    OUTPUT_FOLDER,
    f"capture_{timestamp}"
)

os.makedirs(session_folder, exist_ok=True)


# --------------------------------------------------
# OPEN GLOBAL SHUTTER CAMERA
# --------------------------------------------------

cap = cv2.VideoCapture(
    CAMERA_INDEX,
    cv2.CAP_DSHOW
)

if not cap.isOpened():
    print("ERROR: Could not open camera.")
    exit()


# --------------------------------------------------
# CAMERA SETTINGS
# --------------------------------------------------

cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
cap.set(cv2.CAP_PROP_FPS, FPS)

actual_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
actual_fps = cap.get(cv2.CAP_PROP_FPS)

print("Global shutter camera opened.")
print(f"Resolution: {actual_width} x {actual_height}")
print(f"FPS: {actual_fps}")

print()
print("7-second conveyor simulation.")
print("Move cans continuously under the camera.")
print("Press Q to stop early.")
print()


# --------------------------------------------------
# CAPTURE
# --------------------------------------------------

start_time = time.perf_counter()

frame_number = 0
saved_number = 0

while True:

    ret, frame = cap.read()

    if not ret:
        print("ERROR: Could not read frame.")
        break

    elapsed = time.perf_counter() - start_time

    if elapsed >= CAPTURE_DURATION:
        break

    # --------------------------------------------------
    # SAVE EVERY FRAME
    # --------------------------------------------------

    filename = f"frame_{saved_number:04d}.jpg"

    filepath = os.path.join(
        session_folder,
        filename
    )

    cv2.imwrite(
        filepath,
        frame,
        [cv2.IMWRITE_JPEG_QUALITY, 95]
    )

    frame_number += 1
    saved_number += 1


    # --------------------------------------------------
    # PREVIEW
    # --------------------------------------------------

    preview = frame.copy()

    remaining = max(
        0,
        CAPTURE_DURATION - elapsed
    )

    cv2.putText(
        preview,
        f"CAPTURING: {remaining:.1f}s",
        (20, 50),
        cv2.FONT_HERSHEY_SIMPLEX,
        1,
        (0, 0, 255),
        2
    )

    cv2.putText(
        preview,
        f"Frames saved: {saved_number}",
        (20, 90),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (0, 255, 0),
        2
    )

    cv2.imshow(
        "Conveyor Can Capture",
        preview
    )

    if cv2.waitKey(1) & 0xFF == ord("q"):
        break


# --------------------------------------------------
# CLEANUP
# --------------------------------------------------

cap.release()
cv2.destroyAllWindows()

print()
print("Capture complete.")
print(f"Frames saved: {saved_number}")
print(f"Folder: {session_folder}")