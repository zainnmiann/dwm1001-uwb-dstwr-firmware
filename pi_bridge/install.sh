#!/bin/bash
# Install the UWB->MQTT bridge on a Raspberry Pi. Run with sudo on the Pi.
set -euo pipefail

RUN_USER="${SUDO_USER:-pi}"
echo "==> installing for user: $RUN_USER"

echo "==> dependencies"
apt-get update -qq
apt-get install -y python3-serial python3-paho-mqtt

echo "==> freeing the GPIO UART"
CONFIG=/boot/firmware/config.txt; [ -f "$CONFIG" ] || CONFIG=/boot/config.txt
CMDLINE=/boot/firmware/cmdline.txt; [ -f "$CMDLINE" ] || CMDLINE=/boot/cmdline.txt

grep -q '^enable_uart=1' "$CONFIG" || echo 'enable_uart=1' >> "$CONFIG"
# Hand the stable PL011 to the GPIO pins instead of the clock-sensitive mini-UART.
grep -q '^dtoverlay=disable-bt' "$CONFIG" || echo 'dtoverlay=disable-bt' >> "$CONFIG"

# The serial console would fight the tag for the same pins.
sed -i 's/console=serial0,[0-9]* //; s/console=ttyAMA0,[0-9]* //' "$CMDLINE"
systemctl disable --now hciuart 2>/dev/null || true
systemctl disable --now serial-getty@ttyAMA0.service 2>/dev/null || true
systemctl disable --now serial-getty@ttyS0.service 2>/dev/null || true
usermod -aG dialout "$RUN_USER"

echo "==> files"
install -d /opt/uwb
install -m 0755 uwb_bridge.py /opt/uwb/uwb_bridge.py
# Surveyed anchor coordinates: use a local copy if present, else the one in host/.
for a in uwb_anchors.json; do
  [ -f "$a" ] && { install -m 0644 "$a" /opt/uwb/uwb_anchors.json; break; }
done
[ -f /opt/uwb/uwb_anchors.json ] || echo "WARNING: no uwb_anchors.json installed; anchors will be labelled by address"
[ -f /etc/default/uwb-bridge ] || install -m 0644 uwb-bridge.env /etc/default/uwb-bridge

echo "==> service"
sed "s/User=%i/User=$RUN_USER/" uwb-bridge.service > /etc/systemd/system/uwb-bridge.service
systemctl daemon-reload
systemctl enable uwb-bridge.service

echo
echo "Installed. A REBOOT is required for the UART change to take effect."
echo "After reboot:  systemctl status uwb-bridge   /   journalctl -u uwb-bridge -f"
