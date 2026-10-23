"""Which edition of the station this is.

  full  - Built-in + Team + YOLO detection engines.
  lite  - Built-in + Team only. No YOLO: lighter on the Pi's CPU and heat,
          for running without the active cooler.

tools/build_editions.py sets this when it builds the two zips.
"""
EDITION = "full"
YOLO_AVAILABLE = EDITION == "full"
