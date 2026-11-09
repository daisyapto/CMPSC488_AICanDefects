import os
import time
import cv2
import gpiod
from gpiod.line import Direction, Value
import PySpin

# --- Configuration ---
SENSOR_PIN = 24
TIMEOUT_SECONDS = 10.0

# Active-LOW logic: LOW = object present, HIGH = sensor clear
SENSOR_ACTIVE_STATE = "LOW"

# Window display size (adjust as needed for your screen resolution)
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 720

# Chip detection: Pi 5 uses /dev/gpiochip4; Pi 4/3 uses /dev/gpiochip0
CHIP_PATH = "/dev/gpiochip4" if os.path.exists("/dev/gpiochip4") else "/dev/gpiochip0"


def main():
    # 1. Initialize GPIO line via gpiod
    try:
        line_request = gpiod.request_lines(
            CHIP_PATH,
            consumer="sensor-cam",
            config={SENSOR_PIN: gpiod.LineSettings(direction=Direction.INPUT)},
        )
        print(f"GPIO {SENSOR_PIN} initialized on {CHIP_PATH}.")
    except Exception as e:
        print(f"Failed to access {CHIP_PATH} on pin {SENSOR_PIN}: {e}")
        return

    def get_pin_state() -> str:
        try:
            val = line_request.get_value(SENSOR_PIN)
            return "HIGH" if val == Value.ACTIVE else "LOW"
        except Exception as e:
            print(f"Error reading GPIO: {e}")
            return "UNKNOWN"

    # 2. Initialize Spinnaker system & camera
    system = PySpin.System.GetInstance()
    cam_list = system.GetCameras()

    if cam_list.GetSize() == 0:
        print("No FLIR camera found.")
        cam_list.Clear()
        system.ReleaseInstance()
        line_request.release()
        return

    cam = cam_list[0]
    cam.Init()

    # Configure Pixel Format to Mono8
    nodemap = cam.GetNodeMap()
    pixel_format = PySpin.CEnumerationPtr(nodemap.GetNode("PixelFormat"))
    if PySpin.IsAvailable(pixel_format) and PySpin.IsWritable(pixel_format):
        mono8 = pixel_format.GetEntryByName("Mono8")
        pixel_format.SetIntValue(mono8.GetValue())

    print("Camera initialized and ready.")
    print(
        f"Waiting for sensor on GPIO {SENSOR_PIN} (Trigger: {SENSOR_ACTIVE_STATE})..."
    )

    feed_active = False
    window_open = False
    stop_timer_started = False
    clear_time = 0.0

    try:
        while True:
            current_state = get_pin_state()
            is_detected = current_state == SENSOR_ACTIVE_STATE

            # --- Detection & 10-Second Cooldown Timing ---
            if is_detected:
                # Cancel countdown if an object is present or returns
                stop_timer_started = False

                if not feed_active:
                    print("Object detected: Starting camera feed...")
                    cam.BeginAcquisition()

                    # Create and scale the window
                    cv2.namedWindow("Live", cv2.WINDOW_NORMAL)
                    cv2.resizeWindow("Live", WINDOW_WIDTH, WINDOW_HEIGHT)

                    feed_active = True
                    window_open = True
            else:
                if feed_active:
                    if not stop_timer_started:
                        print(
                            f"Object cleared: Countdown started. Holding feed for {TIMEOUT_SECONDS}s..."
                        )
                        clear_time = time.time()
                        stop_timer_started = True
                    else:
                        elapsed = time.time() - clear_time
                        if elapsed >= TIMEOUT_SECONDS:
                            print(
                                f"Full {TIMEOUT_SECONDS} seconds elapsed. Stopping camera feed."
                            )
                            cam.EndAcquisition()
                            feed_active = False
                            stop_timer_started = False

                            if window_open:
                                cv2.destroyAllWindows()
                                window_open = False

            # --- Frame Capture & Rendering ---
            if feed_active:
                try:
                    image = cam.GetNextImage(1000)

                    if not image.IsIncomplete():
                        img_data = image.GetNDArray()

                        # Visual countdown banner
                        if stop_timer_started:
                            remaining = max(
                                0.0,
                                TIMEOUT_SECONDS - (time.time() - clear_time),
                            )
                            cv2.putText(
                                img_data,
                                f"Holding feed: {remaining:.1f}s remaining",
                                (30, 50),
                                cv2.FONT_HERSHEY_SIMPLEX,
                                1.1,
                                (255, 255, 255),
                                2,
                            )

                        cv2.imshow("Live", img_data)

                    image.Release()
                except PySpin.SpinnakerException:
                    pass

                # ESC key to exit
                if cv2.waitKey(1) == 27:
                    print("ESC pressed. Exiting...")
                    break
            else:
                time.sleep(0.05)

    except KeyboardInterrupt:
        print("\nInterrupted by user.")

    finally:
        if feed_active:
            try:
                cam.EndAcquisition()
            except PySpin.SpinnakerException:
                pass

        cam.DeInit()
        del cam
        cam_list.Clear()
        system.ReleaseInstance()

        cv2.destroyAllWindows()
        line_request.release()
        print("Cleaned up camera, OpenCV windows, and GPIO line.")


if __name__ == "__main__":
    main()
