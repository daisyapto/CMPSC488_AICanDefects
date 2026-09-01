from ultralytics import YOLO
import cv2

print("Loading YOLO...")

# Load a small pretrained YOLO model
model = YOLO("yolo11n.pt")

# Run detection on a sample image
results = model("https://ultralytics.com/images/bus.jpg")

print("Detection completed successfully!")
print(f"Detected {len(results[0].boxes)} objects.")

print("\nYOLO is working correctly!")

# Open the default Mac webcam (0 is usually the built-in FaceTime camera)
# 0 worked for my Mac to connect to the external cam - Daisy
cap = cv2.VideoCapture(0)
print(cap)


def check_available_cameras(max_tested=5):
  available_ports = []
  for i in range(max_tested):
    cap = cv2.VideoCapture(i)
    if cap.isOpened():
      available_ports.append(i)
      cap.release()

  return available_ports


print("Active camera indices:", check_available_cameras())

print("YOLO loaded successfully!")
print("Running test detection...")

if not cap.isOpened():
  print("Error: Could not open camera.")
  exit()

while cap.isOpened():
  success, frame = cap.read()

  if not success:
    print("Ignoring empty camera frame.")
    break

  # Run YOLO inference on the frame
  results = model(frame)

  # Visualize the results on the frame
  annotated_frame = results[0].plot()

  # Display the annotated frame
  cv2.imshow("YOLO Mac Webcam", annotated_frame)

  # Press 'q' to break the loop and close the window
  if cv2.waitKey(1) & 0xFF == ord("q"):
    break

# Release the capture and close all OpenCV windows
cap.release()
cv2.destroyAllWindows()