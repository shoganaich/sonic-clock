#!/bin/bash
# Removes the weather clock from the Sonic Pad.
# Run on the Pad:  sudo bash uninstall.sh
#
# Creality's display-sleep service is NOT turned back on, because it switches
# the backlight off when KlipperScreen is missing. To restore it anyway:
#   sudo systemctl enable --now display-sleep
set -e
[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }

systemctl disable --now weatherclock.service || true
rm -f /etc/systemd/system/weatherclock.service
systemctl daemon-reload
rm -rf /opt/weatherclock
echo "Weather clock removed (its settings too)."
