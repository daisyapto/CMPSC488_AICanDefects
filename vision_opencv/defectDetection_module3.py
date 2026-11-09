import cv2
import numpy as np
import PySpin
import time

# Get the first camera
system = PySpin.System.GetInstance()
cams = system.GetCameras()
cam = cams.GetByIndex(0)
print(cam)

# Initialize
cam.Init()

# Configure acquisition mode
cam.AcquisitionMode.SetValue(PySpin.AcquisitionMode_Continuous)

# Begin acquisition
cam.BeginAcquisition()

while True:
    originalFrameObj = cam.GetNextImage()
    if originalFrameObj.IsIncomplete():
        originalFrameObj.Release()
        continue

    start = time.perf_counter()
    frame = originalFrameObj.GetNDArray().copy()

    # Blur to reduce noise
    blur = cv2.GaussianBlur(frame, (5, 5), 0)

    # Sobel edges
    sobel_x = cv2.Sobel(blur, cv2.CV_64F, 1, 0, ksize=3)
    sobel_y = cv2.Sobel(blur, cv2.CV_64F, 0, 1, ksize=3)
    magnitude = cv2.magnitude(sobel_x, sobel_y)
    magnitude = cv2.convertScaleAbs(magnitude)

    # Binary edge image
    _, edges = cv2.threshold(magnitude, 50, 255, cv2.THRESH_BINARY)

    # Hough Circle
    circles = cv2.HoughCircles(
        blur,
        cv2.HOUGH_GRADIENT,
        dp=1,
        minDist=100,
        param1=100,
        param2=30,
        minRadius=100,
        maxRadius=300
    )

    if circles is not None:
        circles = np.uint16(np.around(circles))

        x0, y0, r = circles[0][0]

        # Reference circle mask
        ideal = np.zeros_like(edges)
        cv2.circle(ideal, (x0, y0), r, 255, 3)

        # Difference between actual edge and ideal circle
        diff = cv2.absdiff(edges, ideal)

        # Clean up
        kernel = np.ones((5,5), np.uint8)
        diff = cv2.morphologyEx(diff, cv2.MORPH_OPEN, kernel)
        diff = cv2.dilate(diff, kernel, iterations=2)

        # Find defect regions
        contours, _ = cv2.findContours(
            diff,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE
        )

        for cnt in contours:
            area = cv2.contourArea(cnt)

            if area > 30:  # ignore noise
                x, y, w, h = cv2.boundingRect(cnt)

                cv2.rectangle(
                    frame,
                    (x, y),
                    (x + w, y + h),
                    (0, 0, 255),
                    2
                )

        # Draw fitted circle
        cv2.circle(frame, (x0, y0), r, (0,255,0), 2)

    cv2.imshow("Defects", frame)

    originalFrameObj.Release()

    if cv2.waitKey(1) & 0xFF == 27:
        break

cam.EndAcquisition()
cam.DeInit()
del cam
cams.Clear()
system.ReleaseInstance()
cv2.destroyAllWindows()