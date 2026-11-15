#!/usr/bin/env python3
"""Weather clock for a Creality Sonic Pad.

Draws straight to the Linux framebuffer (/dev/fb0) with Pillow, so it needs
no X server, desktop or extra packages. Weather and air quality come from
Open-Meteo.

  python3 weatherclock.py                  run fullscreen (as root)
  python3 weatherclock.py --preview-ui DIR render every screen to PNGs

Hold a finger on the screen for 2 seconds to open Settings.
"""

import json
import math
import os
import queue
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------- constants
W, H = 1024, 600
M = 32                                     # outer margin
FB_DEV = "/dev/fb0"
TOUCH_DEV = "/dev/input/event2"            # gt9xxnew_ts
BRIGHTNESS_BIN = "/usr/bin/brightness"
WIFI_IF = "wlan0"
WAKE_SECS = 30                             # full screen after a tap at night
LONG_PRESS = 2.0                           # seconds held to open Settings
IDLE_SECS = 180                            # settings screens return to the clock
REFRESH_OK, REFRESH_FAIL = 600, 60         # seconds between weather fetches
TOUCH_SWAP_XY = os.environ.get("CLOCK_TOUCH_SWAP_XY") == "1"
TOUCH_INVERT_X = os.environ.get("CLOCK_TOUCH_INVERT_X") == "1"
TOUCH_INVERT_Y = os.environ.get("CLOCK_TOUCH_INVERT_Y") == "1"


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- saved settings
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
DEFAULTS = {
    "theme": "Midnight",
    "place": "Sines, Portugal",
    "lat": 37.9567, "lon": -8.8698, "tz": "Europe/Lisbon",
    "day_bright": 100, "night_bright": 35,
    "night_mode": "bedside", "night_hours": "sun",
    "rotate": int(os.environ.get("CLOCK_ROTATE", "0")),
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


def save_settings():
    try:
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(SET, f, indent=2)
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
}
THEME_ORDER = ["Midnight", "Ocean", "Sunset", "Forest", "Graphite", "Black", "Daylight", "Nord",
               "Dracula", "Solarized", "Rose", "Aurora", "Amber", "Terminal", "Paper", "Sky"]

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


def icon(kind, size, day=True):
    """Weather icon drawn 4x larger, then scaled down for smooth edges."""
    key = (THEME_NAME, kind, size, day)
    if key in _icons:
        return _icons[key]
    S = size * 4
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    c = S / 2

    def circ(x, y, r, fill):
        d.ellipse((x - r, y - r, x + r, y + r), fill=fill)

    def seg(x0, y0, x1, y1, w, fill):
        d.line((x0, y0, x1, y1), fill=fill, width=max(1, int(w)))
        circ(x0, y0, w / 2, fill)
        circ(x1, y1, w / 2, fill)

    def sun(x, y, r):
        for i in range(8):
            a = i * math.pi / 4
            seg(x + math.cos(a) * r * 1.45, y + math.sin(a) * r * 1.45,
                x + math.cos(a) * r * 1.85, y + math.sin(a) * r * 1.85, r * 0.22, SUN)
        circ(x, y, r, SUN)

    def moon(x, y, r):
        circ(x, y, r, MOON)
        circ(x + r * 0.55, y - r * 0.35, r * 0.85, (0, 0, 0, 0))

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

    if kind == "clear":
        sky(c, c, S * 0.2)
    elif kind == "partly":
        sky(c - S * 0.13, c - S * 0.13, S * 0.13)
        cloud(c + S * 0.06, c + S * 0.12, S * 0.64, CLOUD)
    elif kind == "cloudy":
        cloud(c + S * 0.13, c - S * 0.06, S * 0.5, CLOUD2)
        cloud(c - S * 0.05, c + S * 0.1, S * 0.66, CLOUD)
    elif kind == "fog":
        yb = cloud(c, c - S * 0.1, S * 0.62, CLOUD2)
        for i, (a, b) in enumerate(((-0.3, 0.22), (-0.22, 0.3))):
            y = yb + S * (0.1 + 0.11 * i)
            seg(c + S * a, y, c + S * b, y, S * 0.05, CLOUD)
    else:
        yb = cloud(c, c - S * 0.1, S * 0.68, CLOUD)
        if kind == "rain":
            for dx in (-0.18, 0.0, 0.18):
                seg(c + S * dx, yb + S * 0.08, c + S * (dx - 0.06), yb + S * 0.24, S * 0.05, RAIN)
        elif kind == "drizzle":
            for dx in (-0.16, 0.0, 0.16):
                seg(c + S * dx, yb + S * 0.09, c + S * (dx - 0.03), yb + S * 0.16, S * 0.045, RAIN)
        elif kind == "snow":
            for dx, dy in ((-0.18, 0.11), (0.0, 0.15), (0.18, 0.11), (-0.09, 0.27), (0.09, 0.27)):
                circ(c + S * dx, yb + S * dy, S * 0.04, SNOW)
        elif kind == "thunder":
            x, y = c - S * 0.02, yb + S * 0.04
            d.polygon([(x + S * 0.02, y), (x - S * 0.08, y + S * 0.17), (x, y + S * 0.17),
                       (x - S * 0.05, y + S * 0.33), (x + S * 0.12, y + S * 0.11),
                       (x + S * 0.03, y + S * 0.11), (x + S * 0.09, y)], fill=BOLT)
    _icons[key] = im.resize((size, size), Image.LANCZOS)
    return _icons[key]


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


class Backlight:
    def __init__(self):
        self.level = None

    def _run(self, *args):
        try:
            r = subprocess.run([BRIGHTNESS_BIN] + list(args), capture_output=True, text=True, timeout=5)
            return r.returncode == 0 and "error" not in (r.stdout + r.stderr).lower()
        except (OSError, subprocess.SubprocessError) as e:
            log("brightness failed:", e)
            return False

    def on(self):
        self._run("-s", "1")

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
            "&daily=weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,"
            "uv_index_max,precipitation_probability_max&forecast_days=7&timezone=%s"
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
    return data


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
                    "lat": r["latitude"], "lon": r["longitude"], "tz": r.get("timezone") or "auto"})
    return out[:5]


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
                        held = time.monotonic() - t0
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

    def show(self, img):
        if SET["rotate"] == 180:
            img = img.rotate(180)
        self.fb.show(img)

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
            self.buttons.append(((0, 540, 440, H), self.open_wifi))
        return img

    # ---- settings
    def open_settings(self):
        conn, _ = wifi_status()
        self.wifi_name = conn or "Not connected"
        self.go("settings")

    def render_settings(self):
        img = background(True)
        d = ImageDraw.Draw(img)
        self.header(img, d, "Settings", self.go_clock)
        box = (W - M - 80, 20, W - M, 80)
        self.button(img, d, box, "", self.confirm_power)
        pic = power_icon(36, TEXT)
        img.paste(pic, ((box[0] + box[2]) // 2 - 18, (box[1] + box[3]) // 2 - 18), pic)
        tiles = [
            ("Theme", SET["theme"], self.open_themes, None),
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
        colw, th, g, y0 = (W - 2 * M - 12) // 2, 108, 12, 100
        for i, (label, value, action, stepper) in enumerate(tiles):
            x = M + (i % 2) * (colw + g)
            y = y0 + (i // 2) * (th + g)
            card(img, x, y, colw, th)
            d.text((x + 22, y + 40), label, font=F("reg", 20), fill=MUTED, anchor="ls")
            room = colw - 44 - (160 if stepper else 30)
            d.text((x + 22, y + 82), value, font=fit(value, "bold", 30, room), fill=TEXT, anchor="ls")
            if stepper:
                self.button(img, d, (x + colw - 164, y + 20, x + colw - 94, y + 88), "-",
                            lambda k=stepper: self.step(k, -10), size=36)
                self.button(img, d, (x + colw - 84, y + 20, x + colw - 14, y + 88), "+",
                            lambda k=stepper: self.step(k, 10), size=36)
            else:
                self.buttons.append(((x, y, x + colw, y + th), action))
                d.text((x + colw - 24, y + th // 2), ">", font=F("bold", 30), fill=MUTED, anchor="rm")
        return img

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
        self.header(img, d, "Theme", lambda: self.go("settings"))
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
            if name == THEME_NAME:
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

    def open_themes(self):
        if THEME_NAME in THEME_ORDER:  # open on the page with the current theme
            self.theme_page = THEME_ORDER.index(THEME_NAME) // 12
        self.go("themes")

    def theme_step(self, step):
        self.theme_page += step

    def pick_theme(self, name):
        apply_theme(name)
        SET["theme"] = name
        save_settings()

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
        SET.update(place="%s, %s" % (p["name"], p["country"]) if p["country"] else p["name"],
                   lat=p["lat"], lon=p["lon"], tz=p["tz"])
        save_settings()
        with lock:
            state.update(data=None, updated=None, error=None)
        refresh_now.set()
        log("location set to", SET["place"])
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
        if kind not in ("tap", "long"):
            return
        log("%s at %d,%d (%s)" % (kind, x, y, self.mode))
        self.last_input = time.monotonic()
        if self.mode == "clock":
            if kind == "long":
                self.open_settings()
                self.dirty = True
                return
            if self.dimmed:  # first tap at night only wakes the screen
                self.shown = None
                return
        for (x0, y0, x1, y1), action in list(self.buttons):
            if x0 <= x <= x1 and y0 <= y <= y1:
                action()
                self.dirty = True
                if self.mode == "clock":
                    self.shown = None
                return

    # ---- main loop
    def run(self):
        while True:
            now = local_now()
            with lock:
                data, updated, error = state["data"], state["updated"], state["error"]
            if self.mode != "clock" and time.monotonic() - self.last_input > IDLE_SECS:
                self.go_clock()
            awake = time.monotonic() - last_touch[0] < WAKE_SECS
            night = in_night_window(now, data)
            quiet = night and SET["night_mode"] != "off" and not awake and self.mode == "clock"
            if self.mode == "clock":
                view = "bedside" if quiet and SET["night_mode"] == "bedside" else "dash"
                key = (now.strftime("%Y%m%d%H%M"), updated, bool(error), view, THEME_NAME,
                       SET["place"], SET["rotate"])
                if key != self.shown:
                    if view == "bedside":
                        self.buttons = []
                        self.show(render_bedside(now, data))
                    else:
                        self.show(self.render_clock(now, data, updated, error))
                    self.shown = key
            elif self.dirty:
                self.redraw()
            level = SET["night_bright"] if quiet else SET["day_bright"]
            if self.bright_preview:
                if time.monotonic() < self.bright_preview[1]:
                    level = self.bright_preview[0]
                else:
                    self.bright_preview = None
            self.bl.set(level)
            self.dimmed = quiet
            now = local_now()
            timeout = 60 - now.second - now.microsecond / 1e6 + 0.05
            if night or self.bright_preview:
                timeout = min(timeout, 2)
            if self.mode != "clock":
                timeout = min(timeout, 10)
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
    app.status = "Connected to HomeNetwork (192.168.50.140)"
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
    print("saved previews to", folder)


def main():
    if "--preview-ui" in sys.argv:
        preview_ui(sys.argv[sys.argv.index("--preview-ui") + 1])
        return
    fb = Framebuffer()
    bl = Backlight()
    bl.on()
    threading.Thread(target=weather_loop, daemon=True).start()
    threading.Thread(target=touch_loop, daemon=True).start()
    App(fb, bl).run()


if __name__ == "__main__":
    main()
