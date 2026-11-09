import subprocess
import time

# --- Hardware Configuration ---
SENSOR_PIN = "24"       # Photoelectric/inductive sensor
REJECT_PIN = "23"       # Pneumatic reject solenoid (Physical Pin 16)

# Pneumatic burst profile (adjust pulse widths to optimize air conservation)
PULSE_COUNT = 3         # Number of rapid hits
ON_TIME = 0.04          # 40ms solenoid open
OFF_TIME = 0.03         # 30ms inter-pulse recovery

# Setup GPIO 24 as input, no pull
subprocess.run(["pinctrl", "set", SENSOR_PIN, "ip", "pn"], check=True)

# Setup GPIO 23 as output, initialized LOW (op dl)
subprocess.run(["pinctrl", "set", REJECT_PIN, "op", "dl"], check=True)

# Single-execution lock flag
has_triggered = False


def fire_rapid_burst():
    """Fires a controlled rapid burst sequence to actuate with minimal air consumption."""
    print(">>> CRITICAL: Trigger condition met. Executing air burst...", flush=True)
    for i in range(PULSE_COUNT):
        # Open valve
        subprocess.run(["pinctrl", "set", REJECT_PIN, "dh"], check=False)
        time.sleep(ON_TIME)
        # Close valve
        subprocess.run(["pinctrl", "set", REJECT_PIN, "dl"], check=False)
        time.sleep(OFF_TIME)
    print(">>> Burst sequence complete.", flush=True)


print(f"System armed. Monitoring Sensor on GPIO {SENSOR_PIN}...")
print(f"Safety latch enabled: Rejector on GPIO {REJECT_PIN} will fire ONCE only.")

try:
    while not has_triggered:
        result = subprocess.check_output(
            ["pinctrl", "get", SENSOR_PIN], text=True
        )

        if "| lo" in result:
            # 1. Fire actuator immediately
            fire_rapid_burst()

            # 2. Lock the mechanism permanently for this session
            has_triggered = True

            # 3. Hardware safety disarm
            subprocess.run(["pinctrl", "set", REJECT_PIN, "dl"], check=False)
            print(">>> SAFETY LATCH: Rejector locked out. Script terminating cleanly.")
            break

        time.sleep(0.005)  # 5ms high-frequency polling for fast-moving line items

except KeyboardInterrupt:
    print("\nAborted by user.")

finally:
    # Fail-safe: ensure solenoid cannot remain energized under any condition
    subprocess.run(["pinctrl", "set", REJECT_PIN, "dl"], check=False)
    print("Solenoid confirmed de-energized.")
