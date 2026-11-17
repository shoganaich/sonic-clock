#!/usr/bin/env python3
"""Weather clock for a Creality Sonic Pad.

Draws straight to the Linux framebuffer (/dev/fb0) with Pillow, so it needs
no X server, desktop or extra packages. Weather and air quality come from
Open-Meteo; official warnings from MeteoAlarm and the US National Weather Service.

  python3 weatherclock.py                  run fullscreen (as root)
  python3 weatherclock.py --preview-ui DIR render every screen to PNGs

Swipe left or right to move between the dashboard, next 24 hours, sea and timer
pages. Swipe right on the dashboard, or tap the gear next to the page dots, to
open Settings.
"""

import hmac
import html
import io
import ipaddress
import json
import math
import os
import queue
import re
import secrets
import shutil
import struct
import subprocess
import sys
import tarfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------- constants
W, H = 1024, 600
M = 32                                     # outer margin
FB_DEV = "/dev/fb0"
TOUCH_DEV = "/dev/input/event2"            # gt9xxnew_ts
BRIGHTNESS_BIN = "/usr/bin/brightness"
WIFI_IF = "wlan0"
WAKE_SECS = 30                             # full screen after a tap at night
SCREEN_OFF_SECS = 10                       # in the screen-off hours, backlight off this long after a touch
SETTINGS_PAGES = 3
LONG_PRESS = 2.0                           # seconds held to open Settings
IDLE_SECS = 180                            # settings screens return to the clock
REFRESH_OK, REFRESH_FAIL = 600, 60         # seconds between weather fetches
TOUCH_SWAP_XY = os.environ.get("CLOCK_TOUCH_SWAP_XY") == "1"
TOUCH_INVERT_X = os.environ.get("CLOCK_TOUCH_INVERT_X") == "1"
TOUCH_INVERT_Y = os.environ.get("CLOCK_TOUCH_INVERT_Y") == "1"
VERSION = "1.2.0"                          # bump together with the GitHub Release tag (v1.2.0)
UPDATE_REPO = "shoganaich/sonic-clock"     # GitHub repo whose Releases the Pad can install
UPDATE_TRIES = 3                           # starts an update gets before the old version is put back
UPDATE_OK_SECS = 120                       # running this long after an update means it works
WEB_PORT = 8080                            # phone settings page (only while turned on)
SWIPE_MIN = 120                            # pixels a finger must travel to count as a swipe
TIMER_CHOICES = (5, 10, 15, 25)            # minutes
RING_SECS = 120                            # how long a finished timer flashes
SHIFTS = [(0, 0), (3, 0), (3, 3), (0, 3), (-3, 3), (-3, 0), (-3, -3), (0, -3), (3, -3)]


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- saved settings
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
UPDATE_MARK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "update-pending.json")
DEFAULTS = {
    "theme": "Midnight",
    "place": "Sines, Portugal",
    "lat": 37.9567, "lon": -8.8698, "tz": "Europe/Lisbon",
    "day_bright": 100, "night_bright": 35,
    "night_mode": "bedside", "night_hours": "sun",
    "rotate": int(os.environ.get("CLOCK_ROTATE", "0")),
    "cc": "PT", "regions": ["Setúbal District", "Sines Municipality"],  # for official warnings
    "sea": False, "rain_alerts": True, "warnings": True,
    "night_theme": "", "pixel_shift": True, "web": False, "animations": True,
    "screen_off": False, "off_from": 23 * 60, "off_until": 7 * 60,   # minutes after midnight
}
NIGHT_MODES = [("bedside", "Bedside clock"), ("dim", "Dim dashboard"), ("off", "Off")]
NIGHT_HOURS = [("sun", "Sunset to sunrise"), ("22", "22:00 to 07:00"), ("23", "23:00 to 07:00")]
ROTATIONS = [(0, "Normal"), (180, "Upside down")]


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE) as f:
            s.update(json.load(f))
    except (OSError, ValueError):
        pass
    return s


SET = load_settings()
SETTINGS_LOCK = threading.Lock()  # the touchscreen and the phone page can save at the same time


def save_settings():
    with SETTINGS_LOCK:
        try:
            tmp = SETTINGS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(dict(SET), f, indent=2)
                f.flush()
                os.fsync(f.fileno())  # the root filesystem is read/write: survive a power cut
            os.replace(tmp, SETTINGS_FILE)
        except OSError as e:
            log("could not save settings:", e)


def label_of(options, value):
    for v, name in options:
        if v == value:
            return name
    return options[0][1]


_tz = {}


def local_now():
    """Current time at the chosen place (falls back to system time)."""
    try:
        from zoneinfo import ZoneInfo
        name = SET["tz"]
        if name not in _tz:
            _tz[name] = ZoneInfo(name)
        return datetime.now(_tz[name]).replace(tzinfo=None)
    except Exception:
        return datetime.now()


# ---------------------------------------------------------------- themes
THEMES = {
    "Midnight": dict(day=((24, 40, 68), (9, 12, 20)), night=((14, 16, 34), (5, 6, 11)),
                     text=(238, 241, 247), muted=(140, 150, 170), dim=(92, 100, 120),
                     card=(28, 35, 52), key=(46, 55, 78), field=(20, 25, 38), accent=(44, 108, 220),
                     active=(34, 60, 104), cloud=(216, 223, 236), cloud2=(138, 149, 172),
                     alert=(92, 52, 34), bedside=(255, 150, 60)),
    "Ocean": dict(day=((10, 62, 78), (3, 20, 30)), night=((6, 32, 42), (2, 10, 14)),
                  text=(230, 246, 248), muted=(128, 172, 180), dim=(78, 116, 124),
                  card=(13, 52, 64), key=(22, 74, 88), field=(7, 32, 40), accent=(0, 150, 168),
                  active=(10, 92, 106), cloud=(220, 236, 240), cloud2=(118, 160, 170),
                  alert=(96, 58, 30), bedside=(90, 200, 210)),
    "Sunset": dict(day=((82, 40, 86), (28, 14, 32)), night=((42, 20, 50), (10, 6, 14)),
                   text=(252, 238, 232), muted=(204, 162, 172), dim=(142, 108, 122),
                   card=(70, 36, 74), key=(98, 52, 98), field=(40, 20, 44), accent=(232, 104, 76),
                   active=(122, 56, 92), cloud=(246, 228, 232), cloud2=(178, 140, 162),
                   alert=(124, 50, 40), bedside=(255, 140, 90)),
    "Forest": dict(day=((26, 60, 42), (8, 20, 14)), night=((14, 32, 22), (4, 10, 7)),
                   text=(232, 244, 234), muted=(140, 176, 150), dim=(90, 120, 100),
                   card=(26, 54, 38), key=(38, 76, 54), field=(14, 32, 22), accent=(62, 160, 92),
                   active=(34, 92, 58), cloud=(226, 238, 228), cloud2=(132, 162, 140),
                   alert=(100, 64, 30), bedside=(230, 170, 80)),
    "Graphite": dict(day=((46, 46, 50), (16, 16, 18)), night=((24, 24, 26), (6, 6, 7)),
                     text=(242, 242, 242), muted=(152, 152, 158), dim=(100, 100, 106),
                     card=(40, 40, 44), key=(60, 60, 66), field=(26, 26, 28), accent=(96, 110, 140),
                     active=(66, 70, 84), cloud=(226, 226, 230), cloud2=(140, 140, 148),
                     alert=(92, 58, 40), bedside=(210, 210, 210)),
    "Black": dict(day=((0, 0, 0), (0, 0, 0)), night=((0, 0, 0), (0, 0, 0)),
                  text=(236, 236, 236), muted=(132, 132, 132), dim=(80, 80, 80),
                  card=(18, 18, 18), key=(34, 34, 34), field=(12, 12, 12), accent=(40, 110, 230),
                  active=(22, 42, 80), cloud=(220, 220, 226), cloud2=(120, 120, 128),
                  alert=(72, 36, 20), bedside=(255, 120, 40)),
    "Daylight": dict(day=((238, 243, 250), (206, 218, 234)), night=((204, 214, 228), (172, 184, 202)),
                     text=(24, 30, 44), muted=(88, 100, 122), dim=(128, 138, 158),
                     card=(250, 252, 255), key=(222, 229, 240), field=(255, 255, 255),
                     accent=(40, 110, 230), active=(204, 222, 250), cloud=(156, 168, 188),
                     cloud2=(112, 124, 146), alert=(255, 220, 192), bedside=(255, 150, 60),
                     moon=(110, 120, 150), moon_dark=(222, 228, 238), snow=(110, 150, 210)),
    "Nord": dict(day=((52, 60, 76), (34, 38, 48)), night=((40, 45, 56), (20, 23, 29)),
                 text=(236, 239, 244), muted=(164, 174, 190), dim=(108, 118, 136),
                 card=(59, 66, 82), key=(70, 79, 98), field=(46, 52, 64), accent=(94, 129, 172),
                 active=(76, 90, 116), cloud=(229, 233, 240), cloud2=(136, 160, 190),
                 alert=(120, 72, 62), bedside=(235, 203, 139)),
    "Dracula": dict(day=((44, 46, 60), (22, 23, 31)), night=((30, 31, 41), (12, 12, 17)),
                    text=(248, 248, 242), muted=(170, 168, 200), dim=(110, 112, 140),
                    card=(54, 57, 74), key=(70, 73, 94), field=(34, 35, 46), accent=(150, 110, 230),
                    active=(84, 72, 120), cloud=(232, 232, 242), cloud2=(150, 150, 184),
                    alert=(112, 50, 64), bedside=(255, 184, 108)),
    "Solarized": dict(day=((0, 50, 62), (0, 28, 36)), night=((0, 34, 43), (0, 16, 21)),
                      text=(238, 232, 213), muted=(147, 161, 161), dim=(96, 116, 122),
                      card=(7, 58, 71), key=(18, 74, 88), field=(0, 38, 48), accent=(38, 139, 210),
                      active=(16, 82, 104), cloud=(238, 232, 213), cloud2=(131, 148, 150),
                      alert=(112, 64, 18), bedside=(203, 150, 40)),
    "Rose": dict(day=((70, 32, 52), (24, 10, 18)), night=((40, 18, 30), (10, 4, 8)),
                 text=(255, 236, 242), muted=(216, 162, 184), dim=(150, 104, 124),
                 card=(76, 36, 56), key=(102, 50, 76), field=(44, 18, 32), accent=(222, 68, 140),
                 active=(124, 48, 86), cloud=(252, 232, 240), cloud2=(186, 140, 162),
                 alert=(130, 54, 40), bedside=(255, 150, 160)),
    "Aurora": dict(day=((14, 44, 62), (32, 16, 52)), night=((8, 26, 38), (16, 8, 28)),
                   text=(232, 248, 244), muted=(150, 196, 194), dim=(96, 134, 140),
                   card=(24, 48, 68), key=(36, 64, 88), field=(12, 28, 40), accent=(28, 170, 120),
                   active=(24, 92, 92), cloud=(226, 242, 240), cloud2=(132, 170, 176),
                   alert=(104, 60, 40), bedside=(120, 230, 180)),
    "Amber": dict(day=((16, 11, 4), (4, 3, 1)), night=((8, 6, 2), (0, 0, 0)),
                  text=(255, 178, 20), muted=(196, 132, 24), dim=(124, 84, 12),
                  card=(30, 21, 6), key=(46, 32, 9), field=(20, 14, 4), accent=(200, 110, 0),
                  active=(66, 44, 8), cloud=(255, 196, 90), cloud2=(176, 124, 42),
                  alert=(96, 40, 8), bedside=(255, 150, 20)),
    "Terminal": dict(day=((4, 16, 8), (0, 4, 2)), night=((2, 8, 4), (0, 0, 0)),
                     text=(90, 250, 130), muted=(44, 176, 86), dim=(26, 112, 54),
                     card=(8, 28, 14), key=(12, 44, 22), field=(4, 16, 8), accent=(24, 150, 70),
                     active=(16, 64, 32), cloud=(160, 244, 180), cloud2=(70, 156, 98),
                     alert=(70, 48, 10), bedside=(70, 210, 100)),
    "Paper": dict(day=((248, 243, 232), (230, 221, 202)), night=((222, 213, 194), (196, 186, 166)),
                  text=(42, 37, 30), muted=(112, 101, 86), dim=(160, 150, 130),
                  card=(255, 252, 244), key=(236, 228, 212), field=(255, 255, 250),
                  accent=(176, 88, 40), active=(242, 222, 192), cloud=(172, 162, 146),
                  cloud2=(126, 116, 100), alert=(255, 214, 180), bedside=(255, 150, 60),
                  moon=(122, 112, 96), moon_dark=(232, 224, 208), snow=(110, 146, 200)),
    "Sky": dict(day=((130, 186, 240), (204, 230, 250)), night=((96, 140, 196), (160, 196, 230)),
                text=(16, 40, 72), muted=(52, 82, 122), dim=(100, 130, 170),
                card=(236, 246, 255), key=(212, 229, 248), field=(255, 255, 255),
                accent=(20, 100, 220), active=(192, 216, 250), cloud=(150, 170, 200),
                cloud2=(110, 130, 166), alert=(255, 222, 196), bedside=(255, 150, 60),
                moon=(90, 110, 150), moon_dark=(220, 232, 245), snow=(90, 130, 200)),
    "Lavender": dict(day=((54, 46, 84), (20, 17, 34)), night=((30, 26, 48), (9, 8, 16)),
                     text=(244, 240, 255), muted=(182, 172, 214), dim=(122, 114, 156),
                     card=(60, 52, 92), key=(80, 70, 118), field=(36, 31, 58), accent=(124, 88, 214),
                     active=(88, 72, 140), cloud=(236, 232, 250), cloud2=(156, 146, 190),
                     alert=(110, 56, 60), bedside=(200, 170, 255)),
    "Coffee": dict(day=((58, 40, 30), (22, 15, 11)), night=((32, 22, 16), (10, 7, 5)),
                   text=(246, 234, 218), muted=(196, 170, 144), dim=(134, 112, 92),
                   card=(64, 45, 34), key=(86, 62, 47), field=(38, 27, 20), accent=(166, 100, 44),
                   active=(104, 70, 44), cloud=(240, 228, 212), cloud2=(170, 148, 126),
                   alert=(120, 56, 34), bedside=(240, 170, 100)),
    "Ember": dict(day=((48, 18, 14), (14, 6, 5)), night=((26, 9, 7), (6, 2, 2)),
                  text=(255, 232, 220), muted=(210, 150, 130), dim=(140, 92, 80),
                  card=(56, 24, 20), key=(80, 34, 28), field=(32, 12, 10), accent=(200, 70, 28),
                  active=(110, 40, 26), cloud=(250, 224, 212), cloud2=(180, 130, 116),
                  alert=(120, 44, 20), bedside=(255, 110, 50)),
    "Indigo": dict(day=((28, 30, 92), (8, 9, 34)), night=((16, 17, 54), (4, 4, 16)),
                   text=(236, 240, 255), muted=(152, 162, 214), dim=(96, 104, 156),
                   card=(34, 38, 98), key=(48, 54, 126), field=(20, 22, 62), accent=(14, 128, 186),
                   active=(32, 70, 140), cloud=(226, 232, 252), cloud2=(140, 150, 200),
                   alert=(110, 54, 46), bedside=(120, 200, 255)),
    "Olive": dict(day=((50, 54, 28), (18, 20, 9)), night=((28, 30, 15), (7, 8, 4)),
                  text=(244, 242, 222), muted=(184, 184, 146), dim=(122, 124, 92),
                  card=(56, 60, 32), key=(76, 82, 44), field=(32, 35, 18), accent=(140, 108, 18),
                  active=(92, 90, 36), cloud=(238, 236, 216), cloud2=(160, 160, 128),
                  alert=(114, 62, 26), bedside=(230, 190, 90)),
    "Copper": dict(day=((16, 52, 56), (5, 20, 22)), night=((8, 30, 32), (2, 9, 10)),
                   text=(236, 246, 244), muted=(148, 184, 180), dim=(92, 126, 124),
                   card=(20, 58, 62), key=(30, 78, 82), field=(10, 34, 36), accent=(176, 88, 44),
                   active=(90, 70, 52), cloud=(226, 240, 238), cloud2=(128, 164, 162),
                   alert=(112, 58, 30), bedside=(240, 150, 90)),
    "Mint": dict(day=((232, 246, 240), (198, 226, 214)), night=((198, 220, 210), (166, 194, 182)),
                 text=(18, 48, 38), muted=(66, 104, 90), dim=(118, 150, 138),
                 card=(248, 255, 252), key=(216, 236, 228), field=(255, 255, 255),
                 accent=(16, 128, 92), active=(196, 232, 216), cloud=(150, 176, 166),
                 cloud2=(106, 134, 124), alert=(255, 222, 196), bedside=(255, 150, 60),
                 moon=(96, 124, 114), moon_dark=(220, 236, 230), snow=(100, 150, 200)),
    "Contrast": dict(day=((0, 0, 0), (0, 0, 0)), night=((0, 0, 0), (0, 0, 0)),
                     text=(255, 255, 255), muted=(222, 222, 222), dim=(170, 170, 170),
                     card=(26, 26, 26), key=(56, 56, 56), field=(0, 0, 0), accent=(0, 88, 200),
                     active=(30, 60, 124), cloud=(255, 255, 255), cloud2=(190, 190, 190),
                     alert=(124, 62, 0), bedside=(255, 214, 0)),
}
THEME_ORDER = ["Midnight", "Ocean", "Sunset", "Forest", "Graphite", "Black", "Daylight", "Nord",
               "Dracula", "Solarized", "Rose", "Aurora", "Amber", "Terminal", "Paper", "Sky",
               "Lavender", "Coffee", "Ember", "Indigo", "Olive", "Copper", "Mint", "Contrast"]

SUN = (255, 190, 70)
RAIN = (96, 165, 255)
BOLT = (255, 206, 64)
WARN = (255, 120, 110)
ON_ACCENT = (255, 255, 255)
THEME_NAME = None


def apply_theme(name):
    global THEME_NAME, T, TEXT, MUTED, DIM, CARD, KEY, FIELD, ACCENT, ACTIVE
    global CLOUD, CLOUD2, ALERT, BEDSIDE, MOON, SNOW
    if name not in THEMES:
        name = "Midnight"
    THEME_NAME, T = name, THEMES[name]
    TEXT, MUTED, DIM = T["text"], T["muted"], T["dim"]
    CARD, KEY, FIELD, ACCENT, ACTIVE = T["card"], T["key"], T["field"], T["accent"], T["active"]
    CLOUD, CLOUD2, ALERT, BEDSIDE = T["cloud"], T["cloud2"], T["alert"], T["bedside"]
    MOON = T.get("moon", (222, 228, 245))
    SNOW = T.get("snow", (242, 246, 255))


apply_theme(SET["theme"])

# ---------------------------------------------------------------- fonts
FONT_DIRS = ["/usr/share/fonts/truetype/nanum/", "/usr/share/fonts/truetype/dejavu/",
             "C:/Windows/Fonts/"]
FONT_NAMES = {
    "heavy": ["NanumSquareRoundEB.ttf", "NanumSquareRoundB.ttf", "DejaVuSans-Bold.ttf", "segoeuib.ttf"],
    "bold": ["NanumSquareRoundB.ttf", "DejaVuSans-Bold.ttf", "segoeuib.ttf"],
    "reg": ["NanumSquareRoundR.ttf", "DejaVuSans.ttf", "segoeui.ttf"],
}
_fonts = {}


def F(kind, size):
    key = (kind, size)
    if key not in _fonts:
        for name in FONT_NAMES[kind]:
            for d in FONT_DIRS:
                if os.path.exists(d + name):
                    _fonts[key] = ImageFont.truetype(d + name, size)
                    break
            if key in _fonts:
                break
        else:
            _fonts[key] = ImageFont.load_default()
    return _fonts[key]


# NanumSquareRound has no glyphs for some ASCII symbols (` ~ ^ \ ...) and would draw
# boxes; text containing those is drawn with DejaVu Sans instead.
FALLBACK_FONTS = {
    "heavy": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "bold": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "reg": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
}
_glyphs = {}


def _render_char(font, ch):
    im = Image.new("L", (font.size * 2, font.size * 2), 0)
    ImageDraw.Draw(im).text((0, 0), ch, font=font, fill=255)
    return im.tobytes()


def has_glyphs(font, text):
    for ch in set(text):
        if ch.isspace() or ord(ch) < 128 and ch.isalnum():
            continue
        if ch == "\\":  # Korean fonts draw the backslash as a won sign
            return False
        key = (id(font), ch)
        if key not in _glyphs:
            if not hasattr(font, "size"):
                return True
            missing = _render_char(font, "\U0010FFFD")  # a character no font has: draws the box
            _glyphs[key] = _render_char(font, ch) != missing
        if not _glyphs[key]:
            return False
    return True


def font_for(text, kind, size):
    """F(kind, size), or DejaVu at the same size if F would draw boxes for text."""
    f = F(kind, size)
    if has_glyphs(f, text):
        return f
    key = ("fallback-" + kind, size)
    if key not in _fonts:
        path = FALLBACK_FONTS[kind]
        _fonts[key] = ImageFont.truetype(path, size) if os.path.exists(path) else f
    return _fonts[key]


def fit(text, kind, size, maxw, smallest=11):
    """Largest font (up to size) in which text fits maxw pixels."""
    while size > smallest and font_for(text, kind, size).getlength(text) > maxw:
        size -= 1
    return font_for(text, kind, size)


# ---------------------------------------------------------------- weather helpers
WMO = {
    0: ("Clear sky", "clear"), 1: ("Mainly clear", "clear"), 2: ("Partly cloudy", "partly"),
    3: ("Overcast", "cloudy"), 45: ("Fog", "fog"), 48: ("Freezing fog", "fog"),
    51: ("Light drizzle", "drizzle"), 53: ("Drizzle", "drizzle"), 55: ("Heavy drizzle", "drizzle"),
    56: ("Freezing drizzle", "drizzle"), 57: ("Freezing drizzle", "drizzle"),
    61: ("Light rain", "rain"), 63: ("Rain", "rain"), 65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "rain"), 67: ("Freezing rain", "rain"),
    71: ("Light snow", "snow"), 73: ("Snow", "snow"), 75: ("Heavy snow", "snow"),
    77: ("Snow grains", "snow"), 80: ("Rain showers", "rain"), 81: ("Rain showers", "rain"),
    82: ("Violent showers", "rain"), 85: ("Snow showers", "snow"), 86: ("Snow showers", "snow"),
    95: ("Thunderstorm", "thunder"), 96: ("Thunder and hail", "thunder"),
    99: ("Thunder and hail", "thunder"),
}


def wmo(code):
    return WMO.get(code, ("Unknown", "cloudy"))


def compass(deg):
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((deg + 22.5) // 45) % 8]


def uv_level(uv):
    for limit, name in ((3, "Low"), (6, "Moderate"), (8, "High"), (11, "Very high")):
        if uv < limit:
            return name
    return "Extreme"


def aqi_level(v):
    for limit, name in ((20, "Good"), (40, "Fair"), (60, "Moderate"), (80, "Poor"), (100, "Very poor")):
        if v < limit:
            return name
    return "Extremely poor"


MOON_NAMES = ["New moon", "Waxing crescent", "First quarter", "Waxing gibbous",
              "Full moon", "Waning gibbous", "Last quarter", "Waning crescent"]


def moon_phase(now):
    """(fraction of cycle 0..1, illuminated 0..1, name). Accurate to about a day."""
    days = (now - datetime(2000, 1, 6, 18, 14)).total_seconds() / 86400.0
    frac = (days % 29.530588853) / 29.530588853
    illum = (1 - math.cos(2 * math.pi * frac)) / 2
    return frac, illum, MOON_NAMES[int(frac * 8 + 0.5) % 8]


def hm(iso):
    return iso[11:16]


def today_index(data, now):
    try:
        return data["daily"]["time"].index(now.strftime("%Y-%m-%d"))
    except (ValueError, KeyError):
        return 0


# ---------------------------------------------------------------- drawing helpers
_icons = {}


ANIM_FRAMES, ANIM_FPS = 24, 12            # weather icon animation: a 2-second loop
_anim = {}


def icon_frame(kind, size, day, frame):
    """One frame of the animated weather icon (drawn once, then reused)."""
    key = (THEME_NAME, kind, size, day, frame)
    if key not in _anim:
        if len(_anim) > ANIM_FRAMES * 3:  # keep only a few icons' frames in memory
            _anim.clear()
        _anim[key] = icon(kind, size, day, frame / ANIM_FRAMES)
    return _anim[key]


def icon(kind, size, day=True, phase=None):
    """Weather icon drawn 4x larger, then scaled down for smooth edges.
    phase 0..1 draws a moment of the animation loop; None draws the still icon."""
    key = (THEME_NAME, kind, size, day)
    if phase is None and key in _icons:
        return _icons[key]
    S = size * 4
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    c = S / 2
    p = phase or 0.0
    wave = math.sin(2 * math.pi * p) if phase is not None else 0.0

    def circ(x, y, r, fill):
        d.ellipse((x - r, y - r, x + r, y + r), fill=fill)

    def seg(x0, y0, x1, y1, w, fill):
        d.line((x0, y0, x1, y1), fill=fill, width=max(1, int(w)))
        circ(x0, y0, w / 2, fill)
        circ(x1, y1, w / 2, fill)

    def sun(x, y, r):
        turn = p * math.pi / 4  # rays turn slowly; 8 rays, so 1/8 turn loops seamlessly
        for i in range(8):
            a = i * math.pi / 4 + turn
            seg(x + math.cos(a) * r * 1.45, y + math.sin(a) * r * 1.45,
                x + math.cos(a) * r * 1.85, y + math.sin(a) * r * 1.85, r * 0.22, SUN)
        circ(x, y, r, SUN)

    def moon(x, y, r):
        circ(x, y, r, MOON)
        circ(x + r * 0.55, y - r * 0.35, r * 0.85, (0, 0, 0, 0))
        if phase is not None:  # twinkling stars
            for k, (sx, sy, sr) in enumerate(((1.25, -0.95, 0.09), (1.55, 0.15, 0.07), (-0.35, -1.35, 0.06))):
                glow = 0.25 + 0.75 * (0.5 + 0.5 * math.sin(2 * math.pi * (p + k / 3)))
                circ(x + r * sx, y + r * sy, r * sr * (0.6 + 0.4 * glow),
                     tuple(int(v * glow) for v in MOON) + (int(255 * glow),))

    def cloud(x, y, w, fill):
        yb = y + w * 0.25
        r1, r2, r3 = w * 0.2, w * 0.29, w * 0.22
        circ(x - w * 0.28, yb - r1, r1, fill)
        circ(x + w * 0.26, yb - r3, r3, fill)
        circ(x - w * 0.02, yb - r2 * 1.25, r2, fill)
        d.rectangle((x - w * 0.28, yb - w * 0.2, x + w * 0.26, yb), fill=fill)
        return yb

    def sky(x, y, r):
        sun(x, y, r) if day else moon(x, y, r * 1.3)

    drift = S * 0.03 * wave  # clouds sway gently left and right

    def falling(k, n):
        """0..1 position of falling drop/flake k of n at this moment (two falls per loop)."""
        return (p * 2 + k / n + 0.13 * k) % 1.0

    if kind == "clear":
        sky(c, c, S * 0.2)
    elif kind == "partly":
        sky(c - S * 0.13, c - S * 0.13, S * 0.13)
        cloud(c + S * 0.06 + drift, c + S * 0.12, S * 0.64, CLOUD)
    elif kind == "cloudy":
        cloud(c + S * 0.13 - drift * 0.7, c - S * 0.06, S * 0.5, CLOUD2)
        cloud(c - S * 0.05 + drift, c + S * 0.1, S * 0.66, CLOUD)
    elif kind == "fog":
        yb = cloud(c + drift * 0.5, c - S * 0.1, S * 0.62, CLOUD2)
        for i, (a, b) in enumerate(((-0.3, 0.22), (-0.22, 0.3))):
            y = yb + S * (0.1 + 0.11 * i)
            dx = S * 0.04 * (wave if i == 0 else -wave)
            seg(c + S * a + dx, y, c + S * b + dx, y, S * 0.05, CLOUD)
    else:
        precip = []  # drawn after the cloud so it sits on top
        if kind == "rain":
            if phase is None:
                precip = [("seg", c + S * dx, yb_ + S * 0.08, c + S * (dx - 0.06), yb_ + S * 0.24, S * 0.05)
                          for dx in (-0.18, 0.0, 0.18) for yb_ in [c - S * 0.1 + S * 0.68 * 0.25]]
            else:
                for k, dx in enumerate((-0.2, -0.07, 0.06, 0.19)):
                    f = falling(k, 4)
                    y0 = c + S * (0.07 + 0.04 + 0.26 * f)
                    x0 = c + S * dx - S * 0.375 * 0.26 * f
                    precip.append(("seg", x0, y0, x0 - S * 0.04, y0 + S * 0.11, S * 0.045))
        elif kind == "drizzle":
            if phase is None:
                precip = [("seg", c + S * dx, c + S * 0.16, c + S * (dx - 0.03), c + S * 0.23, S * 0.045)
                          for dx in (-0.16, 0.0, 0.16)]
            else:
                for k, dx in enumerate((-0.18, -0.06, 0.06, 0.18)):
                    f = falling(k, 4)
                    y0 = c + S * (0.12 + 0.28 * f)
                    precip.append(("seg", c + S * dx - S * 0.1 * f, y0, c + S * (dx - 0.02) - S * 0.1 * f,
                                   y0 + S * 0.06, S * 0.04))
        elif kind == "snow":
            if phase is None:
                precip = [("dot", c + S * dx, c + S * 0.07 + S * dy, S * 0.04)
                          for dx, dy in ((-0.18, 0.11), (0.0, 0.15), (0.18, 0.11), (-0.09, 0.27), (0.09, 0.27))]
            else:
                for k, dx in enumerate((-0.19, -0.09, 0.0, 0.1, 0.19)):
                    f = (p + k / 5 + 0.11 * k) % 1.0  # snow falls slower: one fall per loop
                    sway = S * 0.025 * math.sin(2 * math.pi * (f * 2 + k * 0.3))
                    precip.append(("dot", c + S * dx + sway, c + S * (0.1 + 0.33 * f), S * 0.038))
        flash = phase is not None and kind == "thunder" and (p < 0.06 or 0.12 <= p < 0.16)
        yb = cloud(c + drift * 0.5, c - S * 0.1, S * 0.68, (255, 246, 214) if flash else CLOUD)
        for item in precip:
            if item[0] == "seg":
                seg(*item[1:], RAIN)
            else:
                circ(item[1], item[2], item[3], SNOW)
        if kind == "thunder" and not (phase is not None and 0.55 <= p < 0.8):  # bolt flickers off
            x, y = c - S * 0.02, yb + S * 0.04
            d.polygon([(x + S * 0.02, y), (x - S * 0.08, y + S * 0.17), (x, y + S * 0.17),
                       (x - S * 0.05, y + S * 0.33), (x + S * 0.12, y + S * 0.11),
                       (x + S * 0.03, y + S * 0.11), (x + S * 0.09, y)],
                      fill=(255, 250, 200) if flash else BOLT)
    small = im.resize((size, size), Image.LANCZOS)
    if phase is None:
        _icons[key] = small
    return small


def power_icon(size, color):
    """The standard power symbol: a broken circle with a line through the gap."""
    key = ("power", size, color)
    if key in _icons:
        return _icons[key]
    S = size * 4
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    c, r, w = S / 2, S * 0.32, S * 0.09

    def dot(x, y):
        d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=color)

    ro = r + w / 2
    d.arc((c - ro, c - ro + S * 0.04, c + ro, c + ro + S * 0.04), start=-60, end=240, fill=color, width=int(w))
    for a in (-60, 240):  # round ends on the circle
        dot(c + r * math.cos(math.radians(a)), c + S * 0.04 + r * math.sin(math.radians(a)))
    top, bottom = c - r - S * 0.06, c + S * 0.02
    d.line((c, top, c, bottom), fill=color, width=int(w))
    dot(c, top)
    dot(c, bottom)
    _icons[key] = im.resize((size, size), Image.LANCZOS)
    return _icons[key]


def gear_icon(size, color):
    """Settings gear: a toothed wheel with a hole."""
    key = ("gear", size, color)
    if key in _icons:
        return _icons[key]
    S = size * 4
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    c, teeth = S / 2, 8
    pts = []
    for i in range(teeth * 4):
        a = 2 * math.pi * (i + 0.5) / (teeth * 4)
        r = S * 0.47 if i % 4 in (0, 1) else S * 0.35
        pts.append((c + r * math.cos(a), c + r * math.sin(a)))
    d.polygon(pts, fill=color)
    d.ellipse((c - S * 0.15, c - S * 0.15, c + S * 0.15, c + S * 0.15), fill=(0, 0, 0, 0))
    _icons[key] = im.resize((size, size), Image.LANCZOS)
    return _icons[key]


def moon_icon(frac, size):
    """The moon as it looks tonight: lit part in MOON, dark part in DIM."""
    key = (THEME_NAME, "phase", round(frac * 60), size)
    if key in _icons:
        return _icons[key]
    S = size * 4
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    c, r = S / 2, S * 0.46
    d.ellipse((c - r, c - r, c + r, c + r), fill=T.get("moon_dark", DIM))
    k = math.cos(2 * math.pi * frac)
    for yy in range(int(c - r), int(c + r) + 1):
        w2 = r * r - (yy - c) ** 2
        if w2 <= 0:
            continue
        w = math.sqrt(w2)
        x0, x1 = (c + k * w, c + w) if frac < 0.5 else (c - w, c - k * w)
        if x1 > x0:
            d.line((x0, yy, x1, yy), fill=MOON)
    _icons[key] = im.resize((size, size), Image.LANCZOS)
    return _icons[key]


_masks = {}


def rmask(w, h, r):
    key = (w, h, r)
    if key not in _masks:
        k, R = 4, r * 4
        m = Image.new("L", (w * k, h * k), 0)
        d = ImageDraw.Draw(m)
        d.rectangle((R, 0, w * k - R, h * k), fill=255)
        d.rectangle((0, R, w * k, h * k - R), fill=255)
        for cx, cy in ((R, R), (w * k - R, R), (R, h * k - R), (w * k - R, h * k - R)):
            d.ellipse((cx - R, cy - R, cx + R, cy + R), fill=255)
        _masks[key] = m.resize((w, h), Image.LANCZOS)
    return _masks[key]


def card(img, x, y, w, h, r=18, fill=None):
    img.paste(fill or CARD, (x, y, x + w, y + h), rmask(w, h, r))


def gradient(top, bot, w, h):
    col = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        col.putpixel((0, y), tuple(int(a + (b - a) * t) for a, b in zip(top, bot)))
    return col.resize((w, h))


_bgs = {}


def background(day=True):
    key = (THEME_NAME, day)
    if key not in _bgs:
        _bgs[key] = gradient(*(T["day"] if day else T["night"]), W, H)
    return _bgs[key].copy()


_clock_font = {}


def clock_metrics(maxw, biggest=210):
    key = (maxw, biggest)
    if key not in _clock_font:
        for size in range(biggest, 60, -4):
            f = F("heavy", size)
            dw = max(f.getlength(str(n)) for n in range(10))
            cw = f.getlength(":") * 1.1
            if 4 * dw + cw <= maxw:
                break
        _clock_font[key] = (f, dw, cw)
    return _clock_font[key]


def draw_clock(d, text, x, baseline, maxw, color=None, biggest=210):
    """HH:MM with fixed-width digit slots so the time does not wobble."""
    f, dw, cw = clock_metrics(maxw, biggest)
    for ch in text:
        w = cw if ch == ":" else dw
        d.text((x + w / 2, baseline - (8 if ch == ":" else 0)), ch, font=f, fill=color or TEXT, anchor="ms")
        x += w


# ---------------------------------------------------------------- dashboard
def render(now, data, updated, error):
    day = bool(data["current"].get("is_day", 1)) if data else True
    img = background(day)
    d = ImageDraw.Draw(img)

    # left: place, clock, date
    d.text((M, 62), SET["place"].upper(), font=fit(SET["place"].upper(), "bold", 22, 540), fill=MUTED, anchor="ls")
    draw_clock(d, now.strftime("%H:%M"), M - 6, 238, 548)
    d.text((M, 292), "%s, %d %s" % (now.strftime("%A"), now.day, now.strftime("%B")),
           font=F("reg", 34), fill=TEXT, anchor="ls")

    if not data:
        d.text((W - M, 170), "Waiting for weather...", font=F("bold", 30), fill=MUTED, anchor="rs")
        if error:
            d.text((W - M, 210), "No connection yet, retrying", font=F("reg", 20), fill=DIM, anchor="rs")
        return img

    cur, dl = data["current"], data["daily"]
    t = today_index(data, now)
    desc, kind = wmo(cur["weather_code"])

    # right: current conditions
    ic = icon(kind, 176, day)
    img.paste(ic, (592, 30), ic)
    d.text((W - M, 168), "%d°" % round(cur["temperature_2m"]), font=F("heavy", 112), fill=TEXT, anchor="rs")
    d.text((W - M, 232), desc, font=fit(desc, "bold", 30, 400), fill=TEXT, anchor="rs")
    d.text((W - M, 276), "Feels %d°     H %d°     L %d°" % (
        round(cur["apparent_temperature"]), round(dl["temperature_2m_max"][t]),
        round(dl["temperature_2m_min"][t])), font=F("reg", 26), fill=MUTED, anchor="rs")

    # detail tiles
    uv = dl["uv_index_max"][t] or 0
    rain_p = dl["precipitation_probability_max"][t] or 0
    air = data.get("air") or {}
    aqi = air.get("european_aqi")
    frac, illum, phase = moon_phase(now)
    tiles = [  # label, value, unit, sub, highlight, picture
        ("Humidity", "%d" % cur["relative_humidity_2m"], "%", "", False, None),
        ("Wind", "%d" % round(cur["wind_speed_10m"]), "km/h", "from " + compass(cur["wind_direction_10m"]), False, None),
        ("UV index", "%d" % round(uv), "", uv_level(uv), uv >= 6, None),
        ("Rain", "%d" % rain_p, "%", "%.1f mm now" % (cur.get("precipitation") or 0), False, None),
        ("Air quality", "%d" % aqi if aqi is not None else "--", "",
         aqi_level(aqi) if aqi is not None else "No data", aqi is not None and aqi >= 60, None),
        ("Sun", hm(dl["sunrise"][t]), "", "sets " + hm(dl["sunset"][t]), False, None),
        ("Moon", "%d" % round(illum * 100), "%", phase, False, moon_icon(frac, 30)),
    ]
    gap, ty, th = 12, 318, 100
    tw = (W - 2 * M - gap * (len(tiles) - 1)) // len(tiles)
    for i, (label, val, unit, sub, hot, pic) in enumerate(tiles):
        x = M + i * (tw + gap)
        card(img, x, ty, tw, th, fill=ALERT if hot else None)
        d.text((x + 16, ty + 28), label, font=fit(label, "reg", 17, tw - 24), fill=MUTED, anchor="ls")
        vx = x + 16
        if pic is not None:
            img.paste(pic, (vx, ty + 40), pic)
            vx += pic.size[0] + 8
        vf = F("bold", 32)
        d.text((vx, ty + 68), val, font=vf, fill=TEXT, anchor="ls")
        if unit:
            d.text((vx + 2 + vf.getlength(val), ty + 68), unit, font=F("reg", 17), fill=MUTED, anchor="ls")
        if sub:
            d.text((x + 16, ty + 89), sub, font=fit(sub, "reg", 15, tw - 24), fill=WARN if hot else MUTED, anchor="ls")

    # five-day forecast (the days after today)
    fy, fh, n = 430, 142, 5
    fw = (W - 2 * M - gap * (n - 1)) // n
    for j in range(n):
        i = t + 1 + j
        if i >= len(dl["time"]):
            break
        x = M + j * (fw + gap)
        cx = x + fw // 2
        card(img, x, fy, fw, fh)
        dt = datetime.strptime(dl["time"][i], "%Y-%m-%d")
        d.text((cx, fy + 32), "Tomorrow" if j == 0 else dt.strftime("%A"),
               font=F("bold", 21), fill=TEXT, anchor="ms")
        fic = icon(wmo(dl["weather_code"][i])[1], 62, True)
        img.paste(fic, (cx - 31, fy + 40), fic)
        p = dl["precipitation_probability_max"][i] or 0
        if p >= 10:
            d.text((cx + 36, fy + 80), "%d%%" % p, font=F("bold", 16), fill=RAIN, anchor="ls")
        d.text((cx - 6, fy + 128), "%d°" % round(dl["temperature_2m_max"][i]), font=F("bold", 26), fill=TEXT, anchor="rs")
        d.text((cx + 6, fy + 128), "%d°" % round(dl["temperature_2m_min"][i]), font=F("reg", 26), fill=MUTED, anchor="ls")

    note = "Updated %s" % updated.strftime("%H:%M")
    if error:
        note += "  (offline, retrying)"
    d.text((W - M, 593), note, font=F("reg", 14), fill=DIM, anchor="rs")
    return img


def render_bedside(now, data):
    """Dark, minimal night clock that gives off very little light."""
    img = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(img)
    col = tuple(int(c * 0.85) for c in BEDSIDE)
    soft = tuple(int(c * 0.42) for c in BEDSIDE)
    f, dw, cw = clock_metrics(820, 300)
    draw_clock(d, now.strftime("%H:%M"), (W - (4 * dw + cw)) / 2, 360, 820, color=col, biggest=300)
    d.text((W // 2, 440), "%s, %d %s" % (now.strftime("%A"), now.day, now.strftime("%B")),
           font=F("reg", 30), fill=soft, anchor="ms")
    if data:
        cur = data["current"]
        d.text((W // 2, 490), "%d°   %s" % (round(cur["temperature_2m"]), wmo(cur["weather_code"])[0]),
               font=F("reg", 26), fill=soft, anchor="ms")
    return img


TEMP_LINE = (255, 170, 60)


def is_day_now(data):
    return bool(data["current"].get("is_day", 1)) if data else True


def hour_index(data, when):
    try:
        return data["hourly"]["time"].index(when.strftime("%Y-%m-%dT%H:00"))
    except (KeyError, ValueError):
        return None


def draw_hours(img, d, data, start, count, top):
    """Hour-by-hour chart: times and icons, temperature line, rain chance bars, wind."""
    hr = data["hourly"]
    n = min(count, len(hr["time"]) - start)
    if n < 2:
        return
    x0, x1 = M + 8, W - M - 8
    cw = (x1 - x0) / n
    temps = [hr["temperature_2m"][start + i] for i in range(n)]
    valid = [t for t in temps if t is not None] or [0]
    lo, hi = min(valid), max(valid)
    if hi - lo < 4:
        lo, hi = (hi + lo) / 2 - 2, (hi + lo) / 2 + 2
    ty0, ty1 = top + 100, top + 216          # temperature line
    ry0, ry1 = top + 262, top + 348          # rain chance bars
    wy = top + 404                           # wind speeds
    d.text((x0, ry0 - 18), "Rain chance", font=F("reg", 16), fill=MUTED, anchor="ls")
    d.text((x0, wy - 28), "Wind km/h", font=F("reg", 16), fill=MUTED, anchor="ls")
    d.line((x0, ry1, x1, ry1), fill=DIM, width=1)
    points = []
    for i in range(n):
        k = start + i
        when = datetime.fromisoformat(hr["time"][k])
        x = x0 + cw * (i + 0.5)
        if when.hour == 0 and i > 0:  # midnight: mark the new day
            bx = int(x - cw / 2)
            d.line((bx, top + 62, bx, wy + 8), fill=DIM, width=1)
            d.text((bx + 6, top + 76), when.strftime("%a"), font=F("bold", 15), fill=MUTED, anchor="ls")
        p = hr["precipitation_probability"][k] or 0
        bar = (ry1 - ry0) * p / 100.0
        if p > 0:
            d.rectangle((x - cw * 0.3, ry1 - bar, x + cw * 0.3, ry1), fill=RAIN)
        if i % 3 == 0:
            d.text((x, top + 16), when.strftime("%H:%M"), font=F("bold", 17), fill=TEXT, anchor="ms")
            ic = icon(wmo(hr["weather_code"][k])[1], 40, bool(hr["is_day"][k]))
            img.paste(ic, (int(x) - 20, top + 22), ic)
            if p >= 10:
                d.text((x, ry1 - bar - 6), "%d%%" % p, font=F("bold", 15), fill=RAIN, anchor="ms")
            d.text((x, wy), "%d" % round(hr["wind_speed_10m"][k] or 0), font=F("bold", 18), fill=TEXT, anchor="ms")
        if temps[i] is not None:
            points.append((i, x, ty1 - (temps[i] - lo) / (hi - lo) * (ty1 - ty0), temps[i]))
    if len(points) > 1:
        d.line([(x, y) for _, x, y, _ in points], fill=TEMP_LINE, width=4, joint="curve")
    for i, x, y, t in points:
        if i % 3 == 0:
            d.ellipse((x - 5, y - 5, x + 5, y + 5), fill=TEMP_LINE)
            d.text((x, y - 12), "%d°" % round(t), font=F("bold", 18), fill=TEXT, anchor="ms")


# ---------------------------------------------------------------- hardware
class Framebuffer:
    def __init__(self, path=FB_DEV):
        import fcntl
        self.fd = os.open(path, os.O_RDWR)
        buf = bytearray(160)
        fcntl.ioctl(self.fd, 0x4600, buf, True)  # FBIOGET_VSCREENINFO
        v = struct.unpack_from("20I", buf)
        self.xres, self.yres, _, _, self.xoff, self.yoff, bpp = v[:7]
        if bpp != 32:
            raise SystemExit("unsupported framebuffer depth: %d bpp" % bpp)
        self.order = "BGRA" if v[8] == 16 else "RGBA"   # red offset
        try:
            with open("/sys/class/graphics/fb0/stride") as f:
                self.stride = int(f.read())
        except (OSError, ValueError):
            self.stride = self.xres * 4
        log("framebuffer %dx%d %s stride %d" % (self.xres, self.yres, self.order, self.stride))

    def show(self, img):
        if img.size != (self.xres, self.yres):
            img = img.resize((self.xres, self.yres))
        raw = img.convert("RGBA").tobytes("raw", self.order)
        row = self.xres * 4
        base = self.yoff * self.stride + self.xoff * 4
        if self.stride == row:
            os.pwrite(self.fd, raw, base)
        else:
            for y in range(self.yres):
                os.pwrite(self.fd, raw[y * row:(y + 1) * row], base + y * self.stride)

    def show_region(self, img, x, y):
        """Write a small picture at x,y without touching the rest of the screen."""
        w, h = img.size
        if x < 0 or y < 0 or x + w > self.xres or y + h > self.yres:
            return
        raw = img.convert("RGBA").tobytes("raw", self.order)
        row = w * 4
        for r in range(h):
            os.pwrite(self.fd, raw[r * row:(r + 1) * row],
                      (self.yoff + y + r) * self.stride + (self.xoff + x) * 4)


class Backlight:
    def __init__(self):
        self.level = None
        self.lit = True

    def _run(self, *args):
        try:
            r = subprocess.run([BRIGHTNESS_BIN] + list(args), capture_output=True, text=True, timeout=5)
            return r.returncode == 0 and "error" not in (r.stdout + r.stderr).lower()
        except (OSError, subprocess.SubprocessError) as e:
            log("brightness failed:", e)
            return False

    def on(self):
        self._run("-s", "1")

    def power(self, on):
        """Switch the backlight on or off (only runs the tool when the state changes)."""
        if on != self.lit:
            self._run("-s", "1" if on else "0")
            self.lit = on
            self.level = None  # set the brightness again after switching on

    def set(self, level):
        if level != self.level:
            if not self._run("-v", str(level)):
                log("could not set brightness to", level)
            self.level = level


# ---------------------------------------------------------------- weather thread
state = {"data": None, "updated": None, "error": None}
lock = threading.Lock()
events = queue.Queue()          # ("tap"|"long", x, y) from touch, ("refresh",) from weather
refresh_now = threading.Event()
last_touch = [0.0]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "sonicpad-weather-clock"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def weather_url():
    return ("https://api.open-meteo.com/v1/forecast?latitude=%.4f&longitude=%.4f"
            "&current=temperature_2m,relative_humidity_2m,apparent_temperature,is_day,"
            "precipitation,weather_code,wind_speed_10m,wind_direction_10m"
            "&hourly=temperature_2m,precipitation_probability,precipitation,weather_code,"
            "wind_speed_10m,wind_gusts_10m,is_day"
            "&daily=weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,"
            "uv_index_max,precipitation_probability_max,precipitation_sum,snowfall_sum,"
            "wind_gusts_10m_max"
            "&minutely_15=precipitation&forecast_minutely_15=8"
            "&forecast_days=7&timezone=%s"
            % (SET["lat"], SET["lon"], urllib.parse.quote(SET["tz"], safe="")))


def marine_url():
    return ("https://marine-api.open-meteo.com/v1/marine?latitude=%.4f&longitude=%.4f"
            "&current=wave_height,wave_direction,wave_period,sea_surface_temperature"
            "&hourly=wave_height,sea_level_height_msl&forecast_days=3&timezone=%s"
            % (SET["lat"], SET["lon"], urllib.parse.quote(SET["tz"], safe="")))


def air_url():
    return ("https://air-quality-api.open-meteo.com/v1/air-quality?latitude=%.4f&longitude=%.4f"
            "&current=european_aqi,pm2_5,pm10&timezone=%s"
            % (SET["lat"], SET["lon"], urllib.parse.quote(SET["tz"], safe="")))


def fetch_all():
    data = fetch(weather_url())
    try:
        data["air"] = fetch(air_url()).get("current")
    except Exception as e:  # air quality is optional
        log("air quality fetch failed:", e)
        data["air"] = None
    data["sea"] = None
    if SET["sea"]:
        try:
            data["sea"] = fetch(marine_url())
        except Exception as e:
            log("sea data fetch failed:", e)
    data["alerts"], data["alerts_source"] = None, ""  # None: use forecast-based alerts
    if SET["warnings"]:
        try:
            data["alerts"], data["alerts_source"] = official_alerts()
        except Exception as e:
            log("warnings fetch failed, using forecast-based alerts:", e)
    return data


# ---------------------------------------------------------------- weather warnings
# Official warnings where a free source exists, forecast-based alerts everywhere else.
METEOALARM = {
    "AT": "austria", "BA": "bosnia-herzegovina", "BE": "belgium", "BG": "bulgaria",
    "CH": "switzerland", "CY": "cyprus", "CZ": "czechia", "DE": "germany", "DK": "denmark",
    "EE": "estonia", "ES": "spain", "FI": "finland", "FR": "france", "GB": "united-kingdom",
    "GR": "greece", "HR": "croatia", "HU": "hungary", "IE": "ireland", "IL": "israel",
    "IS": "iceland", "IT": "italy", "LT": "lithuania", "LU": "luxembourg", "LV": "latvia",
    "MD": "moldova", "ME": "montenegro", "MK": "republic-of-north-macedonia", "MT": "malta",
    "NL": "netherlands", "NO": "norway", "PL": "poland", "PT": "portugal", "RO": "romania",
    "RS": "serbia", "SE": "sweden", "SI": "slovenia", "SK": "slovakia", "UA": "ukraine",
}
ALERT_COLORS = {2: (242, 200, 46), 3: (245, 136, 34), 4: (222, 52, 44)}  # yellow, orange, red
REGION_STOP = {"district", "distrito", "de", "do", "da", "dos", "das", "province", "provincia",
               "region", "regiao", "county", "municipality", "municipio", "concelho", "state",
               "department", "departement", "kreis", "landkreis", "city", "of", "the"}


def region_words(name):
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return [w for w in re.findall(r"[a-z0-9]+", s) if w not in REGION_STOP]


def area_matches(area):
    """Does a warning area ("Setúbal") match the chosen place's regions ("Setúbal District")?"""
    words = region_words(area)
    return bool(words) and any(region_words(r) == words for r in SET.get("regions") or [])


def to_local(iso):
    """ISO time with an offset -> naive local time at the chosen place."""
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        return t
    try:
        from zoneinfo import ZoneInfo
        return t.astimezone(ZoneInfo(SET["tz"])).replace(tzinfo=None)
    except Exception:
        return t.astimezone().replace(tzinfo=None)


def official_alerts():
    """(alerts, source name), or (None, "") where no official source is available."""
    cc = (SET.get("cc") or "").upper()
    found = []
    if cc in METEOALARM:
        source = "MeteoAlarm"
        feed = fetch("https://feeds.meteoalarm.org/api/v1/warnings/feeds-" + METEOALARM[cc])
        for w in feed.get("warnings") or []:
            infos = (w.get("alert") or {}).get("info") or []
            if not infos:
                continue
            info = next((i for i in infos if (i.get("language") or "").lower().startswith("en")), infos[0])
            if not any(area_matches(a.get("areaDesc")) for a in info.get("area") or []):
                continue
            level = 0
            for p in info.get("parameter") or []:
                if p.get("valueName") == "awareness_level":
                    try:
                        level = int(str(p.get("value")).split(";")[0])
                    except ValueError:
                        pass
            if level >= 2:  # 1 is green: no warning
                found.append({"level": min(level, 4), "title": info.get("event") or "Weather warning",
                              "detail": info.get("description") or info.get("headline") or "",
                              "start": to_local(info.get("onset") or info.get("effective")),
                              "end": to_local(info.get("expires"))})
    elif cc == "US":
        source = "US National Weather Service"
        feed = fetch("https://api.weather.gov/alerts/active?point=%.4f,%.4f" % (SET["lat"], SET["lon"]))
        levels = {"severe": 3, "extreme": 4}
        for f in feed.get("features") or []:
            p = f.get("properties") or {}
            found.append({"level": levels.get((p.get("severity") or "").lower(), 2),
                          "title": p.get("event") or "Weather alert",
                          "detail": p.get("headline") or p.get("description") or "",
                          "start": to_local(p.get("onset") or p.get("effective")),
                          "end": to_local(p.get("ends") or p.get("expires"))})
    else:
        return None, ""
    now = local_now()
    keep, seen = [], set()
    for a in sorted(found, key=lambda a: (-a["level"], a["start"] or now)):
        if a["end"] and a["end"] < now:
            continue
        if a["start"] and a["start"] > now + timedelta(days=2):
            continue
        if (a["title"], a["start"]) in seen:
            continue
        seen.add((a["title"], a["start"]))
        keep.append(a)
    return keep, source


def forecast_alerts(data, now):
    """Alerts worked out from the forecast, for places without an official source."""
    dl = data["daily"]
    t = today_index(data, now)
    out = []

    def level(v, steps):  # steps for yellow, orange, red
        return 1 + sum(1 for s in steps if v is not None and v >= s)

    def add(lv, title, detail):
        if lv >= 2 and not any(a["title"] == title for a in out):
            out.append({"level": lv, "title": title, "detail": detail, "start": None, "end": None})

    for i, when in ((t, "today"), (t + 1, "tomorrow")):
        if i >= len(dl["time"]):
            break
        get = lambda k: (dl.get(k) or [None] * (i + 1))[i]
        gust, rain, snow = get("wind_gusts_10m_max"), get("precipitation_sum"), get("snowfall_sum")
        hi, lo, code = get("temperature_2m_max"), get("temperature_2m_min"), get("weather_code")
        add(level(gust, (70, 90, 110)), "Strong wind", "Gusts up to %d km/h %s." % (round(gust or 0), when))
        add(level(rain, (30, 60, 100)), "Heavy rain", "About %d mm of rain %s." % (round(rain or 0), when))
        add(level(snow, (2, 10, 25)), "Snow", "About %d cm of snow %s." % (round(snow or 0), when))
        add(level(hi, (35, 38, 42)), "Heat", "Up to %d° %s." % (round(hi or 0), when))
        add(level(-lo if lo is not None else None, (5, 10, 20)), "Severe cold",
            "Down to %d° %s." % (round(lo or 0), when))
        if code in (95, 96, 99):
            add(3 if code in (96, 99) else 2, "Thunderstorms", "Thunderstorms expected %s." % when)
    return sorted(out, key=lambda a: -a["level"])


def active_alerts(data, now):
    if not data or not SET["warnings"]:
        return [], ""
    if data.get("alerts") is not None:
        return data["alerts"], data.get("alerts_source") or ""
    return forecast_alerts(data, now), "Forecast-based (no official warnings available here)"


def alert_when(a, now):
    def f(t):
        return t.strftime("%H:%M") if t.date() == now.date() else t.strftime("%a %H:%M")
    s, e = a.get("start"), a.get("end")
    if s and s > now:
        return "from %s" % f(s) + (" to %s" % f(e) if e else "")
    return "until %s" % f(e) if e else ""


def rain_soon(data, now):
    """Minutes until rain starts within the next 2 hours, or None."""
    if not data or not SET["rain_alerts"]:
        return None
    if (data["current"].get("precipitation") or 0) > 0:
        return None
    m = data.get("minutely_15") or {}
    for ts, p in zip(m.get("time") or [], m.get("precipitation") or []):
        t = datetime.fromisoformat(ts)
        if t > now and (p or 0) >= 0.1:
            return max(5, int(round((t - now).total_seconds() / 300.0)) * 5)
    return None


def tides(sea, now, hours=30):
    """[(High|Low, time, metres)] from the hourly sea level, interpolated between hours."""
    h = (sea or {}).get("hourly") or {}
    times, lv = h.get("time") or [], h.get("sea_level_height_msl") or []
    out = []
    for i in range(1, len(lv) - 1):
        a, b, c = lv[i - 1], lv[i], lv[i + 1]
        if None in (a, b, c):
            continue
        kind = "High" if b > a and b >= c else "Low" if b < a and b <= c else None
        if not kind:
            continue
        curve = a - 2 * b + c
        off = 0.5 * (a - c) / curve if curve else 0.0
        t = datetime.fromisoformat(times[i]) + timedelta(hours=off)
        if now - timedelta(minutes=30) <= t <= now + timedelta(hours=hours):
            out.append((kind, t, b))
    return out


def weather_loop():
    while True:
        try:
            data = fetch_all()
            with lock:
                state.update(data=data, updated=local_now(), error=None)
            delay = REFRESH_OK
            log("weather updated")
        except Exception as e:  # keep showing the last good data
            with lock:
                state["error"] = str(e)
                have_data = state["data"] is not None
            delay = REFRESH_FAIL if have_data else 10  # retry fast until the first update
            log("weather fetch failed:", e)
        events.put(("refresh", 0, 0))
        refresh_now.wait(delay)
        refresh_now.clear()


def geocode(query):
    # search English and Portuguese names, so both "Lisbon" and "Lisboa" work
    results, ids = [], set()
    for lang in ("en", "pt"):
        url = ("https://geocoding-api.open-meteo.com/v1/search?count=20&format=json&language=%s&name=%s"
               % (lang, urllib.parse.quote(query)))
        for r in fetch(url).get("results") or []:
            if r.get("id") not in ids:
                ids.add(r.get("id"))
                results.append(r)
    # places in Portugal first, then bigger places first
    results.sort(key=lambda r: (r.get("country_code") != "PT", -(r.get("population") or 0)))
    out, seen = [], set()
    for r in results:
        label = ", ".join(x for x in (r.get("name"), r.get("admin1"), r.get("country")) if x)
        if label in seen:
            continue
        seen.add(label)
        out.append({"name": r.get("name", "?"), "country": r.get("country", ""), "label": label,
                    "lat": r["latitude"], "lon": r["longitude"], "tz": r.get("timezone") or "auto",
                    "cc": r.get("country_code") or "",
                    "regions": [r[k] for k in ("admin1", "admin2", "admin3", "admin4") if r.get(k)]})
    return out[:5]


def set_place(p):
    SET.update(place="%s, %s" % (p["name"], p["country"]) if p.get("country") else p["name"],
               lat=p["lat"], lon=p["lon"], tz=p["tz"], cc=p.get("cc") or "",
               regions=p.get("regions") or [])
    save_settings()
    with lock:
        state.update(data=None, updated=None, error=None)
    refresh_now.set()
    log("location set to", SET["place"])


# ---------------------------------------------------------------- status and updates
def system_status():
    """[(label, value)] for the Status screen."""
    def safe(fn):
        try:
            return fn()
        except Exception:
            return "--"

    conn, ip = wifi_status()
    signal = ""
    _, out, _ = nm("-t", "-f", "IN-USE,SIGNAL", "device", "wifi")
    for line in out.splitlines():
        p = split_terse(line)
        if len(p) >= 2 and p[0] == "*":
            signal = " (%s%%)" % p[1]

    def read(path):
        with open(path) as f:
            return f.read()

    def uptime():
        s = int(float(read("/proc/uptime").split()[0]))
        d, h, m = s // 86400, s // 3600 % 24, s // 60 % 60
        return ("%d d %d h" % (d, h)) if d else ("%d h %d min" % (h, m))

    def storage():
        st = os.statvfs("/")
        return "%.1f GB free of %.1f GB" % (st.f_bavail * st.f_frsize / 1e9, st.f_blocks * st.f_frsize / 1e9)

    def memory():
        info = dict(l.split(":", 1) for l in read("/proc/meminfo").splitlines() if ":" in l)
        kb = lambda k: int(info[k].split()[0])
        return "%d MB free of %d MB" % (kb("MemAvailable") // 1024, kb("MemTotal") // 1024)

    def cpu_temp():
        return "%d°C" % round(int(read("/sys/class/thermal/thermal_zone0/temp")) / 1000)

    with lock:
        upd, err = state["updated"], state["error"]
    return [("Version", VERSION), ("IP address", ip or "None"),
            ("Wi-Fi", (conn + signal) if conn else "Not connected"), ("Uptime", safe(uptime)),
            ("Storage", safe(storage)), ("Memory", safe(memory)), ("CPU temperature", safe(cpu_temp)),
            ("Weather updated", (upd.strftime("%H:%M") + (" (offline)" if err else "")) if upd else "Not yet")]


def version_tuple(v):
    return tuple(int(n) for n in re.findall(r"\d+", v or "")[:3])


def latest_release():
    """Tag of the newest GitHub Release (raises HTTPError 404 if none is published)."""
    return fetch("https://api.github.com/repos/%s/releases/latest" % UPDATE_REPO)["tag_name"]


def write_atomic(path, data, mode):
    tmp = path + ".new"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def install_release(tag):
    """Download a release from GitHub and install its app and service files."""
    url = "https://codeload.github.com/%s/tar.gz/refs/tags/%s" % (UPDATE_REPO, urllib.parse.quote(tag))
    req = urllib.request.Request(url, headers={"User-Agent": "sonicpad-weather-clock"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read(50 * 1024 * 1024)
    files = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        for m in tar.getmembers():  # only read the two files we need, never extract the archive
            parts = m.name.split("/")
            if m.isfile() and len(parts) == 3 and parts[1] == "weatherclock" \
                    and parts[2] in ("weatherclock.py", "weatherclock.service"):
                files[parts[2]] = tar.extractfile(m).read()
    code = files.get("weatherclock.py")
    if not code:
        raise RuntimeError("weatherclock.py is missing from release %s" % tag)
    compile(code, "weatherclock.py", "exec")  # refuse a broken file
    here = os.path.abspath(__file__)
    candidate = os.path.join(os.path.dirname(here), "weatherclock-check.py")
    with open(candidate, "wb") as f:
        f.write(code)
    try:
        check_release(candidate)
    finally:
        os.remove(candidate)
    shutil.copy2(here, here + ".bak")
    write_atomic(here, code, 0o755)
    unit = "/etc/systemd/system/weatherclock.service"
    svc = files.get("weatherclock.service")
    if svc:
        try:
            with open(unit, "rb") as f:
                old = f.read()
        except OSError:
            old = None
        if svc != old:
            write_atomic(unit, svc, 0o644)
            subprocess.run(["systemctl", "daemon-reload"], timeout=30)
    write_atomic(UPDATE_MARK, json.dumps({"version": tag, "starts": 0}).encode(), 0o644)
    os.sync()
    log("installed release", tag)


def check_release(path):
    """Load a downloaded weatherclock.py in a separate Python and let it draw a few screens.

    Catches releases that compile but fail on start (a missing name, a Pillow feature
    the Pad lacks) before they replace the working version."""
    script = ("import importlib.util, sys\n"
              "spec = importlib.util.spec_from_file_location('candidate', sys.argv[1])\n"
              "mod = importlib.util.module_from_spec(spec)\n"
              "spec.loader.exec_module(mod)\n"
              "getattr(mod, 'self_test', lambda: None)()\n")
    try:
        r = subprocess.run([sys.executable, "-c", script, path], capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise RuntimeError("the new version did not finish its start-up check")
    if r.returncode:
        lines = r.stderr.strip().splitlines()
        raise RuntimeError("the new version fails to start: " + (lines[-1] if lines else "exit %d" % r.returncode))


def guard_update():
    """Count starts after an update, and go back to the previous version if the new one
    keeps crashing. Returns True while the update is still on trial."""
    try:
        with open(UPDATE_MARK) as f:
            mark = json.load(f)
    except (OSError, ValueError):
        return False
    mark["starts"] = mark.get("starts", 0) + 1
    if mark["starts"] <= UPDATE_TRIES:
        write_atomic(UPDATE_MARK, json.dumps(mark).encode(), 0o644)
        return True
    os.remove(UPDATE_MARK)
    here = os.path.abspath(__file__)
    try:
        with open(here + ".bak", "rb") as f:
            old = f.read()
    except OSError:
        log("update %s keeps failing and there is no backup to go back to" % mark.get("version"))
        return False
    log("update %s failed to start %d times: going back to the previous version"
        % (mark.get("version"), UPDATE_TRIES))
    write_atomic(here, old, 0o755)
    os.sync()
    os.execv(sys.executable, [sys.executable, "-u", here])


def confirm_update():
    """The updated clock has run long enough: keep it."""
    try:
        os.remove(UPDATE_MARK)
        log("update confirmed")
    except OSError:
        pass


# ---------------------------------------------------------------- phone settings page
WEB = {"server": None, "pin": "", "tokens": set(), "fails": {}}   # fails: {ip: (count, locked_until)}
WEB_LOCK = threading.Lock()
WEB_CSS = ("body{font-family:system-ui,sans-serif;background:#10141f;color:#eef1f7;margin:0 auto;"
           "padding:16px;max-width:560px}h1{font-size:22px}label{display:block;margin:14px 0 4px;"
           "color:#8c96aa}select,input[type=number],input[type=text],input[type=password]{width:100%;"
           "font-size:18px;padding:10px;border-radius:10px;border:1px solid #2e374e;background:#1c2334;"
           "color:#eef1f7;box-sizing:border-box}.row{display:flex;align-items:center;gap:10px;margin:12px 0}"
           ".row input{width:24px;height:24px}button{margin-top:16px;width:100%;font-size:18px;padding:12px;"
           "border:0;border-radius:10px;background:#2c6cdc;color:#fff}.msg{padding:10px;border-radius:10px;"
           "background:#1d4d2c}.err{background:#5c2422}a{color:#6aa5ff}")


def web_start():
    if WEB["server"]:
        return
    srv = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), WebHandler)
    srv.daemon_threads = True
    WEB.update(server=srv, pin="%04d" % secrets.randbelow(10000), tokens=set(), fails={})
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log("phone settings page on port", WEB_PORT)


def web_stop():
    srv = WEB["server"]
    WEB.update(server=None, tokens=set())
    if srv:
        srv.shutdown()
        srv.server_close()


def page_html(body):
    return ("<!doctype html><html><head><meta charset=utf-8><meta name=viewport "
            "content='width=device-width,initial-scale=1'><title>Sonic Clock</title>"
            "<style>%s</style></head><body>%s</body></html>" % (WEB_CSS, body))


def login_html(error):
    err = "<p class='msg err'>%s</p>" % html.escape(error) if error else ""
    return ("<h1>Sonic Clock</h1>%s<form method=post action=/login><label>PIN shown on the clock "
            "(Settings, Phone settings)</label><input type=password name=pin inputmode=numeric "
            "autocomplete=off autofocus><button>Open settings</button></form>" % err)


HALF_HOURS = [(str(m), "%02d:%02d" % divmod(m, 60)) for m in range(0, 24 * 60, 30)]


def select_html(name, options, current):
    opts = "".join("<option value='%s'%s>%s</option>" % (html.escape(str(v)), " selected" if v == current else "",
                                                         html.escape(label)) for v, label in options)
    return "<select name=%s>%s</select>" % (name, opts)


def settings_html(saved):
    e = html.escape
    themes = [(t, t) for t in THEME_ORDER]
    checks = "".join("<div class=row><input type=checkbox id=%s name=%s%s><label for=%s>%s</label></div>"
                     % (k, k, " checked" if SET.get(k) else "", k, label)
                     for k, label in (("sea", "Sea conditions page"), ("rain_alerts", "Rain alerts"),
                                      ("warnings", "Weather warnings"), ("pixel_shift", "Burn-in protection"),
                                      ("animations", "Animations")))
    return ("<h1>Sonic Clock settings</h1>%s"
            "<p>Location: <b>%s</b></p><form method=get action=/search><label>Change location</label>"
            "<input type=text name=q placeholder='Town or city'><button>Search</button></form>"
            "<form method=post action=/save>"
            "<label>Theme</label>%s<label>Night theme</label>%s"
            "<label>Day brightness (10-100)</label><input type=number name=day_bright min=10 max=100 step=5 value=%d>"
            "<label>Night brightness (5-100)</label><input type=number name=night_bright min=5 max=100 step=5 value=%d>"
            "<label>Night mode</label>%s<label>Night hours</label>%s<label>Screen rotation</label>%s"
            "%s<div class=row><input type=checkbox id=screen_off name=screen_off%s>"
            "<label for=screen_off>Screen off schedule (dark %d s after the last touch)</label></div>"
            "<label>Turn off at</label>%s<label>Turn on at</label>%s"
            "<button>Save</button></form>"
            % ("<p class=msg>Saved.</p>" if saved else "", e(SET["place"]),
               select_html("theme", themes, SET["theme"]),
               select_html("night_theme", [("", "Off")] + themes, SET.get("night_theme") or ""),
               SET["day_bright"], SET["night_bright"],
               select_html("night_mode", NIGHT_MODES, SET["night_mode"]),
               select_html("night_hours", NIGHT_HOURS, SET["night_hours"]),
               select_html("rotate", [(str(v), l) for v, l in ROTATIONS], str(SET["rotate"])), checks,
               " checked" if SET.get("screen_off") else "", SCREEN_OFF_SECS,
               select_html("off_from", HALF_HOURS, str(SET["off_from"])),
               select_html("off_until", HALF_HOURS, str(SET["off_until"]))))


def search_html(query):
    e = html.escape
    try:
        places = geocode(query) if len(query.strip()) >= 2 else []
    except Exception:
        return "<p class='msg err'>Search failed. Check the clock's internet connection.</p><a href=/>Back</a>"
    if not places:
        return "<p class='msg err'>No places found for %s.</p><a href=/>Back</a>" % e(query)
    rows = []
    for p in places:
        hidden = "".join("<input type=hidden name=%s value='%s'>" % (k, e(str(p[k])))
                         for k in ("name", "country", "lat", "lon", "tz", "cc"))
        hidden += "<input type=hidden name=regions value='%s'>" % e("|".join(p["regions"]))
        rows.append("<form method=post action=/place>%s<button>%s</button></form>" % (hidden, e(p["label"])))
    return "<h1>Choose a place</h1>%s<p><a href=/>Back</a></p>" % "".join(rows)


def apply_web_settings(f):
    if f.get("theme") in THEMES:
        SET["theme"] = f["theme"]
    SET["night_theme"] = f.get("night_theme") if f.get("night_theme") in THEMES else ""
    for key, lo in (("day_bright", 10), ("night_bright", 5)):
        try:
            SET[key] = max(lo, min(100, int(f.get(key, SET[key]))))
        except ValueError:
            pass
    for key, opts in (("night_mode", NIGHT_MODES), ("night_hours", NIGHT_HOURS)):
        if f.get(key) in [v for v, _ in opts]:
            SET[key] = f[key]
    if f.get("rotate") in ("0", "180"):
        SET["rotate"] = int(f["rotate"])
    for key in ("sea", "rain_alerts", "warnings", "pixel_shift", "animations", "screen_off"):
        SET[key] = f.get(key) == "on"
    for key in ("off_from", "off_until"):
        if f.get(key) in [v for v, _ in HALF_HOURS]:
            SET[key] = int(f[key])
    save_settings()
    refresh_now.set()


def apply_web_place(f):
    try:
        lat, lon = float(f["lat"]), float(f["lon"])
    except (KeyError, ValueError):
        return
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return
    tz = f.get("tz") or "auto"
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)
    except Exception:
        tz = "auto"
    cc = f.get("cc", "")
    set_place({"name": f.get("name", "?")[:60], "country": f.get("country", "")[:60], "lat": lat, "lon": lon,
               "tz": tz, "cc": cc.upper() if re.fullmatch(r"[A-Za-z]{2}", cc) else "",
               "regions": [r[:80] for r in f.get("regions", "").split("|") if r][:4]})


class WebHandler(BaseHTTPRequestHandler):
    server_version = "SonicClock"
    timeout = 15  # drop connections that stall instead of holding a thread forever

    def log_message(self, fmt, *args):
        pass

    def from_home(self):
        """Only answer devices on the local network (private, loopback or link-local addresses)."""
        try:
            ip = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            return True
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def authed(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "sc" and v in WEB["tokens"]:
                return True
        return False

    def form(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if not 0 < n <= 20000:
            return {}
        body = self.rfile.read(n).decode("utf-8", "replace")
        return {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}

    def send_page(self, body):
        data = page_html(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, where, cookie=None):
        self.send_response(303)
        self.send_header("Location", where)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self.from_home():
            return
        url = urllib.parse.urlparse(self.path)
        if not self.authed():
            return self.send_page(login_html(""))
        q = urllib.parse.parse_qs(url.query)
        if url.path == "/search":
            return self.send_page(search_html(q.get("q", [""])[0]))
        self.send_page(settings_html("saved" in q))

    def do_POST(self):
        if not self.from_home():
            return
        url = urllib.parse.urlparse(self.path)
        f = self.form()
        if url.path == "/login":
            now = time.monotonic()
            who = self.client_address[0]
            token = None
            with WEB_LOCK:  # several phones can log in at once; each device gets its own lockout
                count, locked_until = WEB["fails"].get(who, (0, 0.0))
                locked = now < locked_until
                if not locked and hmac.compare_digest(f.get("pin", "").encode(), WEB["pin"].encode()):
                    token = secrets.token_urlsafe(24)
                    WEB["tokens"].add(token)
                    WEB["fails"].pop(who, None)
                elif not locked:
                    count += 1
                    WEB["fails"][who] = (0, now + 60) if count >= 5 else (count, 0.0)
            if locked:
                return self.send_page(login_html("Too many wrong PINs. Wait a minute and try again."))
            if token:
                return self.redirect("/", "sc=%s; Path=/; HttpOnly; SameSite=Strict" % token)
            return self.send_page(login_html("Wrong PIN."))
        if not self.authed():
            return self.redirect("/")
        if url.path == "/save":
            apply_web_settings(f)
        elif url.path == "/place":
            apply_web_place(f)
        events.put(("web", 0, 0))
        self.redirect("/?saved=1")


def is_night(now, data):
    if not data:
        return False
    t = today_index(data, now)
    sunrise = datetime.fromisoformat(data["daily"]["sunrise"][t])
    sunset = datetime.fromisoformat(data["daily"]["sunset"][t])
    return not (sunrise <= now <= sunset)


def in_night_window(now, data):
    hours = SET["night_hours"]
    if hours == "sun":
        return is_night(now, data) if data else (now.hour >= 21 or now.hour < 7)
    return now.hour >= int(hours) or now.hour < 7


def in_screen_off_window(now):
    """Inside the screen-off hours? The window may cross midnight (e.g. 23:00 to 07:00)."""
    if not SET.get("screen_off"):
        return False
    start, end, t = SET["off_from"], SET["off_until"], now.hour * 60 + now.minute
    if start == end:
        return False
    return start <= t < end if start < end else (t >= start or t < end)


def hhmm(minutes):
    return "%02d:%02d" % divmod(minutes % (24 * 60), 60)


# ---------------------------------------------------------------- touch thread
EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
BTN_TOUCH = 330
ABS_X, ABS_Y, ABS_MT_X, ABS_MT_Y, ABS_MT_ID = 0, 1, 53, 54, 57


def abs_range(fd, code):
    import fcntl
    buf = bytearray(24)
    try:
        fcntl.ioctl(fd, 0x80184540 + code, buf, True)  # EVIOCGABS(code)
        _, lo, hi = struct.unpack_from("3i", buf)
        return (lo, hi) if hi > lo else None
    except OSError:
        return None


def to_screen(x, y, rx, ry):
    px = (x - rx[0]) * (W - 1) / (rx[1] - rx[0])
    py = (y - ry[0]) * (H - 1) / (ry[1] - ry[0])
    if TOUCH_SWAP_XY:
        px, py = py * (W - 1) / (H - 1), px * (H - 1) / (W - 1)
    if TOUCH_INVERT_X:
        px = W - 1 - px
    if TOUCH_INVERT_Y:
        py = H - 1 - py
    if SET["rotate"] == 180:
        px, py = W - 1 - px, H - 1 - py
    return int(min(max(px, 0), W - 1)), int(min(max(py, 0), H - 1))


def touch_loop():
    fmt = "llHHi"  # struct input_event in this Python's native word size
    size = struct.calcsize(fmt)
    while True:
        try:
            fd = os.open(TOUCH_DEV, os.O_RDONLY)
        except OSError as e:
            log("touch unavailable:", e)
            time.sleep(10)
            continue
        rx = abs_range(fd, ABS_MT_X) or abs_range(fd, ABS_X) or (0, W - 1)
        ry = abs_range(fd, ABS_MT_Y) or abs_range(fd, ABS_Y) or (0, H - 1)
        log("touch ranges x=%s y=%s" % (rx, ry))
        x = y = sx = sy = 0
        down = was = False
        t0 = 0.0
        try:
            while True:
                ev = os.read(fd, size)
                if len(ev) < size:
                    break
                _, _, etype, code, value = struct.unpack(fmt, ev)
                if etype == EV_ABS:
                    if code in (ABS_X, ABS_MT_X):
                        x = value
                    elif code in (ABS_Y, ABS_MT_Y):
                        y = value
                    elif code == ABS_MT_ID:
                        down = value >= 0
                elif etype == EV_KEY and code == BTN_TOUCH:
                    down = value == 1
                elif etype == EV_SYN and code == 0:
                    if down and not was:
                        t0, sx, sy = time.monotonic(), x, y
                        last_touch[0] = t0
                    elif was and not down:
                        px, py = to_screen(sx, sy, rx, ry)
                        ex, ey = to_screen(x, y, rx, ry)
                        held = time.monotonic() - t0
                        dx, dy = ex - px, ey - py
                        if abs(dx) >= SWIPE_MIN and abs(dx) > 2 * abs(dy) and held < 1.5:
                            events.put(("swipe_l" if dx < 0 else "swipe_r", px, py))
                        else:
                            events.put(("long" if held >= LONG_PRESS else "tap", px, py))
                    was = down
        except OSError as e:
            log("touch read error:", e)
            time.sleep(2)
        finally:
            os.close(fd)


# ---------------------------------------------------------------- Wi-Fi (NetworkManager)
def nm(*args, timeout=30):
    try:
        r = subprocess.run(["nmcli"] + list(args), capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)


def split_terse(line):
    """Split nmcli -t output on unescaped colons."""
    parts, cur, esc = [], "", False
    for ch in line:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


def saved_wifi():
    """{ssid: connection profile name} for saved Wi-Fi networks."""
    _, out, _ = nm("-t", "-f", "NAME,TYPE", "connection", "show")
    res = {}
    for line in out.splitlines():
        p = split_terse(line)
        if len(p) >= 2 and p[1] == "802-11-wireless":
            _, ssid, _ = nm("-g", "802-11-wireless.ssid", "connection", "show", "id", p[0])
            res[split_terse(ssid)[0] or p[0]] = p[0]
    return res


def wifi_status():
    _, out, _ = nm("-t", "-f", "GENERAL.CONNECTION,IP4.ADDRESS", "device", "show", WIFI_IF)
    conn = ip = None
    for line in out.splitlines():
        k, _, v = line.partition(":")
        if k == "GENERAL.CONNECTION" and v and v != "--":
            conn = v
        elif k.startswith("IP4.ADDRESS") and v and not ip:
            ip = v.split("/")[0]
    return conn, ip


def wifi_scan():
    _, out, _ = nm("-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                   "--rescan", "yes", timeout=45)
    saved = saved_wifi()
    nets = {}
    for line in out.splitlines():
        p = split_terse(line)
        if len(p) < 4 or not p[1]:
            continue
        ssid, active = p[1], p[0] == "*"
        try:
            sig = int(p[2])
        except ValueError:
            sig = 0
        old = nets.get(ssid)
        nets[ssid] = {
            "ssid": ssid,
            "signal": max(sig, old["signal"] if old else 0),
            "secure": p[3] not in ("", "--"),
            "active": active or bool(old and old["active"]),
            "saved": ssid in saved,
        }
    order = sorted(nets.values(), key=lambda n: (not n["active"], not n["saved"], -n["signal"]))
    return order, saved, wifi_status()


# ---------------------------------------------------------------- screens
LAYERS = {
    "lower": ["qwertyuiop", "asdfghjkl", "zxcvbnm"],
    "upper": ["QWERTYUIOP", "ASDFGHJKL", "ZXCVBNM"],
    "sym1": ["1234567890", "@#$%&*-+()", "!\"':;/?"],
    "sym2": ["[]{}<>^~|\\", "_=`.,", ""],
}


def wrap(text, font, maxw):
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if font.getlength(trial) <= maxw:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


class App:
    def __init__(self, fb, bl):
        self.fb, self.bl = fb, bl
        self.mode = "clock"
        self.buttons = []
        self.shown = None
        self.dirty = True
        self.dimmed = False
        self.last_input = time.monotonic()
        self.bright_preview = None
        self.nets, self.saved, self.status, self.page = [], {}, "", 0
        self.wifi_name = ""
        self.sel = None
        self.places = []
        self.theme_page = 0
        self.kb = {}
        self.text, self.layer, self.show_pw, self.note = "", "lower", True, ""
        self.msg = ("", "", [])
        self.view = "dash"            # swipe pages: dash, hourly, sea, timers
        self.settings_page = 0
        self.theme_target = "theme"   # or "night_theme"
        self.day_index = 0
        self.timer_end = None         # monotonic time the timer finishes
        self.timer_wall = None        # clock time it finishes, for display
        self.ring_until = None
        self.status_info, self.update_tag, self.update_note = [], None, ""
        self.alert_list, self.alert_source = [], ""
        self.last_frame, self.last_shift = None, (0, 0)
        self.anim_frame = None
        self.slid = False             # a slide already drew the new screen
        self.trial_until = None       # after an update: keep it once it has run until then

    # ---- drawing helpers
    def button(self, img, d, box, label, action, primary=False, size=24, fill=None):
        x0, y0, x1, y1 = box
        card(img, x0, y0, x1 - x0, y1 - y0, r=14, fill=fill or (ACCENT if primary else KEY))
        d.text(((x0 + x1) // 2, (y0 + y1) // 2), label, font=font_for(label, "bold", size),
               fill=ON_ACCENT if primary else TEXT, anchor="mm")
        self.buttons.append((box, action))

    def header(self, img, d, title, back):
        self.buttons = []
        self.button(img, d, (M, 20, M + 170, 80), "Back", back)
        d.text((W // 2, 62), title, font=F("bold", 36), fill=TEXT, anchor="ms")

    def show(self, img, shift=False):
        self.last_frame = img
        dx = dy = 0
        if shift and SET.get("pixel_shift"):  # nudge the picture every 10 minutes against burn-in
            n = local_now()
            dx, dy = SHIFTS[(n.hour * 6 + n.minute // 10) % len(SHIFTS)]
            if dx or dy:
                moved = img.copy()  # uncovered edges keep the background underneath
                moved.paste(img, (dx, dy))
                img = moved
        self.last_shift = (dx, dy)
        if SET["rotate"] == 180:
            img = img.rotate(180)
        self.fb.show(img)

    def show_region(self, img, x, y):
        """Redraw one small area of the last full picture (same shift and rotation)."""
        dx, dy = self.last_shift
        x, y = x + dx, y + dy
        if SET["rotate"] == 180:
            img = img.rotate(180)
            x, y = W - x - img.size[0], H - y - img.size[1]
        self.fb.show_region(img, x, y)

    def slide_to(self, new, direction, shift=False):
        """Slide a new screen in: direction 1 = from the right, -1 = from the left."""
        old = self.last_frame
        if SET.get("animations") and old is not None and old.size == new.size:
            steps = 6
            for i in range(1, steps):
                off = int(W * (1 - (1 - i / steps) ** 3))  # ease out
                frame = Image.new("RGB", (W, H))
                if direction > 0:
                    frame.paste(old.crop((off, 0, W, H)), (0, 0))
                    frame.paste(new.crop((0, 0, off, H)), (W - off, 0))
                else:
                    frame.paste(old.crop((0, 0, W - off, H)), (off, 0))
                    frame.paste(new.crop((W - off, 0, W, H)), (0, 0))
                self.show(frame)
        self.show(new, shift=shift)

    def clock_key(self, now, updated, error, view, mono):
        """Everything that changes the clock screens; a new key means redraw."""
        tick = int(self.timer_end - mono) if view == "timers" and self.timer_end else 0
        return (now.strftime("%Y%m%d%H%M"), updated, bool(error), view, THEME_NAME,
                SET["place"], SET["rotate"], SET["sea"], tick, self.timer_end is not None)

    def slide_view(self, direction):
        """Show the current swipe page by sliding it in."""
        now, mono = local_now(), time.monotonic()
        with lock:
            data, updated, error = state["data"], state["updated"], state["error"]
        self.slide_to(self.render_view(now, data, updated, error), direction, shift=True)
        self.shown = self.clock_key(now, updated, error, self.view, mono)
        self.anim_frame = None

    def animate_icon(self, data):
        """Next frame of the dashboard's weather icon, drawn into just that area."""
        frame = int(time.monotonic() * ANIM_FPS) % ANIM_FRAMES
        if frame == self.anim_frame:
            return
        self.anim_frame = frame
        day = is_day_now(data)
        ic = icon_frame(wmo(data["current"]["weather_code"])[1], 176, day, frame)
        area = background(day).crop((592, 30, 768, 206))
        area.paste(ic, (0, 0), ic)
        self.show_region(area, 592, 30)

    def views(self):
        return ["dash", "hourly"] + (["sea"] if SET["sea"] else []) + ["timers"]

    def set_view(self, view):
        self.view = view
        self.shown = None

    def page_dots(self, img, d):
        """Page dots, with a tappable Settings gear to their left (Settings is 'left' of the dashboard)."""
        vs = self.views()
        x = W // 2 - (len(vs) - 1) * 11
        gear = gear_icon(22, MUTED)
        gx = x - 38
        img.paste(gear, (gx - 11, 588 - 11), gear)
        self.buttons.append(((gx - 34, 556, gx + 24, H), lambda: self.open_settings(slide=True)))
        for v in vs:
            r = 5 if v == self.view else 4
            d.ellipse((x - r, 588 - r, x + r, 588 + r), fill=TEXT if v == self.view else DIM)
            x += 22

    def render_view(self, now, data, updated, error):
        if self.view == "hourly":
            img = self.render_hourly(now, data)
        elif self.view == "sea":
            img = self.render_sea(now, data)
        elif self.view == "timers":
            img = self.render_timers()
        else:
            img = self.render_clock(now, data, updated, error)
        self.page_dots(img, ImageDraw.Draw(img))
        return img

    def redraw(self):
        if self.mode == "clock":
            self.shown = None
            return
        self.show(getattr(self, "render_" + self.mode)())
        self.dirty = False

    def go(self, mode):
        self.mode = mode
        if mode == "clock":
            self.shown = None

    def go_clock(self):
        self.go("clock")

    # ---- clock
    def render_clock(self, now, data, updated, error):
        img = render(now, data, updated, error)
        d = ImageDraw.Draw(img)
        self.buttons = []
        if error and not data:
            self.button(img, d, (W - M - 300, 250, W - M, 316), "Wi-Fi settings", self.open_wifi)
        elif error:
            d.text((M, 593), "Offline: tap here for Wi-Fi settings", font=F("bold", 16), fill=RAIN, anchor="ls")
            self.buttons.append(((0, 572, 440, H), self.open_wifi))
        if data:
            t = today_index(data, now)
            gap, fy, fh, n = 12, 430, 142, 5
            fw = (W - 2 * M - gap * (n - 1)) // n
            for j in range(n):  # tap a forecast day for its hours
                if t + 1 + j < len(data["daily"]["time"]):
                    x = M + j * (fw + gap)
                    self.buttons.append(((x, fy, x + fw, fy + fh), lambda i=t + 1 + j: self.open_day(i)))
            self.buttons.append(((M, 318, W - M, 418), lambda: self.set_view("hourly")))
        self.draw_pills(img, d, now, data, footer_free=not error)
        return img

    def draw_pills(self, img, d, now, data, footer_free=True):
        """Warning, rain and timer notices next to the place name. Each is tappable."""
        label = SET["place"].upper()
        x = M + fit(label, "bold", 22, 540).getlength(label) + 18
        alerts, source = active_alerts(data, now)
        self.alert_list, self.alert_source = alerts, source
        pills = []
        if alerts:
            a = alerts[0]
            more = "  +%d" % (len(alerts) - 1) if len(alerts) > 1 else ""
            pills.append((a["title"] + more, ALERT_COLORS[a["level"]],
                          (24, 20, 10) if a["level"] == 2 else (255, 255, 255), lambda: self.go("alerts")))
        if self.timer_end is not None:
            pills.append(("Timer ends %s" % self.timer_wall.strftime("%H:%M"), ACCENT, ON_ACCENT,
                          lambda: self.set_view("timers")))
        mins = rain_soon(data, now)
        if mins:
            pills.append(("Rain in ~%d min" % mins, RAIN, (10, 22, 44), lambda: self.set_view("hourly")))
        foot_x, overflow = M, False
        for text, bg, fg, action in pills:
            room = 584 - x - 28
            f = fit(text, "bold", 17, room, smallest=13)
            if not overflow and room >= 60 and f.getlength(text) <= room:
                w = int(f.getlength(text)) + 28
                card(img, int(x), 38, w, 30, r=15, fill=bg)
                d.text((x + w / 2, 53), text, font=f, fill=fg, anchor="mm")
                self.buttons.insert(0, ((int(x) - 6, 16, int(x) + w + 6, 92), action))
                x += w + 10
                continue
            overflow = True  # no room at the top: use the bottom-left corner
            f = F("bold", 14)
            w = int(f.getlength(text)) + 22
            if not footer_free or foot_x + w > 440:
                continue
            card(img, int(foot_x), 576, w, 22, r=11, fill=bg)
            d.text((foot_x + w / 2, 587), text, font=f, fill=fg, anchor="mm")
            self.buttons.insert(0, ((int(foot_x) - 6, 572, int(foot_x) + w + 6, H), action))
            foot_x += w + 8

    # ---- hourly, day detail, sea, timers, warnings
    def render_hourly(self, now, data):
        img = background(is_day_now(data))
        d = ImageDraw.Draw(img)
        self.buttons = []
        d.text((M, 62), "Next 24 hours", font=F("bold", 30), fill=TEXT, anchor="ls")
        d.text((W - M, 62), SET["place"], font=fit(SET["place"], "reg", 20, 420), fill=MUTED, anchor="rs")
        if not data or "hourly" not in data:
            d.text((W // 2, 300), "Waiting for weather...", font=F("bold", 30), fill=MUTED, anchor="mm")
        else:
            draw_hours(img, d, data, hour_index(data, now) or 0, 24, 100)
        return img

    def open_day(self, i):
        self.day_index = i
        self.go("day")

    def render_day(self):
        img = background(True)
        d = ImageDraw.Draw(img)
        with lock:
            data = state["data"]
        if not data:
            self.header(img, d, "Forecast", self.go_clock)
            return img
        dl = data["daily"]
        i = self.day_index = max(0, min(len(dl["time"]) - 1, self.day_index))
        date = datetime.strptime(dl["time"][i], "%Y-%m-%d")
        self.header(img, d, "%s, %d %s" % (date.strftime("%A"), date.day, date.strftime("%B")), self.go_clock)
        g = lambda k: (dl.get(k) or [None] * (i + 1))[i] or 0
        summary = "H %d°   L %d°   Rain %d%% (%.0f mm)   Gusts %d km/h   UV %d   Sun %s - %s" % (
            round(g("temperature_2m_max")), round(g("temperature_2m_min")), g("precipitation_probability_max"),
            g("precipitation_sum"), round(g("wind_gusts_10m_max")), round(g("uv_index_max")),
            hm(dl["sunrise"][i]), hm(dl["sunset"][i]))
        d.text((W // 2, 116), summary, font=fit(summary, "reg", 21, W - 2 * M), fill=MUTED, anchor="ms")
        start = hour_index(data, date)
        if start is not None:
            draw_hours(img, d, data, start, 24, 128)
        d.text((W // 2, 588), "Swipe for other days", font=F("reg", 15), fill=DIM, anchor="ms")
        return img

    def render_sea(self, now, data):
        img = background(is_day_now(data))
        d = ImageDraw.Draw(img)
        self.buttons = []
        d.text((M, 62), "Sea", font=F("bold", 30), fill=TEXT, anchor="ls")
        d.text((W - M, 62), SET["place"], font=fit(SET["place"], "reg", 20, 420), fill=MUTED, anchor="rs")
        sea = (data or {}).get("sea")
        cur = (sea or {}).get("current") or {}
        if not data or sea is None:
            d.text((W // 2, 300), "Getting sea data...", font=F("bold", 30), fill=MUTED, anchor="mm")
            return img
        if cur.get("wave_height") is None:
            d.text((W // 2, 280), "No sea data for this place", font=F("bold", 30), fill=MUTED, anchor="mm")
            d.text((W // 2, 324), "It may be too far from the coast.", font=F("reg", 22), fill=DIM, anchor="mm")
            return img
        sst = cur.get("sea_surface_temperature")
        stats = [("Waves", "%.1f" % cur["wave_height"], "m"),
                 ("Wave period", "%d" % round(cur.get("wave_period") or 0), "s"),
                 ("Waves from", compass(cur.get("wave_direction") or 0), ""),
                 ("Sea temperature", "%d°" % round(sst) if sst is not None else "--", "")]
        g, cw = 12, (W - 2 * M - 36) // 4
        for i, (label, val, unit) in enumerate(stats):
            x = M + i * (cw + g)
            card(img, x, 92, cw, 104)
            d.text((x + 20, 124), label, font=F("reg", 18), fill=MUTED, anchor="ls")
            vf = F("bold", 44)
            d.text((x + 20, 178), val, font=vf, fill=TEXT, anchor="ls")
            if unit:
                d.text((x + 24 + vf.getlength(val), 178), unit, font=F("reg", 22), fill=MUTED, anchor="ls")
        marks = tides(sea, now)[:4]
        d.text((M, 236), "Tides" if marks else "No tide data here", font=F("bold", 22), fill=TEXT, anchor="ls")
        for i, (kind, t, level) in enumerate(marks):
            x = M + i * (cw + g)
            card(img, x, 250, cw, 76)
            day = "" if t.date() == now.date() else t.strftime("%a ")
            d.text((x + 20, 284), "%s  %s%s" % (kind, day, t.strftime("%H:%M")), font=F("bold", 22),
                   fill=TEXT, anchor="ls")
            d.text((x + 20, 312), "%.1f m" % level, font=F("reg", 18), fill=MUTED, anchor="ls")
        # tide curve for the next 24 hours
        h = sea.get("hourly") or {}
        lv, times = h.get("sea_level_height_msl") or [], h.get("time") or []
        try:
            s = times.index(now.strftime("%Y-%m-%dT%H:00"))
        except ValueError:
            s = None
        if s is not None:
            pts = [(i, lv[s + i]) for i in range(min(25, len(lv) - s)) if lv[s + i] is not None]
            if len(pts) > 1:
                x0, x1, y0, y1 = M + 8, W - M - 8, 360, 520
                vals = [v for _, v in pts]
                lo, hi = min(vals), max(vals)
                hi = hi if hi - lo > 0.2 else lo + 0.2
                xy = [(x0 + (x1 - x0) * i / 24, y1 - (v - lo) / (hi - lo) * (y1 - y0)) for i, v in pts]
                d.line(xy, fill=RAIN, width=4, joint="curve")
                for i in range(0, 25, 3):
                    x = x0 + (x1 - x0) * i / 24
                    d.text((x, 548), (now + timedelta(hours=i)).strftime("%H:00"), font=F("reg", 16),
                           fill=MUTED, anchor="ms")
                nx = x0 + (x1 - x0) * now.minute / 60 / 24
                d.line((nx, y0 - 10, nx, y1 + 6), fill=DIM, width=2)
        d.text((W - M, 576), "Model data, not for navigation", font=F("reg", 14), fill=DIM, anchor="rs")
        return img

    def render_timers(self):
        img = background(True)
        d = ImageDraw.Draw(img)
        self.buttons = []
        d.text((M, 62), "Timer", font=F("bold", 30), fill=TEXT, anchor="ls")
        if self.timer_end is None:
            g, cw = 20, (W - 2 * M - 60) // 4
            for i, mins in enumerate(TIMER_CHOICES):
                x = M + i * (cw + g)
                self.button(img, d, (x, 140, x + cw, 400), "", lambda m=mins: self.start_timer(m))
                d.text((x + cw / 2, 300), str(mins), font=F("heavy", 120), fill=TEXT, anchor="ms")
                d.text((x + cw / 2, 352), "minutes", font=F("reg", 26), fill=MUTED, anchor="ms")
            d.text((W // 2, 468), "The screen flashes when the time is up.", font=F("reg", 22),
                   fill=MUTED, anchor="ms")
        else:
            left = max(0, self.timer_end - time.monotonic())
            mm, ss = divmod(int(left + 0.999), 60)
            f, dw, cw = clock_metrics(760, 230)
            draw_clock(d, "%02d:%02d" % (mm, ss), (W - (4 * dw + cw)) / 2, 330, 760, biggest=230)
            d.text((W // 2, 398), "Ends at %s" % self.timer_wall.strftime("%H:%M"), font=F("reg", 28),
                   fill=MUTED, anchor="ms")
            self.button(img, d, (W // 2 - 250, 436, W // 2 - 10, 508), "+1 minute", self.add_minute)
            self.button(img, d, (W // 2 + 10, 436, W // 2 + 250, 508), "Cancel", self.cancel_timer)
        return img

    def start_timer(self, mins):
        self.timer_end = time.monotonic() + mins * 60
        self.timer_wall = local_now() + timedelta(minutes=mins)
        log("timer started:", mins, "min")

    def add_minute(self):
        self.timer_end += 60
        self.timer_wall += timedelta(minutes=1)

    def cancel_timer(self):
        self.timer_end = self.timer_wall = None

    def start_ring(self):
        self.timer_end = self.timer_wall = None
        self.ring_until = time.monotonic() + RING_SECS
        last_touch[0] = time.monotonic()
        self.mode = "ring"
        log("timer finished")

    def stop_ring(self):
        self.ring_until = None
        self.view = "dash"
        self.go_clock()

    def render_ring(self):
        on = int(time.monotonic() * 2) % 2 == 0
        img = Image.new("RGB", (W, H), ACCENT if on else (0, 0, 0))
        d = ImageDraw.Draw(img)
        d.text((W // 2, 290), "Time's up!", font=F("heavy", 130), fill=(255, 255, 255), anchor="mm")
        d.text((W // 2, 420), "Tap anywhere to stop", font=F("reg", 30), fill=(255, 255, 255), anchor="mm")
        self.buttons = [((0, 0, W, H), self.stop_ring)]
        return img

    def render_alerts(self):
        img = background(True)
        d = ImageDraw.Draw(img)
        self.header(img, d, "Weather warnings", self.go_clock)
        now = local_now()
        if not self.alert_list:
            d.text((W // 2, 300), "No warnings right now", font=F("bold", 30), fill=MUTED, anchor="mm")
        y = 100
        for a in self.alert_list:
            lines = wrap(a["detail"], F("reg", 19), W - 2 * M - 60)
            if len(lines) > 3:
                lines = lines[:3]
                lines[2] += " ..."
            h = 62 + 26 * len(lines)
            if y + h > 566:
                break
            card(img, M, y, W - 2 * M, h)
            card(img, M, y, 12, h, r=6, fill=ALERT_COLORS[a["level"]])
            d.text((M + 30, y + 38), a["title"], font=fit(a["title"], "bold", 24, 560), fill=TEXT, anchor="ls")
            d.text((W - M - 20, y + 38), alert_when(a, now), font=F("reg", 18), fill=MUTED, anchor="rs")
            for k, line in enumerate(lines):
                d.text((M + 30, y + 66 + 26 * k), line, font=font_for(line, "reg", 19), fill=MUTED, anchor="ls")
            y += h + 12
        if self.alert_source:
            d.text((M, 590), "Source: " + self.alert_source, font=F("reg", 15), fill=DIM, anchor="ls")
        return img

    # ---- settings
    def open_settings(self, slide=False):
        conn, _ = wifi_status()
        self.wifi_name = conn or "Not connected"
        self.settings_page = 0
        self.go("settings")
        if slide and self.fb:  # Settings sits to the left of the dashboard
            self.slide_to(self.render_settings(), -1)
            self.dirty = False
            self.slid = True

    def close_settings(self):
        self.mode = "clock"
        if self.fb:
            self.slide_view(1)
            self.slid = True

    def render_settings(self):
        img = background(True)
        d = ImageDraw.Draw(img)
        first = self.settings_page == 0
        self.header(img, d, "Settings", self.close_settings if first else lambda: self.set_settings_page(0))
        box = (W - M - 80, 20, W - M, 80)
        self.button(img, d, box, "", self.confirm_power)
        pic = power_icon(36, TEXT)
        img.paste(pic, ((box[0] + box[2]) // 2 - 18, (box[1] + box[3]) // 2 - 18), pic)
        last = self.settings_page == SETTINGS_PAGES - 1
        if not last:
            self.button(img, d, (W - M - 242, 20, W - M - 92, 80), "More",
                        lambda: self.set_settings_page(self.settings_page + 1))
        onoff = lambda k: "On" if SET.get(k) else "Off"
        if last:
            tiles = [
                ("Screen off schedule", onoff("screen_off"), lambda: self.toggle("screen_off"),
                 "toggle:screen_off"),
                ("", "", None, None),
                ("Turn off at", hhmm(SET["off_from"]), None, "time:off_from"),
                ("Turn on at", hhmm(SET["off_until"]), None, "time:off_until"),
            ]
            d.text((M, 372), "Between these times the screen goes dark %d seconds after the last touch."
                   % SCREEN_OFF_SECS, font=F("reg", 20), fill=MUTED, anchor="ls")
            d.text((M, 402), "Touch it to see the clock again. The clock keeps running.",
                   font=F("reg", 20), fill=MUTED, anchor="ls")
        elif first:
            tiles = [
                ("Theme", SET["theme"], lambda: self.open_themes("theme"), None),
                ("Location", SET["place"], self.start_location, None),
                ("Day brightness", "%d%%" % SET["day_bright"], None, "day_bright"),
                ("Night brightness", "%d%%" % SET["night_bright"], None, "night_bright"),
                ("Night mode", label_of(NIGHT_MODES, SET["night_mode"]),
                 lambda: self.cycle("night_mode", NIGHT_MODES), None),
                ("Night hours", label_of(NIGHT_HOURS, SET["night_hours"]),
                 lambda: self.cycle("night_hours", NIGHT_HOURS), None),
                ("Screen rotation", label_of(ROTATIONS, SET["rotate"]),
                 lambda: self.cycle("rotate", ROTATIONS), None),
                ("Wi-Fi", self.wifi_name, self.open_wifi, None),
            ]
        else:
            tiles = [
                ("Sea conditions page", onoff("sea"), lambda: self.toggle("sea"), "toggle:sea"),
                ("Rain alerts", onoff("rain_alerts"), lambda: self.toggle("rain_alerts"), "toggle:rain_alerts"),
                ("Weather warnings", onoff("warnings"), lambda: self.toggle("warnings"), "toggle:warnings"),
                ("Burn-in protection", onoff("pixel_shift"), lambda: self.toggle("pixel_shift"),
                 "toggle:pixel_shift"),
                ("Animations", onoff("animations"), lambda: self.toggle("animations"), "toggle:animations"),
                ("Night theme", SET.get("night_theme") or "Off", lambda: self.open_themes("night_theme"), None),
                ("Phone settings", ("On, PIN %s" % WEB["pin"]) if WEB["server"] else "Off",
                 self.phone_settings, None),
                ("Status & updates", "Version %s" % VERSION, self.open_status, None),
            ]
        colw, th, g, y0 = (W - 2 * M - 12) // 2, 108, 12, 100
        for i, (label, value, action, kind) in enumerate(tiles):
            x = M + (i % 2) * (colw + g)
            y = y0 + (i // 2) * (th + g)
            if not label:  # empty slot
                continue
            card(img, x, y, colw, th)
            d.text((x + 22, y + 40), label, font=F("reg", 20), fill=MUTED, anchor="ls")
            stepper = kind and not kind.startswith("toggle")
            room = colw - 44 - (160 if stepper else 90 if kind else 30)
            d.text((x + 22, y + 82), value, font=fit(value, "bold", 30, room), fill=TEXT, anchor="ls")
            if stepper:
                if kind.startswith("time:"):  # half-hour steps
                    less, more = (lambda k=kind[5:]: self.step_time(k, -30)), (lambda k=kind[5:]: self.step_time(k, 30))
                else:
                    less, more = (lambda k=kind: self.step(k, -10)), (lambda k=kind: self.step(k, 10))
                self.button(img, d, (x + colw - 164, y + 20, x + colw - 94, y + 88), "-", less, size=36)
                self.button(img, d, (x + colw - 84, y + 20, x + colw - 14, y + 88), "+", more, size=36)
                continue
            self.buttons.append(((x, y, x + colw, y + th), action))
            if kind:  # on/off switch
                on = bool(SET.get(kind.split(":")[1]))
                sx, sy = x + colw - 92, y + th // 2 - 18
                card(img, sx, sy, 68, 36, r=18, fill=ACCENT if on else DIM)
                kx = sx + (50 if on else 18)
                d.ellipse((kx - 13, sy + 5, kx + 13, sy + 31), fill=(255, 255, 255))
            else:
                d.text((x + colw - 24, y + th // 2), ">", font=F("bold", 30), fill=MUTED, anchor="rm")
        for k in range(SETTINGS_PAGES):  # page dots
            cx = W // 2 - 11 * (SETTINGS_PAGES - 1) + 22 * k
            r = 5 if k == self.settings_page else 4
            d.ellipse((cx - r, 588 - r, cx + r, 588 + r), fill=TEXT if k == self.settings_page else DIM)
        return img

    def set_settings_page(self, page):
        self.settings_page = page

    def step_time(self, key, delta):
        SET[key] = (SET[key] + delta) % (24 * 60)
        save_settings()

    def toggle(self, key):
        SET[key] = not SET.get(key)
        save_settings()
        if key in ("sea", "warnings"):
            refresh_now.set()  # fetch the new data now
        self.shown = None

    def phone_settings(self):
        if not WEB["server"]:
            try:
                web_start()
            except OSError as e:
                log("phone settings page failed:", e)
                self.message("Could not start phone settings", str(e)[:140],
                             [("OK", lambda: self.go("settings"), True)])
                return
            SET["web"] = True
            save_settings()
        self.phone_message(wifi_status()[1])

    def phone_message(self, ip):
        self.message("Phone settings are on",
                     "On a phone on the same Wi-Fi, open  http://%s:%d  and enter PIN %s"
                     % (ip or "<clock IP>", WEB_PORT, WEB["pin"]),
                     [("Turn off", self.phone_off, False), ("OK", lambda: self.go("settings"), True)])

    def phone_off(self):
        web_stop()
        SET["web"] = False
        save_settings()
        self.go("settings")

    def open_status(self):
        self.status_info = system_status()
        self.update_tag, self.update_note = None, ""
        self.go("status")

    def render_status(self):
        img = background(True)
        d = ImageDraw.Draw(img)
        self.header(img, d, "Status & updates", lambda: self.go("settings"))
        colw, th, g = (W - 2 * M - 12) // 2, 78, 10
        for i, (label, value) in enumerate(self.status_info):
            x = M + (i % 2) * (colw + g)
            y = 98 + (i // 2) * (th + g)
            card(img, x, y, colw, th)
            d.text((x + 22, y + 30), label, font=F("reg", 17), fill=MUTED, anchor="ls")
            d.text((x + 22, y + 62), value, font=fit(value, "bold", 24, colw - 44), fill=TEXT, anchor="ls")
        if self.update_tag:
            self.button(img, d, (W - M - 320, 470, W - M, 540), "Install %s" % self.update_tag,
                        self.install_update, primary=True)
        else:
            self.button(img, d, (W - M - 320, 470, W - M, 540), "Check for updates", self.check_updates)
        if self.update_note:
            for k, line in enumerate(wrap(self.update_note, F("reg", 20), 560)[:2]):
                d.text((M, 498 + 28 * k), line, font=F("reg", 20), fill=MUTED, anchor="ls")
        return img

    def check_updates(self):
        try:
            tag = self.busy("Checking GitHub for updates...", latest_release)
        except urllib.error.HTTPError as e:
            self.update_note = ("No release has been published yet." if e.code == 404
                                else "GitHub answered with error %d." % e.code)
        except Exception as e:
            log("update check failed:", e)
            self.update_note = "Could not reach GitHub. Check the Wi-Fi connection."
        else:
            if version_tuple(tag) > version_tuple(VERSION):
                self.update_tag = tag
                self.update_note = "Version %s is available (you have %s)." % (tag, VERSION)
            else:
                self.update_note = "Up to date. The latest release is %s." % tag
        self.mode = "status"

    def install_update(self):
        tag = self.update_tag
        try:
            self.busy("Installing %s..." % tag, lambda: install_release(tag))
        except Exception as e:
            log("update failed:", e)
            self.message("Update failed", str(e)[:140], [("OK", lambda: self.go("status"), True)])
            return
        self.message("Updated to %s" % tag, "The clock is restarting.", [])
        if self.fb:
            self.redraw()
        subprocess.Popen(["systemctl", "restart", "weatherclock"])

    def step(self, key, delta):
        lo = 10 if key == "day_bright" else 5
        SET[key] = max(lo, min(100, SET[key] + delta))
        save_settings()
        self.bright_preview = (SET[key], time.monotonic() + 4)  # show the new level for a moment

    def cycle(self, key, options):
        values = [v for v, _ in options]
        i = values.index(SET[key]) if SET[key] in values else -1
        SET[key] = values[(i + 1) % len(values)]
        save_settings()

    # ---- power
    def confirm_power(self):
        self.message("Turn off the clock?",
                     "Don't use the side button or pull the plug: the Pad's files can be damaged. "
                     "Shut down here first, then unplug.",
                     [("Cancel", lambda: self.go("settings"), False),
                      ("Restart", lambda: self.power("reboot", "Restarting...",
                                                     "The clock will be back in about a minute."), False),
                      ("Shut down", lambda: self.power("poweroff", "Shutting down...",
                                                       "Wait 30 seconds, then it is safe to unplug the Pad."), True)])

    def power(self, verb, title, body):
        self.message(title, body, [])
        if self.fb:
            self.redraw()
        log("power:", verb)
        os.sync()
        try:
            r = subprocess.run(["systemctl", verb], capture_output=True, text=True, timeout=30)
            err = "" if r.returncode == 0 else (r.stderr.strip() or "systemctl %s failed" % verb)
        except (OSError, subprocess.SubprocessError) as e:
            err = str(e)
        if err:
            log("power failed:", err)
            self.message("Could not %s" % ("restart" if verb == "reboot" else "shut down"), err[:140],
                         [("OK", lambda: self.go("settings"), True)])

    # ---- theme picker
    def render_themes(self):
        img = background(True)
        d = ImageDraw.Draw(img)
        night = self.theme_target == "night_theme"
        self.header(img, d, "Night theme" if night else "Theme", lambda: self.go("settings"))
        current = SET.get(self.theme_target) or ""
        if night:
            self.button(img, d, (W - M - 170, 20, W - M, 80), "Off", lambda: self.pick_theme(""),
                        primary=not current)
        cols, rows, g, y0 = 4, 3, 14, 104
        per_page = cols * rows
        pages = (len(THEME_ORDER) + per_page - 1) // per_page
        self.theme_page = max(0, min(self.theme_page, pages - 1))
        cw = (W - 2 * M - g * (cols - 1)) // cols
        ch = 128
        shown = THEME_ORDER[self.theme_page * per_page:(self.theme_page + 1) * per_page]
        for i, name in enumerate(shown):
            th = THEMES[name]
            x = M + (i % cols) * (cw + g)
            y = y0 + (i // cols) * (ch + g)
            if name == current:
                card(img, x - 5, y - 5, cw + 10, ch + 10, r=22, fill=ACCENT)
            img.paste(gradient(th["day"][0], th["day"][1], cw, ch), (x, y), rmask(cw, ch, 18))
            d.text((x + 16, y + 38), name, font=F("bold", 24), fill=th["text"], anchor="ls")
            d.text((x + 16, y + 106), "12:34", font=F("heavy", 40), fill=th["text"], anchor="ls")
            card(img, x + cw - 78, y + 78, 62, 34, r=10, fill=th["card"])
            card(img, x + cw - 68, y + 88, 42, 14, r=7, fill=th["accent"])
            self.buttons.append(((x, y, x + cw, y + ch), lambda n=name: self.pick_theme(n)))
        if pages > 1:
            yb = 536
            if self.theme_page > 0:
                self.button(img, d, (M, yb, M + 200, yb + 54), "Previous", lambda: self.theme_step(-1))
            if self.theme_page < pages - 1:
                self.button(img, d, (W - M - 200, yb, W - M, yb + 54), "Next", lambda: self.theme_step(1))
            d.text((W // 2, yb + 27), "Page %d of %d" % (self.theme_page + 1, pages),
                   font=F("reg", 20), fill=MUTED, anchor="mm")
        return img

    def open_themes(self, target="theme"):
        self.theme_target = target
        current = SET.get(target) or ""
        self.theme_page = THEME_ORDER.index(current) // 12 if current in THEME_ORDER else 0
        self.go("themes")

    def theme_step(self, step):
        self.theme_page += step

    def pick_theme(self, name):
        SET[self.theme_target] = name
        save_settings()
        with lock:
            data = state["data"]
        self.sync_theme(in_night_window(local_now(), data))

    def sync_theme(self, night):
        """Use the night theme during night hours (if one is chosen), the main theme otherwise."""
        want = SET["night_theme"] if night and SET.get("night_theme") in THEMES else SET["theme"]
        if want != THEME_NAME:
            apply_theme(want)
            self.shown = None
            self.dirty = True

    # ---- location search
    def start_location(self):
        self.open_keyboard(title="Search for a town or city", placeholder="e.g. Sines",
                           submit="Search", on_submit=self.search_location,
                           on_cancel=lambda: self.go("settings"), secret=False)

    def search_location(self):
        q = self.text.strip()
        if len(q) < 2:
            self.note = "Type at least 2 letters"
            return
        try:
            places = self.busy("Searching for %s..." % q, lambda: geocode(q))
        except Exception as e:
            log("place search failed:", e)
            self.message("Search failed", "Check the Wi-Fi connection and try again.",
                         [("OK", lambda: self.go("keyboard"), True)])
            return
        if not places:
            self.mode = "keyboard"
            self.note = "No places found"
            return
        self.places = places
        self.go("places")

    def render_places(self):
        img = background(True)
        d = ImageDraw.Draw(img)
        self.header(img, d, "Choose a place", lambda: self.go("keyboard"))
        for i, p in enumerate(self.places[:5]):
            y = 104 + i * 92
            card(img, M, y, W - 2 * M, 80)
            d.text((M + 24, y + 50), p["label"], font=fit(p["label"], "bold", 28, W - 2 * M - 48),
                   fill=TEXT, anchor="ls")
            self.buttons.append(((M, y, W - M, y + 80), lambda p=p: self.pick_place(p)))
        return img

    def pick_place(self, p):
        set_place(p)
        self.go_clock()

    # ---- Wi-Fi list
    def open_wifi(self):
        self.page = 0
        self.do_scan()

    def do_scan(self):
        nets, saved, (conn, ip) = self.busy("Scanning for networks...", wifi_scan)
        self.nets, self.saved = nets, saved
        self.status = ("Connected to %s (%s)" % (conn, ip or "no IP")) if conn else "Not connected"
        self.wifi_name = conn or "Not connected"
        self.mode = "wifi"

    def render_wifi(self):
        img = background(True)
        d = ImageDraw.Draw(img)
        self.header(img, d, "Wi-Fi", lambda: self.go("settings"))
        self.button(img, d, (W - M - 170, 20, W - M, 80), "Rescan", self.do_scan)
        d.text((M, 118), self.status, font=F("reg", 20), fill=MUTED, anchor="ls")
        rows, rh, gap, y0 = 5, 70, 8, 134
        pages = max(1, (len(self.nets) + rows - 1) // rows)
        self.page = min(self.page, pages - 1)
        if not self.nets:
            d.text((W // 2, 300), "No networks found. Tap Rescan.", font=F("bold", 26), fill=MUTED, anchor="mm")
        for i, n in enumerate(self.nets[self.page * rows:(self.page + 1) * rows]):
            y = y0 + i * (rh + gap)
            card(img, M, y, W - 2 * M, rh, fill=ACTIVE if n["active"] else CARD)
            self.buttons.append(((M, y, W - M, y + rh), lambda n=n: self.pick(n)))
            d.text((M + 24, y + 45), n["ssid"], font=fit(n["ssid"], "bold", 26, 640), fill=TEXT, anchor="ls")
            tag = ("Connected" if n["active"] else "Saved" if n["saved"]
                   else "Password" if n["secure"] else "Open")
            d.text((W - M - 104, y + 44), tag, font=F("reg", 18), fill=MUTED, anchor="rs")
            for b in range(4):  # signal bars
                bh = 10 + b * 8
                bx = W - M - 80 + b * 14
                on = n["signal"] >= 20 + b * 20
                d.rectangle((bx, y + 52 - bh, bx + 9, y + 52), fill=TEXT if on else DIM)
        if pages > 1:
            yb = y0 + rows * (rh + gap) + 2
            if self.page > 0:
                self.button(img, d, (M, yb, M + 200, yb + 54), "Previous", lambda: self.set_page(-1))
            if self.page < pages - 1:
                self.button(img, d, (W - M - 200, yb, W - M, yb + 54), "Next", lambda: self.set_page(1))
            d.text((W // 2, yb + 27), "Page %d of %d" % (self.page + 1, pages),
                   font=F("reg", 20), fill=MUTED, anchor="mm")
        return img

    def set_page(self, step):
        self.page += step

    def pick(self, n):
        self.sel = n
        if n["ssid"] in self.saved:
            self.mode = "netmenu"
        elif not n["secure"]:
            self.connect_new(n["ssid"], None)
        else:
            self.start_password()

    # ---- saved network menu
    def render_netmenu(self):
        img = background(True)
        d = ImageDraw.Draw(img)
        self.buttons = []
        n = self.sel
        d.text((W // 2, 110), n["ssid"], font=fit(n["ssid"], "bold", 38, W - 2 * M), fill=TEXT, anchor="ms")
        d.text((W // 2, 150), "Connected" if n["active"] else "Saved network",
               font=F("reg", 22), fill=MUTED, anchor="ms")
        x0, x1, y = W // 2 - 230, W // 2 + 230, 190
        if not n["active"]:
            self.button(img, d, (x0, y, x1, y + 72), "Connect", self.connect_saved, primary=True)
            y += 88
        self.button(img, d, (x0, y, x1, y + 72), "Change password", self.start_password)
        y += 88
        self.button(img, d, (x0, y, x1, y + 72), "Forget this network", self.confirm_forget)
        y += 88
        self.button(img, d, (x0, y, x1, y + 72), "Back", lambda: self.go("wifi"))
        return img

    def confirm_forget(self):
        extra = " The clock will go offline until you join another network." if self.sel["active"] else ""
        self.message("Forget %s?" % self.sel["ssid"], "The saved password will be deleted." + extra,
                     [("Cancel", lambda: self.go("netmenu"), False), ("Forget", self.forget, True)])

    def forget(self):
        name = self.saved.get(self.sel["ssid"], self.sel["ssid"])
        self.busy("Forgetting %s..." % self.sel["ssid"], lambda: nm("connection", "delete", "id", name))
        self.do_scan()

    def connect_saved(self):
        ssid = self.sel["ssid"]
        name = self.saved.get(ssid, ssid)
        code, out, err = self.busy("Connecting to %s..." % ssid,
                                   lambda: nm("connection", "up", "id", name, timeout=60))
        self.after_connect(ssid, code, out, err, cleanup=False)

    def start_password(self):
        self.open_keyboard(title=self.sel["ssid"], placeholder="Type the Wi-Fi password",
                           submit="Connect", on_submit=self.submit_password,
                           on_cancel=lambda: self.go("netmenu" if self.sel["ssid"] in self.saved else "wifi"),
                           secret=True)

    def submit_password(self):
        if len(self.text) < 8:
            self.note = "At least 8 characters"
            return
        self.connect_new(self.sel["ssid"], self.text)

    def connect_new(self, ssid, pw):
        def job():
            old = self.saved.get(ssid)
            if old:  # replace the saved profile (new password)
                nm("connection", "delete", "id", old)
            args = ["device", "wifi", "connect", ssid]
            if pw:
                args += ["password", pw]
            return nm(*(args + ["ifname", WIFI_IF]), timeout=60)
        code, out, err = self.busy("Connecting to %s..." % ssid, job)
        self.after_connect(ssid, code, out, err, cleanup=True)

    def after_connect(self, ssid, code, out, err, cleanup):
        if code == 0:
            log("wifi connected to", ssid)
            refresh_now.set()
            self.message("Connected", "The clock is now on %s." % ssid, [("OK", self.go_clock, True)])
            return
        log("wifi connect to %s failed: %s" % (ssid, err or out))
        if cleanup:  # do not leave a profile with a wrong password behind
            nm("connection", "delete", "id", ssid)
            self.saved.pop(ssid, None)
        low = (err + out).lower()
        if "secret" in low or "password" in low or "802-1x" in low:
            reason = "Wrong password, or the network did not respond."
        elif "no network with ssid" in low:
            reason = "That network is out of range."
        else:
            reason = (err or out or "Unknown error")[:140]
        self.message("Could not connect to %s" % ssid, reason, [("OK", lambda: self.go("wifi"), True)])

    # ---- on-screen keyboard
    def open_keyboard(self, **kb):
        self.kb = kb
        self.text, self.layer, self.note = "", "lower", ""
        self.mode = "keyboard"

    def render_keyboard(self):
        img = background(True)
        d = ImageDraw.Draw(img)
        kb = self.kb
        self.buttons = []
        self.button(img, d, (M, 16, M + 170, 76), "Cancel", kb["on_cancel"])
        self.button(img, d, (W - M - 190, 16, W - M, 76), kb["submit"], kb["on_submit"], primary=True)
        d.text((W // 2, 56), kb["title"], font=fit(kb["title"], "bold", 28, W - 2 * M - 400),
               fill=TEXT, anchor="ms")
        card(img, M, 92, W - 2 * M, 80, fill=FIELD)
        f = F("reg", 34)
        if self.text:
            txt = (self.text if (self.show_pw or not kb["secret"]) else "*" * len(self.text)) + "|"
            f = font_for(txt, "reg", 34)
            while f.getlength(txt) > W - 2 * M - 48:
                txt = txt[1:]
            d.text((M + 22, 145), txt, font=f, fill=TEXT, anchor="ls")
        else:
            d.text((M + 22, 145), kb["placeholder"], font=f, fill=DIM, anchor="ls")
        if self.note:
            d.text((W - M - 20, 140), self.note, font=F("bold", 18), fill=WARN, anchor="rs")

        rows = LAYERS[self.layer]
        kw, kh, g = 90, 80, 8
        ys = [192, 282, 372, 462]
        L, R = 26, W - 26

        def keyrow(chars, y, x0, x1):
            total = len(chars) * kw + (len(chars) - 1) * g
            x = (x0 + x1 - total) // 2
            for c in chars:
                self.button(img, d, (x, y, x + kw, y + kh), c, lambda c=c: self.type(c), size=30)
                x += kw + g

        keyrow(rows[0], ys[0], L, R)
        keyrow(rows[1], ys[1], L, R)
        left = {"lower": ("Shift", "upper"), "upper": ("Shift", "lower"),
                "sym1": ("#+=", "sym2"), "sym2": ("123", "sym1")}[self.layer]
        self.button(img, d, (L, ys[2], L + 130, ys[2] + kh), left[0],
                    lambda: self.set_layer(left[1]), primary=self.layer == "upper")
        self.button(img, d, (R - 130, ys[2], R, ys[2] + kh), "Delete", self.backspace)
        keyrow(rows[2], ys[2], L + 130 + g, R - 130 - g)
        letters = self.layer in ("lower", "upper")
        self.button(img, d, (L, ys[3], L + 170, ys[3] + kh), "?123" if letters else "ABC",
                    lambda: self.set_layer("sym1" if letters else "lower"))
        space_end = R - 170 - g if kb["secret"] else R
        self.button(img, d, (L + 170 + g, ys[3], space_end, ys[3] + kh), "space", lambda: self.type(" "))
        if kb["secret"]:
            self.button(img, d, (R - 170, ys[3], R, ys[3] + kh), "Hide" if self.show_pw else "Show",
                        self.toggle_show)
        return img

    def type(self, c):
        if len(self.text) < 63:
            self.text += c
        self.note = ""

    def backspace(self):
        self.text = self.text[:-1]
        self.note = ""

    def set_layer(self, layer):
        self.layer = layer

    def toggle_show(self):
        self.show_pw = not self.show_pw

    # ---- messages
    def message(self, title, body, buttons):
        self.msg = (title, body, buttons)
        self.mode = "message"

    def render_message(self):
        img = background(True)
        d = ImageDraw.Draw(img)
        self.buttons = []
        title, body, buttons = self.msg
        y = 250 if buttons else 300
        d.text((W // 2, y), title, font=fit(title, "bold", 36, W - 2 * M), fill=TEXT, anchor="ms")
        for line in wrap(body, font_for(body, "reg", 24), 820):
            y += 40
            d.text((W // 2, y), line, font=font_for(line, "reg", 24), fill=MUTED, anchor="ms")
        if buttons:
            bw, g = 240, 24
            x = (W - (len(buttons) * bw + (len(buttons) - 1) * g)) // 2
            for label, action, primary in buttons:
                self.button(img, d, (x, 420, x + bw, 492), label, action, primary=primary)
                x += bw + g
        return img

    def busy(self, text, fn):
        self.message(text, "", [])
        if self.fb:
            self.redraw()
        try:
            return fn()
        finally:
            self.last_input = time.monotonic()
            while True:  # drop taps made while we were busy
                try:
                    events.get_nowait()
                except queue.Empty:
                    break

    # ---- input
    def handle(self, kind, x, y):
        if kind == "web":  # settings changed from the phone page
            self.shown = None
            self.dirty = True
            return
        if kind not in ("tap", "long", "swipe_l", "swipe_r"):
            return
        log("%s at %d,%d (%s)" % (kind, x, y, self.mode))
        self.last_input = time.monotonic()
        if self.mode == "ring":  # any touch stops a finished timer
            self.stop_ring()
            return
        if self.mode == "clock":
            if kind == "long":
                self.open_settings()
                self.dirty = True
                return
            if self.dimmed:  # first touch at night only wakes the screen
                self.shown = None
                return
        if kind in ("swipe_l", "swipe_r"):
            self.swipe(1 if kind == "swipe_l" else -1)
            self.dirty, self.slid = not self.slid, False
            return
        for (x0, y0, x1, y1), action in list(self.buttons):
            if x0 <= x <= x1 and y0 <= y <= y1:
                action()
                if self.slid:  # the slide already drew the new screen
                    self.slid = self.dirty = False
                    return
                self.dirty = True
                if self.mode == "clock":
                    self.shown = None
                return

    def swipe(self, step):
        if self.mode == "clock":
            vs = self.views()
            i = vs.index(self.view) if self.view in vs else 0
            if step < 0 and i == 0:  # swipe right on the dashboard: Settings
                self.open_settings(slide=True)
                return
            j = max(0, min(len(vs) - 1, i + step))
            if j != i:
                self.view = vs[j]
                self.slide_view(step)
                self.slid = True
        elif self.mode == "day":
            self.day_index += step  # clamped when drawn
        elif self.mode == "settings":
            self.settings_page = max(0, min(SETTINGS_PAGES - 1, self.settings_page + step))
        elif self.mode == "themes":
            pages = (len(THEME_ORDER) + 11) // 12
            self.theme_page = max(0, min(pages - 1, self.theme_page + step))

    # ---- main loop
    def run(self):
        while True:
            now = local_now()
            mono = time.monotonic()
            with lock:
                data, updated, error = state["data"], state["updated"], state["error"]
            if self.trial_until and mono >= self.trial_until:
                confirm_update()
                self.trial_until = None
            if self.timer_end is not None and mono >= self.timer_end:
                self.start_ring()
            if self.mode == "ring" and mono > self.ring_until:
                self.stop_ring()
            idle = mono - self.last_input > IDLE_SECS
            if self.mode not in ("clock", "ring") and idle:
                self.go_clock()
            if self.view not in self.views() or (
                    idle and self.view != "dash" and not (self.view == "timers" and self.timer_end)):
                self.set_view("dash")
            awake = mono - last_touch[0] < WAKE_SECS
            night = in_night_window(now, data)
            self.sync_theme(night)
            quiet = night and SET["night_mode"] != "off" and not awake and self.mode == "clock"
            off_hours = in_screen_off_window(now)
            dark = off_hours and self.mode == "clock" and mono - last_touch[0] >= SCREEN_OFF_SECS
            animating = False
            if dark:
                self.shown = None  # draw a fresh picture when it wakes
            elif self.mode == "clock":
                view = "bedside" if quiet and SET["night_mode"] == "bedside" else self.view
                key = self.clock_key(now, updated, error, view, mono)
                if key != self.shown:
                    if view == "bedside":
                        self.buttons = []
                        self.show(render_bedside(now, data), shift=True)
                    else:
                        self.show(self.render_view(now, data, updated, error), shift=True)
                    self.shown = key
                    self.anim_frame = None
                animating = bool(SET.get("animations") and view == "dash" and data and not quiet)
                if animating:
                    self.animate_icon(data)
            elif self.mode == "ring" or self.dirty:
                self.redraw()
            level = SET["night_bright"] if quiet else SET["day_bright"]
            if self.bright_preview:
                if mono < self.bright_preview[1]:
                    level = self.bright_preview[0]
                else:
                    self.bright_preview = None
            if self.mode == "ring":  # flash the backlight too
                level = 100 if int(mono * 2) % 2 == 0 else 25
            self.bl.power(not dark)
            if not dark:
                self.bl.set(level)
            self.dimmed = quiet or dark
            now = local_now()
            timeout = 60 - now.second - now.microsecond / 1e6 + 0.05
            if night or self.bright_preview:
                timeout = min(timeout, 2)
            if off_hours and not dark and self.mode == "clock":  # go dark on time after the last touch
                timeout = min(timeout, max(0.2, last_touch[0] + SCREEN_OFF_SECS - time.monotonic()))
            if self.mode != "clock":
                timeout = min(timeout, 10)
            if self.timer_end is not None:
                left = self.timer_end - time.monotonic()
                timeout = min(timeout, max(0.05, left), 0.5 if self.view == "timers" else 60)
            if self.mode == "ring":
                timeout = min(timeout, 0.5)
            if animating:  # wake for the next icon frame
                timeout = min(timeout, max(0.01, (int(mono * ANIM_FPS) + 1) / ANIM_FPS - time.monotonic()))
            try:
                kind, x, y = events.get(timeout=timeout)
            except queue.Empty:
                continue
            try:
                self.handle(kind, x, y)
            except Exception as e:
                log("error handling input:", repr(e))
                self.message("Something went wrong", str(e)[:140], [("OK", self.go_clock, True)])
                self.dirty = True


# ---------------------------------------------------------------- previews
def self_test():
    """Draw the main screens without the display, touch panel or network.
    An older version runs this on a downloaded update before installing it."""
    now = local_now()
    app = App(None, None)
    app.render_view(now, None, None, None)
    app.render_settings()
    render_bedside(now, None)


def preview_ui(folder):
    os.makedirs(folder, exist_ok=True)
    now = local_now()
    data = fetch_all()
    app = App(None, None)
    p = lambda name: os.path.join(folder, name)
    for name in THEME_ORDER:
        apply_theme(name)
        app.render_clock(now, data, now, None).save(p("clock-%s.png" % name.lower()))
    apply_theme("Midnight")
    render_bedside(now, data).save(p("bedside.png"))
    app.render_clock(now, data, now, "offline").save(p("clock-offline.png"))
    app.wifi_name = "HomeNetwork"
    app.render_settings().save(p("settings.png"))
    app.confirm_power()
    app.render_message().save(p("power.png"))
    app.render_themes().save(p("themes.png"))
    app.theme_page = 1
    app.render_themes().save(p("themes-2.png"))
    app.theme_page = 0
    app.start_location()
    app.text = "Lisb"
    app.render_keyboard().save(p("keyboard-location.png"))
    app.places = geocode("Lisboa")
    log("search Lisboa ->", [x["label"] for x in app.places])
    app.render_places().save(p("places.png"))
    app.nets = [
        {"ssid": "HomeNetwork", "signal": 84, "secure": True, "active": True, "saved": True},
        {"ssid": "MEO-5G-1234", "signal": 66, "secure": True, "active": False, "saved": False},
        {"ssid": "Vodafone Guest", "signal": 41, "secure": False, "active": False, "saved": False},
    ]
    app.saved = {"HomeNetwork": "HomeNetwork"}
    app.status = "Connected to HomeNetwork (192.168.1.50)"
    app.render_wifi().save(p("wifi.png"))
    app.sel = app.nets[1]
    app.start_password()
    app.text = "sines2026"
    app.render_keyboard().save(p("keyboard-wifi.png"))
    app.text, app.layer = "sines`~^\\{}2026", "sym1"
    app.render_keyboard().save(p("keyboard-sym1.png"))
    app.layer = "sym2"
    app.render_keyboard().save(p("keyboard-sym2.png"))
    apply_theme("Daylight")
    app.render_settings().save(p("settings-daylight.png"))
    apply_theme("Midnight")
    # new screens
    with lock:
        state.update(data=data, updated=now)
    app.view = "hourly"
    app.render_view(now, data, now, None).save(p("hourly.png"))
    SET["sea"] = True
    try:
        data["sea"] = fetch(marine_url())
    except Exception as e:
        log("sea preview fetch failed:", e)
    app.view = "sea"
    app.render_view(now, data, now, None).save(p("sea.png"))
    app.view = "timers"
    app.render_view(now, data, now, None).save(p("timers.png"))
    app.start_timer(10)
    app.timer_end -= 152  # show a partly run timer
    app.render_view(now, data, now, None).save(p("timer-running.png"))
    app.render_ring().save(p("timer-done.png"))
    app.open_day(today_index(data, now) + 1)
    app.render_day().save(p("day.png"))
    # dashboard with a warning, a running timer and rain on the way
    demo = dict(data)
    demo["current"] = dict(data["current"], precipitation=0)
    demo["minutely_15"] = {"time": [(now + timedelta(minutes=25)).strftime("%Y-%m-%dT%H:%M")],
                           "precipitation": [0.4]}
    demo["alerts"] = [{"level": 3, "title": "Orange Wind Warning",
                       "detail": "Gusts of up to 90 km/h from the northwest, especially on the coast and "
                                 "in high areas.", "start": now - timedelta(hours=2),
                       "end": now + timedelta(hours=9)},
                      {"level": 2, "title": "Yellow Coastal Event Warning",
                       "detail": "Northwest waves of 4 to 5 metres.", "start": now + timedelta(hours=14),
                       "end": now + timedelta(hours=30)}]
    demo["alerts_source"] = "MeteoAlarm"
    app.view = "dash"
    app.render_view(now, demo, now, None).save(p("clock-alerts.png"))
    app.render_alerts().save(p("alerts.png"))
    app.timer_end = app.timer_wall = None
    app.alert_list = forecast_alerts(dict(data, daily=dict(data["daily"], wind_gusts_10m_max=[95] * 7)), now)
    app.alert_source = "Forecast-based (no official warnings available here)"
    app.render_alerts().save(p("alerts-forecast.png"))
    app.settings_page = 1
    app.render_settings().save(p("settings-2.png"))
    app.settings_page = 2
    app.render_settings().save(p("settings-3.png"))
    log("status check returned %d items" % len(system_status()))
    app.status_info = [  # sample values: screenshots are public, so never show the real network
        ("Version", VERSION), ("IP address", "192.168.1.50"), ("Wi-Fi", "HomeNetwork (82%)"),
        ("Uptime", "2 d 5 h"), ("Storage", "2.6 GB free of 7.3 GB"), ("Memory", "1810 MB free of 1983 MB"),
        ("CPU temperature", "49°C"), ("Weather updated", now.strftime("%H:%M"))]
    app.update_note = "Up to date. The latest release is v%s." % VERSION
    app.render_status().save(p("status.png"))
    SET["night_theme"] = "Amber"
    app.open_themes("night_theme")
    app.render_themes().save(p("night-theme.png"))
    WEB["pin"] = "4821"
    app.phone_message("192.168.1.50")
    app.render_message().save(p("phone.png"))
    # animation frames side by side, to check the icon animations (subfolder: not README screenshots)
    os.makedirs(p("anim"), exist_ok=True)
    for kind, day in (("partly", True), ("rain", True), ("thunder", True), ("snow", True),
                      ("clear", False), ("fog", True)):
        sheet = background(True).crop((0, 0, 176 * 6, 176))
        for k, frame in enumerate(range(0, ANIM_FRAMES, ANIM_FRAMES // 6)):
            ic = icon_frame(kind, 176, day, frame)
            sheet.paste(ic, (176 * k, 0), ic)
        sheet.save(p(os.path.join("anim", "%s.png" % kind)))
    print("saved previews to", folder)


def main():
    if "--preview-ui" in sys.argv:
        preview_ui(sys.argv[sys.argv.index("--preview-ui") + 1])
        return
    on_trial = guard_update()
    fb = Framebuffer()
    bl = Backlight()
    bl.on()
    threading.Thread(target=weather_loop, daemon=True).start()
    threading.Thread(target=touch_loop, daemon=True).start()
    if SET.get("web"):
        try:
            web_start()
        except OSError as e:
            log("phone settings page failed:", e)
    app = App(fb, bl)
    if on_trial:
        app.trial_until = time.monotonic() + UPDATE_OK_SECS
    app.run()


if __name__ == "__main__":
    main()
