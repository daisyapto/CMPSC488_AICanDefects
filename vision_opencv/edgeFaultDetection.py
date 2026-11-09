import cv2
import numpy as np

# ---------------------------------------------------------
# CONFIGURATION & PARAMETERS
# ---------------------------------------------------------
REFERENCE_IMAGE_PATH = r"C:\Users\esosa\Downloads\can_Samples\can_top.jpg"
PIXELS_PER_MM = 8.0          # Set to match your physical distance calibration
TOLERANCE_MM = 2.0           # Acceptable diameter variation in mm
CIRCULARITY_THRESHOLD = 0.85 # Form factor: <0.85 indicates dents or rim warping

# ---------------------------------------------------------
# HELPER: Detect Rim Geometry & Shape Circularity
# ---------------------------------------------------------
def analyze_can_rim(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (9, 9), 2)

    circles = cv2.HoughCircles(
        blurred,
        cv2.HOUGH_GRADIENT,
        dp=1.2,
        minDist=80,
        param1=100,
        param2=35,
        minRadius=40,
        maxRadius=350
    )

    if circles is None:
        return None

    circles = np.round(circles[0, :]).astype("int")
    x, y, radius = circles[0]
    diameter_px = radius * 2
    diameter_mm = diameter_px / PIXELS_PER_MM

    # Defect check: isolate rim region to evaluate contour circularity
    pad = 15
    y1, y2 = max(0, y - radius - pad), min(frame.shape[0], y + radius + pad)
    x1, x2 = max(0, x - radius - pad), min(frame.shape[1], x + radius + pad)
    roi = blurred[y1:y2, x1:x2]

    edges = cv2.Canny(roi, 50, 150)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    circularity = 1.0
    if contours:
        c = max(contours, key=cv2.contourArea)
        area = cv2.contourArea(c)
        perimeter = cv2.arcLength(c, True)
        if perimeter > 0:
            circularity = (4 * np.pi * area) / (perimeter ** 2)

    return {
        "center": (x, y),
        "radius": radius,
        "diameter_px": diameter_px,
        "diameter_mm": diameter_mm,
        "circularity": circularity
    }

# ---------------------------------------------------------
# HELPER: Auto-Connect to Working Camera Index (DirectShow)
# ---------------------------------------------------------
def open_camera():
    # Tries DirectShow on indexes 0, 1, 2 to resolve Windows MSMF -1072875772 errors
    for idx in [0, 1, 2]:
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        if cap.isOpened():
            ret, test_frame = cap.read()
            if ret and test_frame is not None:
                print(f"Successfully connected to camera at index {idx} using DirectShow.")
                return cap
            cap.release()
    return None

# ---------------------------------------------------------
# STEP 1: Process the Reference Image
# ---------------------------------------------------------
ref_img = cv2.imread(REFERENCE_IMAGE_PATH)
if ref_img is None:
    print(f"Error: Could not open reference image: {REFERENCE_IMAGE_PATH}")
    exit()

ref_data = analyze_can_rim(ref_img)
if ref_data is None:
    print("Error: Rim not detected in reference image. Review file path or lighting.")
    exit()

target_dia_mm = ref_data["diameter_mm"]
print(f"Specification Baseline Set:")
print(f" - Reference Diameter: {ref_data['diameter_px']} px ({target_dia_mm:.2f} mm)")

# Draw baseline circle on reference window
cv2.circle(ref_img, ref_data["center"], ref_data["radius"], (0, 255, 0), 2)
cv2.putText(
    ref_img,
    f"Target Spec: {target_dia_mm:.2f} mm",
    (20, 40),
    cv2.FONT_HERSHEY_SIMPLEX,
    0.7,
    (0, 255, 0),
    2
)
cv2.imshow("Specification Standard (Reference)", ref_img)

# ---------------------------------------------------------
# STEP 2: Live Camera Stream & Defect Inspection
# ---------------------------------------------------------
cap = open_camera()
if cap is None:
    print("Error: No working camera stream detected on indexes 0, 1, or 2.")
    exit()

print("Inspection running. Position can under camera. Press 'q' to exit.")

while True:
    ret, frame = cap.read()
    if not ret:
        print("Camera frame dropped.")
        break

    live_data = analyze_can_rim(frame)

    if live_data is not None:
        x, y = live_data["center"]
        radius = live_data["radius"]
        measured_mm = live_data["diameter_mm"]
        circularity = live_data["circularity"]

        delta_mm = abs(measured_mm - target_dia_mm)
        is_size_bad = delta_mm > TOLERANCE_MM
        is_shape_bad = circularity < CIRCULARITY_THRESHOLD

        if is_size_bad or is_shape_bad:
            color = (0, 0, 255)  # Red: Defect
            status = "REJECT: DEFECT"
            reasons = []
            if is_size_bad:
                reasons.append(f"Size diff: {delta_mm:.2f}mm")
            if is_shape_bad:
                reasons.append(f"Dent/Warpage ({circularity:.2f})")
            detail = " | ".join(reasons)
        else:
            color = (0, 255, 0)  # Green: Passed
            status = "PASS: IN SPEC"
            detail = f"Delta: {delta_mm:.2f}mm | Circ: {circularity:.2f}"

        # Draw inspection annotations
        cv2.circle(frame, (x, y), radius, color, 2)
        cv2.circle(frame, (x, y), 3, (0, 0, 255), -1)

        cv2.putText(frame, status, (20, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
        cv2.putText(frame, f"Live: {measured_mm:.2f}mm (Target: {target_dia_mm:.2f}mm)",
                    (20, 65), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        cv2.putText(frame, detail, (20, 95), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)
    else:
        cv2.putText(frame, "Align can rim under camera...", (20, 35),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)

    cv2.imshow("Live QC Inspection", frame)

    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()