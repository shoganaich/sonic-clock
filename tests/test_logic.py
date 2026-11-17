"""Weather helpers, warnings, time windows, touch mapping and settings."""

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from helpers import fixed_now, reset, sample_data, sample_sea, wc


class Basics(unittest.TestCase):
    def setUp(self):
        reset()

    def test_version_tuple(self):
        self.assertEqual(wc.version_tuple("v1.2.0"), (1, 2, 0))
        self.assertEqual(wc.version_tuple(wc.VERSION), wc.version_tuple("v" + wc.VERSION))
        self.assertGreater(wc.version_tuple("v1.10.0"), wc.version_tuple("v1.9.9"))
        self.assertEqual(wc.version_tuple(None), ())

    def test_compass(self):
        self.assertEqual([wc.compass(d) for d in (0, 22, 23, 90, 180, 270, 315, 359)],
                         ["N", "N", "NE", "E", "S", "W", "NW", "N"])

    def test_uv_and_aqi_levels(self):
        self.assertEqual([wc.uv_level(v) for v in (0, 2.9, 3, 6, 8, 11)],
                         ["Low", "Low", "Moderate", "High", "Very high", "Extreme"])
        self.assertEqual([wc.aqi_level(v) for v in (0, 20, 40, 60, 80, 100)],
                         ["Good", "Fair", "Moderate", "Poor", "Very poor", "Extremely poor"])

    def test_moon_phase(self):
        self.assertEqual(wc.moon_phase(datetime(2024, 4, 8, 18, 21))[2], "New moon")
        self.assertEqual(wc.moon_phase(datetime(2024, 4, 23, 23, 49))[2], "Full moon")
        self.assertEqual(wc.moon_phase(datetime(2024, 4, 15, 19, 13))[2], "First quarter")
        _, illum, _ = wc.moon_phase(datetime(2024, 4, 23, 23, 49))
        self.assertGreater(illum, 0.98)

    def test_wmo_unknown_code(self):
        self.assertEqual(wc.wmo(95), ("Thunderstorm", "thunder"))
        self.assertEqual(wc.wmo(1234), ("Unknown", "cloudy"))

    def test_hhmm(self):
        self.assertEqual(wc.hhmm(0), "00:00")
        self.assertEqual(wc.hhmm(23 * 60 + 30), "23:30")
        self.assertEqual(wc.hhmm(24 * 60 + 30), "00:30")

    def test_every_theme_applies(self):
        self.assertEqual(sorted(wc.THEME_ORDER), sorted(wc.THEMES))
        for name in wc.THEME_ORDER:
            wc.apply_theme(name)
            self.assertEqual(wc.THEME_NAME, name)
        wc.apply_theme("No such theme")
        self.assertEqual(wc.THEME_NAME, "Midnight")

    def test_split_terse(self):
        self.assertEqual(wc.split_terse(r"My\:Net:802-11-wireless"), ["My:Net", "802-11-wireless"])
        self.assertEqual(wc.split_terse(r"*:Back\\slash:70:WPA2"), ["*", "Back\\slash", "70", "WPA2"])
        self.assertEqual(wc.split_terse(""), [""])


class TimeWindows(unittest.TestCase):
    def setUp(self):
        reset()

    def at(self, h, m=0):
        return datetime(2026, 6, 15, h, m)

    def test_screen_off_crossing_midnight(self):
        wc.SET.update(screen_off=True, off_from=23 * 60, off_until=7 * 60)
        self.assertTrue(wc.in_screen_off_window(self.at(23, 30)))
        self.assertTrue(wc.in_screen_off_window(self.at(6, 59)))
        self.assertFalse(wc.in_screen_off_window(self.at(7, 0)))
        self.assertFalse(wc.in_screen_off_window(self.at(12)))

    def test_screen_off_same_day(self):
        wc.SET.update(screen_off=True, off_from=10 * 60, off_until=15 * 60)
        self.assertTrue(wc.in_screen_off_window(self.at(10)))
        self.assertFalse(wc.in_screen_off_window(self.at(15)))
        self.assertFalse(wc.in_screen_off_window(self.at(9, 59)))

    def test_screen_off_disabled_or_empty(self):
        wc.SET.update(screen_off=False, off_from=0, off_until=12 * 60)
        self.assertFalse(wc.in_screen_off_window(self.at(6)))
        wc.SET.update(screen_off=True, off_from=600, off_until=600)
        self.assertFalse(wc.in_screen_off_window(self.at(10)))

    def test_night_window_fixed_hours(self):
        wc.SET["night_hours"] = "22"
        self.assertTrue(wc.in_night_window(self.at(22), None))
        self.assertTrue(wc.in_night_window(self.at(6, 59), None))
        self.assertFalse(wc.in_night_window(self.at(21, 59), None))

    def test_night_window_sun(self):
        data = sample_data(fixed_now())  # sunrise 07:12, sunset 18:41
        self.assertFalse(wc.in_night_window(self.at(12), data))
        self.assertTrue(wc.in_night_window(self.at(19), data))
        self.assertTrue(wc.in_night_window(self.at(7, 0), data))
        self.assertTrue(wc.in_night_window(self.at(21), None))  # no weather yet: 21:00 to 07:00


class Forecast(unittest.TestCase):
    def setUp(self):
        reset()
        self.now = fixed_now()
        self.data = sample_data(self.now)

    def test_calm_forecast_has_no_alerts(self):
        self.assertEqual(wc.forecast_alerts(self.data, self.now), [])

    def test_forecast_alert_levels(self):
        dl = self.data["daily"]
        dl["wind_gusts_10m_max"][0] = 95      # orange
        dl["temperature_2m_min"][1] = -12     # orange, tomorrow
        dl["weather_code"][0] = 95            # yellow thunderstorm
        dl["precipitation_sum"][1] = 120      # red
        alerts = {a["title"]: a["level"] for a in wc.forecast_alerts(self.data, self.now)}
        self.assertEqual(alerts, {"Strong wind": 3, "Severe cold": 3, "Thunderstorms": 2, "Heavy rain": 4})
        levels = [a["level"] for a in wc.forecast_alerts(self.data, self.now)]
        self.assertEqual(levels, sorted(levels, reverse=True))

    def test_active_alerts_prefers_official(self):
        self.data["alerts"] = [{"level": 2, "title": "Yellow Wind", "detail": "", "start": None, "end": None}]
        self.data["alerts_source"] = "MeteoAlarm"
        alerts, source = wc.active_alerts(self.data, self.now)
        self.assertEqual((alerts[0]["title"], source), ("Yellow Wind", "MeteoAlarm"))
        wc.SET["warnings"] = False
        self.assertEqual(wc.active_alerts(self.data, self.now), ([], ""))

    def test_alert_when(self):
        later = self.now + timedelta(hours=3)
        tomorrow = self.now + timedelta(days=1)
        self.assertEqual(wc.alert_when({"start": later, "end": None}, self.now), "from 17:37")
        self.assertEqual(wc.alert_when({"start": None, "end": tomorrow}, self.now),
                         "until " + tomorrow.strftime("%a") + " 14:37")
        self.assertEqual(wc.alert_when({"start": None, "end": None}, self.now), "")

    def test_rain_soon(self):
        self.data["minutely_15"]["precipitation"][2] = 0.4   # 30 minutes from now
        self.assertEqual(wc.rain_soon(self.data, self.now), 30)
        self.data["current"]["precipitation"] = 0.5          # already raining
        self.assertIsNone(wc.rain_soon(self.data, self.now))
        self.data["current"]["precipitation"] = 0
        wc.SET["rain_alerts"] = False
        self.assertIsNone(wc.rain_soon(self.data, self.now))

    def test_tides_alternate_near_the_true_peaks(self):
        start = self.now.replace(hour=0, minute=0)
        found = wc.tides(sample_sea(self.now), start, hours=30)
        kinds = [k for k, _, _ in found]
        self.assertEqual(kinds, ["High", "Low", "High", "Low", "High"])
        first_high = start + timedelta(hours=12.42 / 4)   # sine wave peaks a quarter period in
        self.assertLess(abs((found[0][1] - first_high).total_seconds()), 15 * 60)
        self.assertEqual(wc.tides(None, self.now), [])


class OfficialWarnings(unittest.TestCase):
    def setUp(self):
        reset()

    def utc(self, hours):
        return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    def warning(self, area, level, onset, expires, event="Wind", lang="en-GB"):
        return {"alert": {"info": [{
            "language": lang, "event": event, "description": "Strong gusts.",
            "area": [{"areaDesc": area}],
            "parameter": [{"valueName": "awareness_level", "value": "%d; colour; Moderate" % level}],
            "onset": self.utc(onset), "expires": self.utc(expires)}]}}

    def test_region_matching(self):
        self.assertTrue(wc.area_matches("Setúbal"))
        self.assertTrue(wc.area_matches("SETUBAL"))
        self.assertFalse(wc.area_matches("Faro"))
        self.assertFalse(wc.area_matches(""))
        self.assertEqual(wc.region_words("Distrito de Setúbal"), ["setubal"])

    def test_meteoalarm_feed(self):
        feed = {"warnings": [
            self.warning("Setúbal", 3, -1, 6, event="Orange Wind"),
            self.warning("Faro", 4, -1, 6, event="Red Rain"),              # another region
            self.warning("Setúbal", 1, -1, 6, event="Green"),              # green: no warning
            self.warning("Setúbal", 2, -30, -2, event="Expired"),
            self.warning("Setúbal", 2, 72, 80, event="Too far ahead"),
            self.warning("Setúbal", 2, 10, 20, event="Yellow Coastal"),
        ]}
        with mock.patch.object(wc, "fetch", return_value=feed) as fetch:
            alerts, source = wc.official_alerts()
        self.assertIn("feeds-portugal", fetch.call_args[0][0])
        self.assertEqual(source, "MeteoAlarm")
        self.assertEqual([(a["title"], a["level"]) for a in alerts], [("Orange Wind", 3), ("Yellow Coastal", 2)])
        self.assertIsInstance(alerts[0]["start"], datetime)
        self.assertIsNone(alerts[0]["start"].tzinfo)

    def test_meteoalarm_prefers_english(self):
        w = self.warning("Setúbal", 2, -1, 6, event="Aviso amarelo", lang="pt-PT")
        en = dict(w["alert"]["info"][0], language="en-GB", event="Yellow warning")
        w["alert"]["info"].append(en)
        with mock.patch.object(wc, "fetch", return_value={"warnings": [w]}):
            alerts, _ = wc.official_alerts()
        self.assertEqual([a["title"] for a in alerts], ["Yellow warning"])

    def test_us_weather_service(self):
        wc.SET.update(cc="US", lat=40.0, lon=-75.0, tz="America/New_York")
        feed = {"features": [{"properties": {"event": "Winter Storm Warning", "severity": "Severe",
                                             "headline": "Heavy snow", "onset": self.utc(-1),
                                             "ends": self.utc(12)}}]}
        with mock.patch.object(wc, "fetch", return_value=feed) as fetch:
            alerts, source = wc.official_alerts()
        self.assertIn("point=40.0000,-75.0000", fetch.call_args[0][0])
        self.assertEqual((alerts[0]["title"], alerts[0]["level"], source),
                         ("Winter Storm Warning", 3, "US National Weather Service"))

    def test_no_official_source(self):
        wc.SET["cc"] = "BR"
        with mock.patch.object(wc, "fetch") as fetch:
            self.assertEqual(wc.official_alerts(), (None, ""))
        fetch.assert_not_called()


class Touch(unittest.TestCase):
    def setUp(self):
        reset()
        self.r = (0, 4095)

    def test_corners(self):
        self.assertEqual(wc.to_screen(0, 0, self.r, self.r), (0, 0))
        self.assertEqual(wc.to_screen(4095, 4095, self.r, self.r), (wc.W - 1, wc.H - 1))
        self.assertEqual(wc.to_screen(-50, 9999, self.r, self.r), (0, wc.H - 1))  # clamped

    def test_rotated(self):
        wc.SET["rotate"] = 180
        self.assertEqual(wc.to_screen(0, 0, self.r, self.r), (wc.W - 1, wc.H - 1))

    def test_invert_x(self):
        with mock.patch.object(wc, "TOUCH_INVERT_X", True):
            self.assertEqual(wc.to_screen(0, 0, self.r, self.r), (wc.W - 1, 0))


class Settings(unittest.TestCase):
    def setUp(self):
        reset()

    def saved(self):
        with open(wc.SETTINGS_FILE) as f:
            return json.load(f)

    def test_save_settings_is_atomic(self):
        wc.SET["theme"] = "Nord"
        wc.save_settings()
        self.assertEqual(self.saved()["theme"], "Nord")
        self.assertFalse(os.path.exists(wc.SETTINGS_FILE + ".tmp"))

    def test_load_settings_survives_a_broken_file(self):
        with open(wc.SETTINGS_FILE, "w") as f:
            f.write("{not json")
        self.assertEqual(wc.load_settings(), wc.DEFAULTS)

    def test_web_settings_are_validated(self):
        wc.apply_web_settings({"theme": "Nord", "night_theme": "<script>", "day_bright": "300",
                               "night_bright": "1", "night_mode": "dim", "night_hours": "99",
                               "rotate": "90", "sea": "on", "off_from": "1350", "off_until": "17"})
        s = self.saved()
        self.assertEqual(s["theme"], "Nord")
        self.assertEqual(s["night_theme"], "")
        self.assertEqual((s["day_bright"], s["night_bright"]), (100, 5))
        self.assertEqual((s["night_mode"], s["night_hours"], s["rotate"]), ("dim", "sun", 0))
        self.assertEqual((s["sea"], s["rain_alerts"]), (True, False))  # unchecked boxes are off
        self.assertEqual((s["off_from"], s["off_until"]), (1350, 7 * 60))
        self.assertTrue(wc.refresh_now.is_set())

    def test_web_settings_ignore_bad_numbers(self):
        wc.apply_web_settings({"day_bright": "lots"})
        self.assertEqual(wc.SET["day_bright"], wc.DEFAULTS["day_bright"])

    def test_web_place(self):
        wc.apply_web_place({"lat": "95", "lon": "0", "name": "Nowhere"})
        self.assertEqual(wc.SET["place"], wc.DEFAULTS["place"])
        wc.apply_web_place({"lat": "38.72", "lon": "-9.14", "name": "Lisboa", "country": "Portugal",
                            "tz": "Europe/Lisbon", "cc": "pt", "regions": "Lisboa|Lisbon Municipality"})
        s = self.saved()
        self.assertEqual((s["place"], s["cc"], s["tz"]), ("Lisboa, Portugal", "PT", "Europe/Lisbon"))
        self.assertEqual(s["regions"], ["Lisboa", "Lisbon Municipality"])

    def test_web_place_rejects_bad_timezone_and_country(self):
        wc.apply_web_place({"lat": "0", "lon": "0", "name": "X", "tz": "../../etc/passwd", "cc": "P1"})
        self.assertEqual((wc.SET["tz"], wc.SET["cc"]), ("auto", ""))


if __name__ == "__main__":
    unittest.main()
