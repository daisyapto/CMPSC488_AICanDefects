""" Test script 1
import PySpin
import cv2
import numpy as np

system = PySpin.System.GetInstance()
cam_list = system.GetCameras()
cam = cam_list[0]

cam.Init()

nodemap = cam.GetNodeMap()

# Set pixel format to Mono8
pixel_format = PySpin.CEnumerationPtr(nodemap.GetNode("PixelFormat"))
mono8 = pixel_format.GetEntryByName("Mono8")
pixel_format.SetIntValue(mono8.GetValue())

cam.BeginAcquisition()

while True:
    frame = cam.GetNextImage()

    if frame.IsIncomplete():
        frame.Release()
        continue

    img = frame.GetNDArray()

    cv2.imshow("Live", img)

    frame.Release()

    if cv2.waitKey(1) == 27:
        break

cam.EndAcquisition()
cam.DeInit()
cam_list.Clear()
system.ReleaseInstance()
cv2.destroyAllWindows()"""

"""#Test script 2
import PySpin

# Create a system pointer
system = PySpin.System.GetInstance()

# Get the number of cameras
num_cameras = system.GetCameras()

print(f"Found {num_cameras} camera(s):")

# Enumerate cameras
for cam in num_cameras:
    camera = cam.GetCamera()
    print(f"Camera: ({camera.GetSerialNumber()})")
    camera.Disable()"""

""" Test script 3

import PySpin

print(PySpin.System.GetInstance())

# Create a camera manager
cam_manager = PySpin.CameraManager()

# Find cameras by IP address
cam_list = cam_manager.GetCamerasByIP("")  # Replace with your camera's IP

if cam_list:
    cam = cam_list[0]
    print("Found camera:", cam.GetSerialNumber())
    cam.Open()
    cam.StartGrabbing(PySpin.GrabStrategy_LatestImage)
    while True:
        grab_result = cam.Grab()
        if grab_result.IsGrabSuccessful():
            image = grab_result.GetImage()
            print("Image grabbed")
        else:
            print("Grab failed")"""

"""#Test script 4

import PySpin

system = PySpin.System.GetInstance()
cam_list = system.GetCameras()

print("Number of cameras detected:", cam_list.GetSize())

for i, cam in enumerate(cam_list):
    nodemap_tldevice = cam.GetTLDeviceNodeMap()

    serial_node = PySpin.CStringPtr(
        nodemap_tldevice.GetNode("DeviceSerialNumber")
    )

    model_node = PySpin.CStringPtr(
        nodemap_tldevice.GetNode("DeviceModelName")
    )

    ip_node = PySpin.CStringPtr(
        nodemap_tldevice.GetNode("GevCurrentIPAddress")
    )

    print(f"\nCamera Index: {i}")
    print("Model:", model_node.GetValue())
    print("Serial:", serial_node.GetValue())

cam_list.Clear()
system.ReleaseInstance()"""

"""#Test script 5
import cv2
import numpy as np

img = np.zeros((500,500), dtype=np.uint8)

cv2.imshow("Test", img)
cv2.waitKey(0)"""

import PySpin
import cv2

system = PySpin.System.GetInstance()
cam_list = system.GetCameras()

print("Cameras found:", cam_list.GetSize())

cam = cam_list[0]

cam.Init()
cam.BeginAcquisition()

cv2.namedWindow("Live", cv2.WINDOW_NORMAL)

try:
    while True:

        image = cam.GetNextImage(5000)

        if image.IsIncomplete():
            image.Release()
            continue

        img = image.GetNDArray()

        cv2.imshow("Live", img)

        image.Release()

        if cv2.waitKey(1) == 27:  # ESC
            break

finally:
    cam.EndAcquisition()
    cam.DeInit()

    del cam

    cam_list.Clear()
    system.ReleaseInstance()

    cv2.destroyAllWindows()
