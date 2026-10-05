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

#Test script 2
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
    camera.Disable()

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
