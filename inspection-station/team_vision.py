"""
team_vision.py - the team's own OpenCV defect detection.

Put your detection code in detect() below. In the dashboard choose
Settings -> Detection -> Detection engine -> "Team", and the station will use
this function for every can: PASS / FAIL, the air reject, the boxes in the live
view, the saved images and the reports.

THE CONTRACT (the only thing the station needs from you)
--------------------------------------------------------
    detect(image) -> result

    image   numpy array, BGR (like cv2.imread gives you), full camera resolution.
            A copy, so you may draw on it or change it.

    result  one of:
            []                                   good can
            [{"label": "Dent",                   one dict per defect found
              "box": (x, y, w, h),               pixels in `image`
              "confidence": 0.93}, ...]          0..1 (optional, default 0.9)
            [("Scratch", x, y, w, h, 0.8), ...]  same thing as tuples
            True / False                         good / defective, if you only classify

Rules
  * Keep it fast. The decision must be ready before the can reaches the air
    nozzle (Reject delay, 150 ms by default). Aim for under ~50 ms on the Pi.
  * If detect() raises an exception, the can is REJECTED (fail-safe) and the
    error shows in the dashboard. A crash never lets a can through.
  * Don't open windows (cv2.imshow) or read the camera here - the station
    gives you the image.
  * The station reloads this file automatically when you save it.
  * Labels the review page knows: Dent, Scratch, Flange damage, Contamination,
    Crease / wrinkle, Coating void. Other labels work too.

TEST IT ON YOUR LAPTOP, without the station
-------------------------------------------
    python team_vision.py can1.png can2.png ...       # or a folder of images
It prints the result for each image and writes <name>_result.png with the
boxes drawn. Add --show to open a window as well.

The detect() below is a working EXAMPLE (dark / bright spots on the can) so the
station runs out of the box. Replace it with your own.
"""
import time

import cv2
import numpy as np

# Example tuning values - yours can be anything.
THRESHOLD = 35          # how different from its surroundings a pixel must be
MIN_AREA = 0.0001       # smallest defect, as a fraction of the image area


def detect(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    h, w = gray.shape

    # Only look inside the can: the biggest circle in the picture.
    roi = np.zeros_like(gray)
    m = min(h, w)
    circles = cv2.HoughCircles(gray, cv2.HOUGH_GRADIENT, dp=1.5, minDist=m, param1=120, param2=40,
                               minRadius=int(m * 0.2), maxRadius=int(m * 0.49))
    if circles is not None:
        x, y, r = circles[0][0]
        cv2.circle(roi, (int(x), int(y)), int(r * 0.85), 255, -1)
    else:
        roi[int(h * .05):int(h * .95), int(w * .05):int(w * .95)] = 255

    # Pixels that differ from their neighbourhood = possible defects.
    background = cv2.medianBlur(gray, max(21, (w // 16) | 1))
    diff = cv2.absdiff(gray, background)
    _, mask = cv2.threshold(diff, THRESHOLD, 255, cv2.THRESH_BINARY)
    mask = cv2.bitwise_and(mask, roi)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))

    defects = []
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for c in contours:
        area = cv2.contourArea(c)
        if area < MIN_AREA * h * w:
            continue
        x, y, bw, bh = cv2.boundingRect(c)
        (_, _), (rw, rh), _ = cv2.minAreaRect(c)
        long_thin = max(rw, rh) / max(1.0, min(rw, rh)) >= 3.5
        strength = cv2.mean(diff, mask=cv2.drawContours(np.zeros_like(gray), [c], -1, 255, -1))[0]
        defects.append({
            "label": "Scratch" if long_thin else "Dent",
            "box": (x, y, bw, bh),
            "confidence": float(min(0.99, 0.5 + strength / (THRESHOLD * 4))),
        })
    return defects


# =============================================================================
# Laptop test runner - not used by the station
# =============================================================================
if __name__ == "__main__":
    import sys
    from pathlib import Path

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    show = "--show" in sys.argv
    files = []
    for a in args:
        p = Path(a)
        files += sorted(f for f in p.iterdir() if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff")) \
            if p.is_dir() else [p]
    if not files:
        sys.exit("usage: python team_vision.py IMAGE_OR_FOLDER [...] [--show]")

    for f in files:
        if f.stem.endswith("_result"):
            continue
        img = cv2.imread(str(f))
        if img is None:
            print(f"{f.name}: can't read")
            continue
        t0 = time.perf_counter()
        out = detect(img.copy())
        ms = (time.perf_counter() - t0) * 1000
        items = out if isinstance(out, (list, tuple)) else []
        passed = (not items) if not isinstance(out, bool) else out
        drawn = img.copy()
        for d in items:
            label, box = (d["label"], d["box"]) if isinstance(d, dict) else (d[0], d[1:5])
            x, y, bw, bh = map(int, box)
            cv2.rectangle(drawn, (x, y), (x + bw, y + bh), (0, 0, 255), 2)
            cv2.putText(drawn, str(label), (x, max(15, y - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
        verdict = "PASS" if passed else "FAIL"
        cv2.putText(drawn, verdict, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 200, 0) if passed else (0, 0, 255), 2)
        dst = f.with_name(f.stem + "_result.png")
        cv2.imwrite(str(dst), drawn)
        labels = ", ".join(str(d["label"] if isinstance(d, dict) else d[0]) for d in items)
        print(f"{f.name}: {verdict} {labels}  ({ms:.0f} ms)  -> {dst.name}")
        if show:
            cv2.imshow("team_vision", drawn)
            if cv2.waitKey(0) & 0xFF == 27:
                break
    cv2.destroyAllWindows() if show else None
