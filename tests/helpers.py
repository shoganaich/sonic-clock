"""Shared setup for the tests: loads weatherclock.py and builds sample weather data.

The app is one script with module-level state (SET, state, WEB), so every test calls
reset() to start from the default settings, with settings.json in a temporary folder.
"""

import importlib.util
import os
import sys
import tempfile
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_PATH = os.path.join(ROOT, "weatherclock", "weatherclock.py")


def _load():
    if "weatherclock" in sys.modules:
        return sys.modules["weatherclock"]
    spec = importlib.util.spec_from_file_location("weatherclock", APP_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["weatherclock"] = mod
    spec.loader.exec_module(mod)
    return mod


wc = _load()
wc.log = lambda *a: None  # keep the app's log lines out of the test output
_tmp = tempfile.mkdtemp(prefix="sonic-clock-tests-")


def reset():
    """Default settings, no weather, Midnight theme, settings saved to a temporary folder."""
    wc.SETTINGS_FILE = os.path.join(_tmp, "settings.json")
    wc.UPDATE_MARK = os.path.join(_tmp, "update-pending.json")
    for path in (wc.SETTINGS_FILE, wc.UPDATE_MARK):
        if os.path.exists(path):
            os.remove(path)
    wc.SET.clear()
    wc.SET.update(wc.DEFAULTS)
    wc.SET["rotate"] = 0
    wc.state.update(data=None, updated=None, error=None)
    wc.WEB.update(server=None, pin="", tokens=set(), fails={})
    wc.refresh_now.clear()
    while not wc.events.empty():
        wc.events.get_nowait()
    wc.apply_theme("Midnight")


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M")


def sample_data(now):
    """Open-Meteo style weather for 7 days from today, in the shape fetch_all() returns."""
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hours = [day0 + timedelta(hours=i) for i in range(7 * 24)]
    days = [day0 + timedelta(days=i) for i in range(7)]
    return {
        "current": {"temperature_2m": 18.4, "relative_humidity_2m": 71, "apparent_temperature": 17.9,
                    "is_day": 1, "precipitation": 0.0, "weather_code": 2, "wind_speed_10m": 14.2,
                    "wind_direction_10m": 315},
        "hourly": {
            "time": [iso(t) for t in hours],
            "temperature_2m": [14 + 6 * ((t.hour - 4) % 24 < 12) + (t.hour % 12) * 0.3 for t in hours],
            "precipitation_probability": [(i * 7) % 100 for i in range(len(hours))],
            "precipitation": [0.2 if i % 9 == 0 else 0.0 for i in range(len(hours))],
            "weather_code": [(0, 2, 3, 61, 80, 95)[i % 6] for i in range(len(hours))],
            "wind_speed_10m": [10 + i % 15 for i in range(len(hours))],
            "wind_gusts_10m": [20 + i % 25 for i in range(len(hours))],
            "is_day": [1 if 7 <= t.hour < 19 else 0 for t in hours],
        },
        "daily": {
            "time": [t.strftime("%Y-%m-%d") for t in days],
            "weather_code": [0, 2, 3, 61, 71, 95, 45],
            "temperature_2m_max": [22, 21, 19, 17, 9, 20, 23],
            "temperature_2m_min": [12, 11, 10, 9, 1, 11, 13],
            "sunrise": [iso(t.replace(hour=7, minute=12)) for t in days],
            "sunset": [iso(t.replace(hour=18, minute=41)) for t in days],
            "uv_index_max": [6.5, 5.1, 3.0, 2.2, 1.0, 4.4, 7.2],
            "precipitation_probability_max": [5, 20, 45, 90, 70, 60, 10],
            "precipitation_sum": [0, 0.4, 2.1, 14.8, 6.0, 9.5, 0],
            "snowfall_sum": [0, 0, 0, 0, 3.5, 0, 0],
            "wind_gusts_10m_max": [32, 41, 55, 66, 48, 72, 30],
        },
        "minutely_15": {"time": [iso(now + timedelta(minutes=15 * i)) for i in range(8)],
                        "precipitation": [0.0] * 8},
        "air": {"european_aqi": 34, "pm2_5": 8.1, "pm10": 15.3},
        "sea": None,
        "alerts": None,
        "alerts_source": "",
    }


def sample_sea(now):
    import math
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hours = [day0 + timedelta(hours=i) for i in range(72)]
    return {
        "current": {"wave_height": 1.8, "wave_direction": 300, "wave_period": 9.5,
                    "sea_surface_temperature": 17.2},
        "hourly": {"time": [iso(t) for t in hours],
                   "wave_height": [1.5 + 0.5 * math.sin(i / 6) for i in range(len(hours))],
                   "sea_level_height_msl": [1.2 * math.sin(2 * math.pi * i / 12.42) for i in range(len(hours))]},
    }


def fixed_now():
    return datetime(2026, 6, 15, 14, 37)
