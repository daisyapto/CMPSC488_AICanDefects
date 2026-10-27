#!/usr/bin/env bash
# =============================================================================
#  Raspberry Pi setup for the can inspection station.   Run:  sudo bash pi_setup.sh
#
#  Safe to run again (each step checks first). Every step asks before changing
#  anything; press Enter for the default shown in [brackets].
#    1. software (apt): Flask, NumPy, OpenCV, Aravis (Genie Nano), gpiozero
#    2. relay pins HIGH from the first second of boot (relays/air valve stay OFF while booting)
#    3. bigger network buffers (no incomplete camera frames)
#    4. Ethernet port for the camera: fixed 169.254.100.1/16, no internet route
#    5. user in the gpio group
#    6. start automatically at boot (+ optional screen in kiosk mode)
#  Afterwards: python3 pi_check.py
# =============================================================================
set -u
if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo:  sudo bash pi_setup.sh"; exit 1; fi
DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="${SUDO_USER:-pi}"
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"
REBOOT=0
ask() {  # ask "question" default(y/n)
  local a; read -r -p "$1 [$2] " a; a="${a:-$2}"; [[ "$a" =~ ^[Yy] ]]
}
say() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# ----------------------------------------------------------------------------- 1
say "1. Software"
PKGS="python3-flask python3-numpy python3-opencv python3-gi gir1.2-aravis-0.8 aravis-tools python3-gpiozero python3-lgpio python3-serial build-essential libglib2.0-dev liblgpio-dev"
MISSING=""
for p in $PKGS; do dpkg -s "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"; done
if [ -z "$MISSING" ]; then echo "  all installed"
elif ask "  install:$MISSING ?" y; then apt-get update && apt-get install -y $MISSING
fi

# ----------------------------------------------------------------------------- 2
say "2. Relay pins safe during boot"
CFG=/boot/firmware/config.txt; [ -f "$CFG" ] || CFG=/boot/config.txt
LINE="gpio=5,6,23,26=op,dh"
if grep -q "^gpio=17,22,23,27=op,dh" "$CFG"; then          # old wiring - those pins now drive the motors
  cp "$CFG" "$CFG.bak.$(date +%Y%m%d%H%M%S)"
  sed -i '/^gpio=17,22,23,27=op,dh/d; /relay pins high (= relays off) from boot/d' "$CFG"
  echo "  removed the old relay line (gpio=17,22,23,27) - GPIO 17/22/27 are motor pins now"
  REBOOT=1
fi
if grep -q "^gpio=5,6,23,26=op,dh" "$CFG"; then echo "  already in $CFG"
else
  echo "  The relay board is active-low: until the station starts, a floating GPIO can switch a relay"
  echo "  (or the air valve) ON while the Pi boots. This line in $CFG holds them HIGH = OFF."
  if ask "  add '$LINE' to $CFG ?" y; then
    cp "$CFG" "$CFG.bak.$(date +%Y%m%d%H%M%S)"
    printf '\n[all]\n# can inspection station: relay pins high (= relays off) from boot\n%s\n' "$LINE" >> "$CFG"
    REBOOT=1; echo "  added (backup next to it)"
  fi
fi
MLINE="gpio=4,12=op,dl"
if grep -q "^$MLINE" "$CFG"; then echo "  motor HAT drivers off at boot: already in $CFG"
elif ask "  keep the Motor HAT drivers OFF while the Pi boots ('$MLINE')?" y; then
  cp "$CFG" "$CFG.bak.$(date +%Y%m%d%H%M%S)"
  printf '# can inspection station: motor HAT drivers off (EN low) from boot\n%s\n' "$MLINE" >> "$CFG"
  REBOOT=1; echo "  added"
fi

# ----------------------------------------------------------------------------- 2b
say "2b. Motor program (team C code in motor_c/)"
if make -C "$DIR/motor_c" >/dev/null 2>&1; then
  chown -R "$USER_NAME" "$DIR/motor_c" 2>/dev/null; echo "  built motor_c/motord"
else
  echo "  ! could not build motor_c - run: make -C $DIR/motor_c  to see why"
fi

# ----------------------------------------------------------------------------- 3
say "3. Network buffers for the camera"
SYS=/etc/sysctl.d/60-gige-camera.conf
if [ -f "$SYS" ]; then echo "  already set ($SYS)"
elif ask "  set 32 MB receive buffers (prevents incomplete camera frames)?" y; then
  printf 'net.core.rmem_max=33554432\nnet.core.rmem_default=33554432\n' > "$SYS"
  sysctl -q --system && echo "  done"
fi

# ----------------------------------------------------------------------------- 4
say "4. Ethernet port for the camera"
CON="$(nmcli -t -f NAME,DEVICE con show 2>/dev/null | awk -F: '$2=="eth0"{print $1; exit}')"
[ -z "$CON" ] && CON="$(nmcli -t -f NAME,TYPE con show 2>/dev/null | awk -F: '$2 ~ /ethernet/{print $1; exit}')"
if [ -z "$CON" ]; then
  echo "  no NetworkManager Ethernet connection found - creating one"; CON="camera"
  nmcli con add type ethernet ifname eth0 con-name "$CON" >/dev/null
fi
CUR="$(nmcli -g ipv4.method,ipv4.addresses con show "$CON" | tr '\n' ' ')"
echo "  connection '$CON': $CUR"
if echo "$CUR" | grep -q "169.254.100.1/16"; then
  echo "  already set for the camera"
  nmcli con mod "$CON" connection.autoconnect yes ipv4.dad-timeout 0 >/dev/null 2>&1
  for other in $(nmcli -t -f NAME,TYPE con show | awk -F: -v c="$CON" '$2 ~ /ethernet/ && $1 != c {print $1}'); do
    nmcli con mod "$other" connection.autoconnect no >/dev/null 2>&1   # e.g. netplan-eth0 (DHCP) would take eth0
  done
  if ! ip -4 addr show eth0 | grep -q "169.254.100.1"; then
    nmcli con up "$CON" >/dev/null 2>&1 && echo "  eth0 brought up: 169.254.100.1" || echo "  ! could not bring '$CON' up (cable / camera power?)"
  fi
else
  echo "  The camera talks link-local (169.254.x.x). With no DHCP server, eth0 would get no address."
  echo "  This gives eth0 169.254.100.1/16 and no internet route (internet stays on Wi-Fi)."
  if [ -n "${SSH_CONNECTION:-}" ] && ip -4 addr show eth0 | grep -q "${SSH_CONNECTION%% *}"; then :; fi
  if [ -n "${SSH_CONNECTION:-}" ]; then echo "  (you are on SSH - if that SSH runs over the Ethernet cable it will drop; use Wi-Fi)"; fi
  if ask "  configure eth0 for the camera?" y; then
    nmcli con mod "$CON" ipv4.method manual ipv4.addresses 169.254.100.1/16 ipv4.gateway "" \
      ipv4.never-default yes ipv6.method disabled connection.autoconnect yes
    nmcli con up "$CON" >/dev/null 2>&1 && echo "  done - eth0 is 169.254.100.1/16" || echo "  saved (comes up when the cable is connected)"
  fi
fi

# ----------------------------------------------------------------------------- 5
say "5. GPIO permission"
if id -nG "$USER_NAME" | grep -qw gpio; then echo "  $USER_NAME is in the gpio group"
else usermod -aG gpio "$USER_NAME" && echo "  added $USER_NAME to gpio (log out and in)"; fi
chown -R "$USER_NAME":"$USER_NAME" "$DIR"

# ----------------------------------------------------------------------------- 6
say "6. Start automatically"
UNIT=/etc/systemd/system/can-inspection.service
if [ -f "$UNIT" ]; then echo "  already installed ($UNIT)"
elif ask "  start the station at boot and restart it if it ever crashes?" y; then
  cat > "$UNIT" <<EOF
[Unit]
Description=Can inspection station
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 $DIR/run.py --no-browser
Restart=on-failure
RestartSec=3
# on stop the station closes the valve and switches the relays off itself
KillSignal=SIGTERM
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload && systemctl enable can-inspection >/dev/null 2>&1 && echo "  enabled"
  echo "  control it with: sudo systemctl start|stop|restart|status can-inspection"
  echo "  logs:            journalctl -u can-inspection -f"
fi

if ask "  open a screen on the Pi in kiosk mode at login? (needs the desktop)" n; then
  if ask "  show the big line display (y) or the full dashboard (n)?" y; then PAGE="/display"; else PAGE="/"; fi
  BROWSER="$(command -v chromium-browser || command -v chromium || echo chromium)"
  CMD="sh -c 'until curl -s http://localhost:8080 >/dev/null; do sleep 1; done; $BROWSER --kiosk --noerrdialogs --disable-infobars --disable-session-crashed-bubble http://localhost:8080$PAGE'"
  install -d -o "$USER_NAME" "$USER_HOME/.config/autostart" "$USER_HOME/.config/labwc"
  cat > "$USER_HOME/.config/autostart/can-inspection-screen.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Can inspection screen
Exec=$CMD
EOF
  grep -q "can-inspection" "$USER_HOME/.config/labwc/autostart" 2>/dev/null || \
    echo "$CMD &   # can-inspection screen" >> "$USER_HOME/.config/labwc/autostart"
  chown -R "$USER_NAME":"$USER_NAME" "$USER_HOME/.config/autostart" "$USER_HOME/.config/labwc"
  command -v raspi-config >/dev/null && raspi-config nonint do_blanking 1 && echo "  screen blanking off"
  echo "  the screen opens at the next login"
fi

say "Done."
[ "$REBOOT" = 1 ] && echo "Reboot once for the boot-time relay setting:  sudo reboot"
echo "Then check everything:  python3 $DIR/pi_check.py"
