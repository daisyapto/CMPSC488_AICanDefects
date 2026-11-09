import subprocess
import time

# Configure GPIO17 as input
subprocess.run(["pinctrl", "set", "17", "ip", "pn"], check=True)

last_state = None

print("Monitoring GPIO17... Press Ctrl+C to stop.")

try:
    while True:
        result = subprocess.check_output(
            ["pinctrl", "get", "17"], text=True
        )

        if "| hi" in result:
            state = "HIGH"
        elif "| lo" in result:
            state = "LOW"
        else:
            state = "UNKNOWN"

        if state != last_state:
            print(f"GPIO17: {state}", flush=True)
            last_state = state

        time.sleep(0.05)

except KeyboardInterrupt:
    print("\nStopped.")
 
