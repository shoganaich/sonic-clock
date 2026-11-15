#!/bin/bash
# Copies the weather clock to the Sonic Pad and installs it (Linux / macOS).
#
#   ./scripts/deploy.sh 192.168.1.50                 install / update
#   ./scripts/deploy.sh 192.168.1.50 --screenshots   also refresh docs/screenshots
#
# Set PAD_USER if your login is not "sonic".
set -e
HOST="${1:?usage: deploy.sh <pad-ip> [--screenshots]}"
TARGET="${PAD_USER:-sonic}@$HOST"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "Copying files to $TARGET ..."
scp -r "$ROOT/weatherclock" "$TARGET:/tmp/"

echo "Checking and installing on the Pad ..."
ssh -t "$TARGET" "python3 -m py_compile /tmp/weatherclock/weatherclock.py && sudo bash /tmp/weatherclock/install.sh"

if [ "$2" = "--screenshots" ]; then
  echo "Rendering screenshots on the Pad ..."
  ssh "$TARGET" "rm -rf /tmp/wcui && python3 /tmp/weatherclock/weatherclock.py --preview-ui /tmp/wcui"
  scp "$TARGET:/tmp/wcui/*.png" "$ROOT/docs/screenshots/"
fi
echo "Done."
