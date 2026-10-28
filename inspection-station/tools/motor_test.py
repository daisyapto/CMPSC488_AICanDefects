"""Turn the conveyor motor on its own - no dashboard, no camera.

    python3 tools/motor_test.py              500 steps forward at 30 %
    python3 tools/motor_test.py 1000 -1 50   1000 steps reverse at 50 %

Stop the station and the GTK motor program first (only one program can use the pins).
Each step prints OK or the exact error, so you can see where it fails.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from inspection_station.config import MOTOR_PINS          # noqa: E402
from inspection_station.conveyor import c_speed, half_period_us   # noqa: E402

steps = int(sys.argv[1]) if len(sys.argv) > 1 else 500
direction = int(sys.argv[2]) if len(sys.argv) > 2 else 1
pct = int(sys.argv[3]) if len(sys.argv) > 3 else 30
P = MOTOR_PINS["conveyor"]
half = half_period_us(c_speed(pct))


def step(n, text):
    print(f"{n}. {text} ... ", end="", flush=True)


step(1, "import lgpio")
try:
    import lgpio
    print("OK")
except Exception as e:
    print(f"FAILED: {e}\n   fix: run 'deactivate' if the prompt shows (.venv), or: sudo apt install python3-lgpio")
    sys.exit(1)

step(2, "open the GPIO chip")
h = None
for chip in (0, 4):
    try:
        h = lgpio.gpiochip_open(chip)
        print(f"OK (gpiochip{chip})")
        break
    except Exception as e:
        err = e
if h is None:
    print(f"FAILED: {err}\n   fix: ls -l /dev/gpiochip*  (must be group gpio, you in the gpio group)")
    sys.exit(1)

step(3, f"claim conveyor pins {P}")
try:
    for pin in P.values():
        lgpio.gpio_claim_output(h, pin, 0)
    print("OK")
except Exception as e:
    print(f"FAILED on GPIO {pin}: {e}\n   fix: another program uses it - pkill -x motor ; "
          "sudo systemctl stop can-inspection ; pkill -f run.py")
    sys.exit(1)

step(4, f"driver on, direction {'forward' if direction > 0 else 'reverse'}")
try:
    lgpio.gpio_write(h, P["en"], 1)                 # HAT: EN high = on
    lgpio.gpio_write(h, P["dir"], 0 if direction > 0 else 1)
    time.sleep(0.001)
    print("OK")
except Exception as e:
    print(f"FAILED: {e}")
    sys.exit(1)

rate = round(1e6 / (2 * half))
step(5, f"{steps} steps at {pct} % ({rate} steps/s, {half} us half period)")
try:
    lgpio.tx_pulse(h, P["step"], half, half, 0, steps)
    t0 = time.monotonic()
    time.sleep(steps * 2 * half / 1e6)          # the move's own time first - tx_busy can read 0 at the start
    while lgpio.tx_busy(h, P["step"], lgpio.TX_PWM):
        time.sleep(0.01)
        if time.monotonic() - t0 > steps / rate + 5:
            print("still busy - stopping")
            break
    print(f"OK ({time.monotonic() - t0:.2f} s)")
except Exception as e:
    print(f"FAILED: {e}")
    print("   trying the C program's method (software timed) ... ", end="", flush=True)
    try:
        for _ in range(steps):
            lgpio.gpio_write(h, P["step"], 1)
            time.sleep(half / 1e6)
            lgpio.gpio_write(h, P["step"], 0)
            time.sleep(half / 1e6)
        print("OK - send this output to fix the station")
    except Exception as e2:
        print(f"FAILED: {e2}")

step(6, "driver off, pins released")
try:
    lgpio.tx_pwm(h, P["step"], 0, 0)
except Exception:
    pass
lgpio.gpio_write(h, P["en"], 0)
for pin in P.values():
    lgpio.gpio_free(h, pin)
lgpio.gpiochip_close(h)
print("OK")
print("\nDid the belt move? If every step says OK but nothing turned: motor power on the HAT, "
      "motor cable on MOTOR1, HAT pressed onto the header.")
