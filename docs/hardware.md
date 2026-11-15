# Sonic Pad hardware notes

What was found while getting the display working on a Sonic Pad running Debian 11,
installed with [Jpe230/SonicPad-Debian](https://github.com/Jpe230/SonicPad-Debian).
Some details (such as `display-sleep.service` and `/usr/bin/brightness`) come from
that image and may differ on other builds. Collected with [`tools/sonicpad-diagnose.sh`](../tools/sonicpad-diagnose.sh).

## System

| Item | Value |
|---|---|
| SoC | Allwinner, `sun50iw10p1` (4 × ARMv8 @ 1.5 GHz, PowerVR GPU) |
| RAM / storage | 2 GB / 8 GB eMMC (about 2.5 GB free on the root filesystem) |
| OS | Debian 11 (bullseye), kernel 4.9.191 |
| Userland | Mostly **32-bit ARM (`armhf`)** on a 64-bit kernel. `python3` is 32-bit. |
| Python | 3.9.2 with Pillow 8.1.2. No pygame, no requests. |
| Boot target | `multi-user.target`, no display manager |
| Serial console | `ttyS0` at 115200 |

## Display

- The panel is **1024 × 600, landscape**. `/sys/class/graphics/fb0/virtual_size`
  says `1024,1200` because the driver reserves two 600-line buffers.
- `/dev/fb0`, 32 bpp, stride 4096, pixel order **BGRA** (red offset 16).
- Allwinner display engine at `/dev/disp`; status in `/sys/class/disp/disp/attr/sys`
  (shows `lcd output`, `backlight(255)`, `unblank`).
- There is **no framebuffer console** (`vtconsole` is only the dummy device), so
  nothing draws on the screen unless an app writes to `/dev/fb0`.
- SDL2 in Debian 11 has no framebuffer driver and `/dev/dri/card0` is the GPU, not a
  display controller, so pygame/SDL can't draw here. The app renders with Pillow and
  writes raw pixels to `/dev/fb0`.

## Backlight

- No `/sys/class/backlight/`. The backlight is controlled through `/dev/disp`
  ioctls, wrapped by Creality's `/usr/bin/brightness` (a 32-bit binary):

  ```
  brightness -s 1        backlight on   (-s 0 = off)
  brightness -v 0..100   brightness in percent
  brightness -d 0..255   brightness, raw range
  ```

  A bare value (`brightness 100`) is rejected with "brightness value is error".
- PWM channel `pwm-0` on `300a000.pwm` runs at about 99.6 % duty at full brightness.

## Why the screen was black

`display-sleep.service` runs `/usr/local/bin/display-sleep.sh`. It was written for
KlipperScreen: every 2 seconds it asks the X display `:0` whether the monitor is on
(`xset -q`) and calls `brightness -s 0` or `-s 1` to match.

With KlipperScreen and X gone, `xset` returns nothing, the script decides the
monitor is "Off", and it switches the backlight off about 2 seconds after every
boot. Disabling the service (the installer does this) fixes it.

Test that proved it (undone by a reboot):

```bash
sudo systemctl stop display-sleep
sudo brightness -s 1
sudo dd if=/dev/urandom of=/dev/fb0 bs=4096 count=600   # coloured noise on screen
```

## Touchscreen

- Goodix GT911 (`gt9xxnew_ts`) on I²C bus 1, address `0x5d`, `/dev/input/event2`.
- Multi-touch protocol: `ABS_MT_POSITION_X/Y`, `ABS_MT_TRACKING_ID` and `BTN_TOUCH`.
- Coordinate range 0–1024 × 0–600, matching the screen with no flips or swaps.
- Other input devices: `sunxi-keyboard` (event0) and the power key `axp806-pek` (event1).

## Network

- Wi-Fi: XR819 chip (`xr819_wlan` module), interface `wlan0`, managed by NetworkManager
  (`nmcli` works for scanning and joining). `eth0` exists but was unused.
- After a reboot Wi-Fi takes roughly a minute to reconnect and resolve DNS, so the
  app retries every 10 seconds until its first weather update.

## Leftover Creality services

Still enabled and safe to disable if you don't need them:

| Service | What it is |
|---|---|
| `klipper-mcu.service` | Klipper host MCU process for the 3D printer |
| `nginx.service` | Web server used by the printer web UI |
| `ModemManager.service` | Mobile-data modem support |

```bash
sudo systemctl disable --now klipper-mcu nginx ModemManager
```
