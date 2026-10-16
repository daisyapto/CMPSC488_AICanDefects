from datetime import datetime
import subprocess
import time

# Configure GPIO17 as input with no pull resistor ('pn')
# Tip: change 'pn' to 'pu' (pull-up) or 'pd' (pull-down) if you have a floating switch
subprocess.run(["pinctrl", "set", "17", "ip", "pn"], check=True)

last_state = None
last_time = time.time()

print("Monitoring GPIO17... Press Ctrl+C to stop.")

try:
    while True:
        raw_output = subprocess.check_output(
            ["pinctrl", "get", "17"], text=True
        ).strip()

        # Parse the logic level
        if "| hi" in raw_output:
            state = "HIGH"
        elif "| lo" in raw_output:
            state = "LOW"
        else:
            state = "UNKNOWN"

        if state != last_state:
            now = time.time()
            duration = now - last_time
            timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]

            if last_state is not None:
                edge = (
                    "RISING (LOW -> HIGH)"
                    if state == "HIGH"
                    else "FALLING (HIGH -> LOW)"
                )
                # Map logic level to your hardware context (e.g., button / reed switch)
                label = "CLOSED / ACTIVE" if state == "HIGH" else "OPEN / IDLE"

                print(
                    f"[{timestamp}] State: {state:<4} | Transition: {edge:<21} | "
                    f"Held for: {duration:6.2f}s | Label: {label}"
                )
            else:
                # First reading
                print(
                    f"[{timestamp}] Initial State: {state} (Raw: '{raw_output}')"
                )

            last_state = state
            last_time = now

        time.sleep(0.05)

except KeyboardInterrupt:
    print("\nStopped.")
