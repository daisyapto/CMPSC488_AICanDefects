This project will work on creating the budget solution for the Yard's Brewing Company AI Camera system for detecting and rejecting defective cans.

Our budget solution will use OpenCV tools to convert the frame into a binary image and apply measurement techniques to discover the defects along the can flange by comparison to a perfect round can flange.

- yoloClassification directory: contains 3 scripts that go along with a directory "dataset" that contains "pass" and "fail" subdirectories (these folders can be found and manually downloaded from our Teams2 channel; many images to upload to GitHub); datasetSplit.py prepares training and testing split for YOLO classification model (only works with 1 can in the frame, an initial test); yoloTraining.py creates a trained yolo model file; yoloTesting.py tests the generated YOLO model file on the data and returns performance metrics
- edgeDetection.py: currently one of our best-working initial testing solution, contains multiple windows that show how the OpenCV tools process a live frame; currently quite sensitive and bounding boxes are not very accurate to defects
- edgeFaultDetection.py:
- framesCR.py:
- testScript1.py: the first initial test connecting to the ELS where it simply opens the camera and creates a live frame, a couple of tools tested but moved to edgeDetection.py
