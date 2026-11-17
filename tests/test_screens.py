"""Renders every screen offline through preview_ui(), the code behind --preview-ui."""

import contextlib
import copy
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from PIL import Image

from helpers import ROOT, reset, sample_data, sample_sea, wc

PLACES = {"results": [
    {"id": 2267057, "name": "Lisbon", "admin1": "Lisbon", "admin2": "Lisboa", "country": "Portugal",
     "country_code": "PT", "latitude": 38.72, "longitude": -9.13, "timezone": "Europe/Lisbon",
     "population": 517802},
    {"id": 4893, "name": "Lisbon", "admin1": "Maine", "country": "United States", "country_code": "US",
     "latitude": 44.03, "longitude": -70.1, "timezone": "America/New_York", "population": 9000},
]}


def fake_fetch(url):
    now = wc.local_now()
    if "api.open-meteo.com/v1/forecast" in url:
        return copy.deepcopy(sample_data(now))
    if "air-quality-api" in url:
        return {"current": {"european_aqi": 34, "pm2_5": 8.1, "pm10": 15.3}}
    if "marine-api" in url:
        return sample_sea(now)
    if "meteoalarm" in url:
        return {"warnings": []}
    if "geocoding-api" in url:
        return copy.deepcopy(PLACES)
    raise AssertionError("unexpected request: " + url)


class Screens(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset()
        cls.out = tempfile.mkdtemp(prefix="sonic-clock-screens-")
        with mock.patch.object(wc, "fetch", side_effect=fake_fetch), \
                mock.patch.object(wc, "nm", return_value=(1, "", "nmcli not available")), \
                contextlib.redirect_stdout(io.StringIO()):
            wc.preview_ui(cls.out)
        cls.rendered = sorted(f for f in os.listdir(cls.out) if f.endswith(".png"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out, ignore_errors=True)
        reset()

    def test_every_theme_is_rendered(self):
        for name in wc.THEME_ORDER:
            self.assertIn("clock-%s.png" % name.lower(), self.rendered)

    def test_every_readme_screenshot_is_still_rendered(self):
        docs = os.path.join(ROOT, "docs", "screenshots")
        missing = [f for f in os.listdir(docs) if f.endswith(".png") and f not in self.rendered]
        self.assertEqual(missing, [], "screens in docs/screenshots that preview_ui no longer makes")

    def test_screens_are_full_size_and_not_blank(self):
        for name in self.rendered:
            with Image.open(os.path.join(self.out, name)) as im:
                self.assertEqual(im.size, (wc.W, wc.H), name)
                colours = im.convert("RGB").getcolors(maxcolors=1 << 16)
                self.assertTrue(colours is None or len(colours) > 20, name + " looks blank")

    def test_animation_sheets(self):
        sheets = os.listdir(os.path.join(self.out, "anim"))
        self.assertEqual(sorted(sheets), ["clear.png", "fog.png", "partly.png", "rain.png", "snow.png",
                                          "thunder.png"])


class Taps(unittest.TestCase):
    def setUp(self):
        reset()
        self.now = wc.local_now()
        self.data = sample_data(self.now)
        wc.state.update(data=self.data, updated=self.now)
        self.app = wc.App(None, None)

    def tap(self, x, y):
        for (x0, y0, x1, y1), action in self.app.buttons:
            if x0 <= x < x1 and y0 <= y < y1:
                return action()
        self.fail("nothing to tap at %d,%d" % (x, y))

    def test_forecast_day_opens_its_hours(self):
        self.app.render_clock(self.now, self.data, self.now, None)
        self.tap(wc.M + 10, 500)  # first forecast card: tomorrow
        self.assertEqual((self.app.mode, self.app.day_index), ("day", wc.today_index(self.data, self.now) + 1))
        self.app.render_day()

    def test_offline_without_weather_offers_wifi(self):
        with mock.patch.object(self.app, "open_wifi") as open_wifi:
            self.app.render_clock(self.now, None, None, "offline")
            self.tap(wc.W - wc.M - 10, 280)
        open_wifi.assert_called_once()

    def test_every_settings_page_renders_at_both_rotations(self):
        for rotate in (0, 180):
            wc.SET["rotate"] = rotate
            for page in range(wc.SETTINGS_PAGES):
                self.app.settings_page = page
                self.assertEqual(self.app.render_settings().size, (wc.W, wc.H))
                self.assertTrue(self.app.buttons)


if __name__ == "__main__":
    unittest.main()
