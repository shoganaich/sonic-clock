#!/bin/bash
# Installs the weather clock and turns off Creality's display-sleep service.
# Run on the Sonic Pad:  sudo bash install.sh
# Undo:  sudo systemctl disable --now weatherclock; sudo systemctl enable --now display-sleep
set -e
[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }
DIR="$(cd "$(dirname "$0")" && pwd)"

install -d /opt/weatherclock
install -m 755 "$DIR/weatherclock.py" /opt/weatherclock/weatherclock.py
install -m 644 "$DIR/weatherclock.service" /etc/systemd/system/weatherclock.service

systemctl disable --now display-sleep.service || true
systemctl daemon-reload
systemctl enable weatherclock.service
systemctl restart weatherclock.service
sleep 8
systemctl --no-pager status weatherclock.service | head -n 15
