"""Which edition of the station this is.

  full  - Built-in + Team + YOLO detection engines.
  lite  - Built-in + Team only. No YOLO: lighter on the Pi's CPU and heat,
          for running without the active cooler.

The demo branch ships the full edition; set EDITION = "lite" to hide YOLO.
"""
EDITION = "full"
YOLO_AVAILABLE = EDITION == "full"
