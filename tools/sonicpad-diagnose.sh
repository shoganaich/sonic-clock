#!/bin/bash
# Creality Sonic Pad - READ-ONLY display/system diagnostic
# Changes nothing: it only reads files and runs status commands.
# Usage (on the Sonic Pad, as root):  bash sonicpad-diagnose.sh
# Output: /tmp/sonicpad-diag.txt

OUT=/tmp/sonicpad-diag.txt
: > "$OUT"

section() { printf '\n\n===== %s =====\n' "$1" >> "$OUT"; }
run() {
  printf '\n$ %s\n' "$*" >> "$OUT"
  timeout 20 bash -c "$*" >> "$OUT" 2>&1
}
showfile() {
  for f in "$@"; do
    [ -e "$f" ] || { printf '\n[missing] %s\n' "$f" >> "$OUT"; continue; }
    printf '\n--- %s ---\n' "$f" >> "$OUT"
    if file -b "$f" 2>/dev/null | grep -qi text; then
      cat "$f" >> "$OUT" 2>&1
    else
      file "$f" >> "$OUT" 2>&1
      echo "[binary - printable strings:]" >> "$OUT"
      strings -n 4 "$f" 2>/dev/null | head -n 150 >> "$OUT"
    fi
  done
}
dtprops() {  # print device-tree node properties as text
  local node="$1"
  [ -d "$node" ] || return
  printf '\n--- DT node %s ---\n' "$node" >> "$OUT"
  for p in "$node"/*; do
    [ -f "$p" ] || continue
    printf '%s = %s\n' "$(basename "$p")" \
      "$(tr '\0' ' ' < "$p" 2>/dev/null | tr -cd '[:print:] ' | head -c 200)" >> "$OUT"
  done
}

section "SYSTEM"
run "uname -a"
run "cat /etc/os-release"
run "cat /proc/cmdline"
run "uptime; free -m; df -h"
run "timedatectl"

section "BOOT TARGET AND SERVICES"
run "systemctl get-default"
run "systemctl list-units --type=service --state=running --no-pager"
run "systemctl list-unit-files --state=enabled --no-pager"
run "systemctl --failed --no-pager"

section "DISPLAY-SLEEP SCRIPT (prime suspect)"
run "ps -eo pid,ppid,user,etime,args | grep -Ei 'display|bright|sleep|klipper|screen|lcd' | grep -v grep"
run "grep -rIl 'display-sleep' /etc /lib/systemd /usr/lib/systemd /usr/local /root /home 2>/dev/null"
showfile /usr/local/bin/display-sleep.sh
for u in $(grep -rIl 'display-sleep' /etc/systemd /lib/systemd /usr/lib/systemd 2>/dev/null); do showfile "$u"; done
showfile /etc/rc.local

section "BRIGHTNESS UTILITY"
run "ls -la /usr/bin/brightness"
showfile /usr/bin/brightness
run "dpkg -S /usr/bin/brightness"

section "FRAMEBUFFER"
run "ls -la /dev/fb* /dev/disp /dev/dri /dev/tty0 /dev/tty1"
run "cat /proc/fb"
for a in name virtual_size bits_per_pixel stride blank mode modes rotate state pan; do
  run "cat /sys/class/graphics/fb0/$a"
done
run "ls -la /sys/class/graphics/"
run "for v in /sys/class/vtconsole/vtcon*; do echo \$v: \$(cat \$v/name) bind=\$(cat \$v/bind); done"
run "cat /sys/class/tty/console/active"
run "systemctl status getty@tty1 --no-pager"

section "SUNXI DISPLAY ENGINE"
run "ls -la /sys/class/disp/disp/attr/"
run "cat /sys/class/disp/disp/attr/sys"
run "ls -la /sys/kernel/debug/dispdbg 2>/dev/null"

section "BACKLIGHT / LEDS / PWM"
run "ls -la /sys/class/backlight/ /sys/class/leds/ /sys/class/pwm/"
run "for f in /sys/class/leds/*/brightness /sys/class/leds/*/max_brightness /sys/class/leds/*/trigger; do echo \$f: \$(cat \$f); done"
run "cat /sys/kernel/debug/pwm 2>/dev/null"

section "DEVICE TREE (lcd / disp / backlight)"
run "find /proc/device-tree -maxdepth 3 \\( -iname '*lcd*' -o -iname '*disp*' -o -iname '*backlight*' -o -iname '*pwm*' -o -iname '*ctp*' -o -iname '*gt9*' \\) 2>/dev/null"
for n in $(find /proc/device-tree -maxdepth 3 -type d \( -iname 'lcd0*' -o -iname 'disp*' -o -iname '*backlight*' -o -iname 'ctp*' \) 2>/dev/null); do
  dtprops "$n"
done

section "KERNEL LOG (display related)"
run "dmesg | grep -iE 'lcd|disp|fb|backlight|pwm|panel|dsi|lvds|edp|pvr|tcon|hdmi' | tail -n 200"
run "dmesg | grep -iE 'gt9|goodix|ctp|touch|input' | tail -n 60"
run "lsmod"

section "INPUT DEVICES"
run "cat /proc/bus/input/devices"
run "ls -la /dev/input/ /dev/input/by-path 2>/dev/null"

section "NETWORK"
run "ip -brief addr; ip route"
run "cat /etc/resolv.conf"
run "ping -c 2 -W 3 1.1.1.1"
run "getent hosts api.open-meteo.com"
run "curl -sS -m 15 'https://api.open-meteo.com/v1/forecast?latitude=37.9567&longitude=-8.8698&current=temperature_2m&timezone=Europe%2FLisbon' || wget -qO- -T 15 'https://api.open-meteo.com/v1/forecast?latitude=37.9567&longitude=-8.8698&current=temperature_2m&timezone=Europe%2FLisbon'"

section "PYTHON / GRAPHICS LIBRARIES"
run "python3 --version"
run "python3 -c 'import pygame; print(pygame.version.ver, pygame.get_sdl_version())'"
run "python3 -c 'import PIL; print(\"Pillow\", PIL.__version__)'"
run "python3 -c 'import requests; print(\"requests\", requests.__version__)'"
run "dpkg -l | grep -Ei 'sdl|pygame|python3-pil|fonts-|libdrm|evdev' "
run "fc-list 2>/dev/null | head -n 30"

section "LEFTOVER CREALITY / KLIPPER FILES"
run "find / -xdev -maxdepth 4 \\( -iname '*creality*' -o -iname '*klipper*' -o -iname '*sonic*' \\) 2>/dev/null | head -n 100"
run "ls -la /usr/local/bin /etc/init.d"

echo "Done. Report written to $OUT ($(wc -l < "$OUT") lines)."
