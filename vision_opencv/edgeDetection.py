# Code ref: Copilot
# This program runs and opens 3 live cam windows, one for canny imaging, one for gray-scale imaging, and one for canny imaging with error detection
# In the gray-scale image, the level of closeness to a perfect circle is checked.

import cv2
import numpy as np

# macOS:
# index 0 = FaceTime camera / connection to phone, but when disconnect pressed on pop up, will link to plugged in cam
# index 1 = webcam on laptop

cap = cv2.VideoCapture(0)
print(cap)

while True:
    ret, frame = cap.read()
    if not ret:
        break

    # Create grayscale frame
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    # blur to blue the image to see the lines more clearly
    blur = cv2.GaussianBlur(gray, (9, 9), 0)

    # Canny creates binary image of the blurred image, extracting the white areas as distinctive lines
    edges = cv2.Canny(blur, 100, 200)

    # 3. Calculate Sobel X (detects vertical lines, dx=1, dy=0)
    # We use cv2.CV_64F to capture negative gradients
    sobelx_64f = cv2.Sobel(blur, cv2.CV_64F, 1, 0, ksize=3)
    sobelx = cv2.convertScaleAbs(sobelx_64f)

    # 4. Calculate Sobel Y (detects horizontal lines, dx=0, dy=1)
    sobely_64f = cv2.Sobel(blur, cv2.CV_64F, 0, 1, ksize=3)
    sobely = cv2.convertScaleAbs(sobely_64f)

    # 5. Combine the two gradients (approximated magnitude)
    sobel_combined = cv2.addWeighted(sobelx, 0.5, sobely, 0.5, 0)

    # Edge detection
    contours, _ = cv2.findContours(
        edges,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_NONE
    )

    # Closeness to perfect circle
    if contours:
        contour = max(contours, key=cv2.contourArea)
        area = cv2.contourArea(contour)

        if area > 1000:
            perimeter = cv2.arcLength(contour, True)

            circularity = (
                    4 * np.pi * area /
                    (perimeter * perimeter)
            )

            circularity_percent = circularity * 100

            (x, y), radius = cv2.minEnclosingCircle(contour)

            center = (int(x), int(y))
            radius = int(radius)

            cv2.circle(
                gray,
                center,
                radius,
                (0, 255, 0),
                2
            )

            points = contour.reshape(-1, 2)

            distances = np.sqrt(
                (points[:, 0] - x) ** 2 +
                (points[:, 1] - y) ** 2
            )

            mean_radius = np.mean(distances)
            radius_std = np.std(distances)

            errors = np.abs(distances - mean_radius)

            perfection = max(
                0,
                100 * (1 - radius_std / mean_radius)
            )

            cv2.putText(
                gray,
                f"Circularity: {circularity_percent:.1f}%",
                (20, 40),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 255, 0),
                2
            )

            cv2.putText(
                gray,
                f"Perfection: {perfection:.1f}%",
                (20, 80),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 255, 0),
                2
            )

        # Hough circle detection
        # Finds circle-like objects within a canny image, even when the circle is not complete
        # Used to be compared to true edge
        # Difference between perfect circle detected and true edge shows the defects
        gray2 = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        gray3 = gray2.copy()

        circles = cv2.HoughCircles(
            edges,
            cv2.HOUGH_GRADIENT,
            dp=1.2,
            minDist=100,
            param1=50,
            param2=15,
            minRadius=100,
            maxRadius=300
        )

        if circles is not None:
            circles = np.round(circles[0, :]).astype(int)
            x, y, radius = circles[0]
            cv2.circle(
                gray2,
                (x, y),
                radius,
                (0, 255, 0),
                2
            )
            circle_mask = np.zeros_like(edges)

            cv2.circle(
                circle_mask,
                (x, y),
                radius,
                255,
                2
            )
            cv2.imshow("Ideal Circle", circle_mask)

            missing_edge = cv2.bitwise_and(
                circle_mask,
                cv2.bitwise_not(edges)
            )

            missing_contours, _ = cv2.findContours(
                missing_edge,
                cv2.RETR_EXTERNAL,
                cv2.CHAIN_APPROX_SIMPLE
            )

            for mc in missing_contours:

                if cv2.contourArea(mc) < 5:
                    continue

                x_box, y_box, w_box, h_box = cv2.boundingRect(mc)

                cv2.rectangle(
                    gray2,
                    (x_box, y_box),
                    (x_box + w_box, y_box + h_box),
                    (0, 0, 255),
                    2
                )

            center_x = x
            center_y = y
            hough_radius = radius

            hough_points = contour.reshape(-1,2)

            errors = []
            for px, py in hough_points:
                actual_radius = np.sqrt(
                    (px - center_x) ** 2 +
                    (py - center_y) ** 2
                )
                error = actual_radius - hough_radius
                errors.append(error)

            """
            errors = np.array(errors)
            print("Max Error:", np.max(np.abs(errors)))
            print("Mean Error:", np.mean(np.abs(errors)))
            print("Std Dev:", np.std(errors))"""

            threshold = 100 # Modify based on how big a defect should be before marking

            for px, py in hough_points:

                actual_radius = np.sqrt(
                    (px - center_x) ** 2 +
                    (py - center_y) ** 2
                )

                error = abs(actual_radius - hough_radius)

                if error > threshold:
                    cv2.circle(
                        gray2,
                        (px, py),
                        2,
                        (0, 0, 255),
                        -1
                    )

        cv2.imshow("Edges", edges)
        cv2.imshow("Circle Inspection - Hough Circles with Canny", gray2)

        circles_sobel = cv2.HoughCircles(
            sobel_combined,
            cv2.HOUGH_GRADIENT,
            dp=1.2,
            minDist=100,
            param1=50,
            param2=15,
            minRadius=100,
            maxRadius=300
        )

        if circles_sobel is not None:
            circles_sobel = np.round(circles_sobel[0, :]).astype(int)
            x, y, radius = circles_sobel[0]
            cv2.circle(
                gray3,
                (x, y),
                radius,
                (0, 255, 0),
                2
            )
            circle_mask = np.zeros_like(sobel_combined)

            cv2.circle(
                circle_mask,
                (x, y),
                radius,
                255,
                2
            )
            cv2.imshow("Ideal Circle - Sobel", circle_mask)

            missing_edge = cv2.bitwise_and(
                circle_mask,
                cv2.bitwise_not(sobel_combined)
            )

            missing_contours, _ = cv2.findContours(
                missing_edge,
                cv2.RETR_EXTERNAL,
                cv2.CHAIN_APPROX_SIMPLE
            )

            for mc in missing_contours:

                if cv2.contourArea(mc) < 5:
                    continue

                x_box, y_box, w_box, h_box = cv2.boundingRect(mc)

                cv2.rectangle(
                    gray3,
                    (x_box, y_box),
                    (x_box + w_box, y_box + h_box),
                    (0, 0, 255),
                    2
                )

            center_x = x
            center_y = y
            hough_radius = radius

            hough_points = contour.reshape(-1,2)

            errors = []
            for px, py in hough_points:
                actual_radius = np.sqrt(
                    (px - center_x) ** 2 +
                    (py - center_y) ** 2
                )
                error = actual_radius - hough_radius
                errors.append(error)

            """
            errors = np.array(errors)
            print("Max Error:", np.max(np.abs(errors)))
            print("Mean Error:", np.mean(np.abs(errors)))
            print("Std Dev:", np.std(errors))"""

            threshold = 100 # Modify based on how big a defect should be before marking

            for px, py in hough_points:

                actual_radius = np.sqrt(
                    (px - center_x) ** 2 +
                    (py - center_y) ** 2
                )

                error = abs(actual_radius - hough_radius)

                if error > threshold:
                    cv2.circle(
                        gray3,
                        (px, py),
                        2,
                        (0, 0, 255),
                        -1
                    )

        cv2.imshow("Sobel Edges", sobel_combined)
        cv2.imshow("Circle Inspection - Hough Circles with Sobel", gray3)

    if cv2.waitKey(1) & 0xFF == 27:
        break

cap.release()
cv2.destroyAllWindows()
