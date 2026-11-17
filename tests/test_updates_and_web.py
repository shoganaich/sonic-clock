"""The GitHub Release installer and the phone settings page."""

import http.client
import io
import json
import os
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from unittest import mock

from helpers import reset, wc


def release_tarball(files):
    """A GitHub source tarball: every path sits under one top folder."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class InstallRelease(unittest.TestCase):
    def setUp(self):
        reset()
        self.dir = tempfile.mkdtemp(prefix="sonic-clock-update-")
        self.app = os.path.join(self.dir, "weatherclock.py")
        with open(self.app, "wb") as f:
            f.write(b'VERSION = "1.0.0"\n')

    def install(self, files, check=None):
        raw = release_tarball(files)
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(raw)) as urlopen, \
                mock.patch.object(wc, "__file__", self.app), \
                mock.patch.object(wc, "check_release", side_effect=check) as checked, \
                mock.patch.object(wc.subprocess, "run") as run:
            try:
                wc.install_release("v1.3.0")
            finally:
                self.url = urlopen.call_args[0][0].full_url
                self.checked = checked.call_args_list
                self.ran = run.call_args_list

    def marker(self):
        with open(wc.UPDATE_MARK) as f:
            return json.load(f)

    def current(self):
        with open(self.app, "rb") as f:
            return f.read()

    def test_installs_the_app_and_keeps_a_backup(self):
        self.install({"sonic-clock-1.3.0/weatherclock/weatherclock.py": b'VERSION = "1.3.0"\n',
                      "sonic-clock-1.3.0/README.md": b"docs"})
        self.assertEqual(self.url, "https://codeload.github.com/%s/tar.gz/refs/tags/v1.3.0" % wc.UPDATE_REPO)
        self.assertEqual(self.current(), b'VERSION = "1.3.0"\n')
        with open(self.app + ".bak", "rb") as f:
            self.assertEqual(f.read(), b'VERSION = "1.0.0"\n')
        self.assertTrue(os.access(self.app, os.X_OK))
        self.assertEqual(self.ran, [])  # no service file in the release: systemd untouched
        self.assertEqual(self.marker(), {"version": "v1.3.0", "starts": 0})
        self.assertEqual(len(self.checked), 1)
        self.assertEqual(os.listdir(self.dir), ["weatherclock.py", "weatherclock.py.bak"])  # check file gone

    def test_refuses_code_that_does_not_compile(self):
        with self.assertRaises(SyntaxError):
            self.install({"sonic-clock-1.3.0/weatherclock/weatherclock.py": b"def broken(:\n"})
        self.assertEqual(self.current(), b'VERSION = "1.0.0"\n')
        self.assertFalse(os.path.exists(self.app + ".bak"))

    def test_refuses_code_that_fails_its_start_up_check(self):
        with self.assertRaises(RuntimeError):
            self.install({"sonic-clock-1.3.0/weatherclock/weatherclock.py": b"x = 1\n"},
                         check=RuntimeError("the new version fails to start"))
        self.assertEqual(self.current(), b'VERSION = "1.0.0"\n')
        self.assertEqual(os.listdir(self.dir), ["weatherclock.py"])
        self.assertFalse(os.path.exists(wc.UPDATE_MARK))

    def test_refuses_a_release_without_the_app(self):
        with self.assertRaises(RuntimeError):
            self.install({"sonic-clock-1.3.0/README.md": b"docs"})
        self.assertEqual(self.current(), b'VERSION = "1.0.0"\n')

    def test_ignores_files_outside_the_app_folder(self):
        with self.assertRaises(RuntimeError):
            self.install({"sonic-clock-1.3.0/weatherclock.py": b"x = 1\n",
                          "sonic-clock-1.3.0/other/weatherclock/weatherclock.py": b"x = 2\n"})
        self.assertEqual(self.current(), b'VERSION = "1.0.0"\n')


class CheckRelease(unittest.TestCase):
    """check_release() runs a real Python, as it does on the Pad."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sonic-clock-check-")

    def candidate(self, code):
        path = os.path.join(self.dir, "weatherclock-check.py")
        with open(path, "w") as f:
            f.write(code)
        return path

    def test_this_version_passes(self):
        with open(wc.__file__) as f:
            wc.check_release(self.candidate(f.read()))

    def test_error_on_import_is_caught(self):
        with self.assertRaisesRegex(RuntimeError, "NameError"):
            wc.check_release(self.candidate("VERSION = '9.9.9'\nprint(missing_name)\n"))

    def test_error_while_drawing_is_caught(self):
        with self.assertRaisesRegex(RuntimeError, "ZeroDivisionError"):
            wc.check_release(self.candidate("def self_test():\n    1 / 0\n"))

    def test_a_version_without_self_test_only_needs_to_load(self):
        wc.check_release(self.candidate("VERSION = '9.9.9'\n"))


class Rollback(unittest.TestCase):
    def setUp(self):
        reset()
        self.dir = tempfile.mkdtemp(prefix="sonic-clock-rollback-")
        self.app = os.path.join(self.dir, "weatherclock.py")
        for path, text in ((self.app, b"new"), (self.app + ".bak", b"old")):
            with open(path, "wb") as f:
                f.write(text)

    def start(self):
        with mock.patch.object(wc, "__file__", self.app), mock.patch.object(wc.os, "execv") as execv:
            on_trial = wc.guard_update()
        with open(self.app, "rb") as f:
            return on_trial, f.read(), execv.call_args

    def mark(self, starts):
        with open(wc.UPDATE_MARK, "w") as f:
            json.dump({"version": "v1.3.0", "starts": starts}, f)

    def test_no_update_pending(self):
        self.assertEqual(self.start(), (False, b"new", None))

    def test_the_first_starts_are_on_trial(self):
        self.mark(0)
        for _ in range(wc.UPDATE_TRIES):
            self.assertEqual(self.start(), (True, b"new", None))
        with open(wc.UPDATE_MARK) as f:
            self.assertEqual(json.load(f)["starts"], wc.UPDATE_TRIES)

    def test_goes_back_after_too_many_starts(self):
        self.mark(wc.UPDATE_TRIES)
        on_trial, code, execv = self.start()
        self.assertEqual(code, b"old")
        self.assertFalse(os.path.exists(wc.UPDATE_MARK))
        self.assertEqual(execv[0][1][-1], self.app)  # restarted on the old version

    def test_keeps_the_new_version_without_a_backup(self):
        os.remove(self.app + ".bak")
        self.mark(wc.UPDATE_TRIES)
        self.assertEqual(self.start(), (False, b"new", None))
        self.assertFalse(os.path.exists(wc.UPDATE_MARK))

    def test_confirm_update(self):
        self.mark(1)
        wc.confirm_update()
        self.assertFalse(os.path.exists(wc.UPDATE_MARK))
        wc.confirm_update()  # nothing pending: no error

    def test_run_loop_confirms_after_the_trial(self):
        self.mark(1)
        app = wc.App(None, mock.Mock())
        app.trial_until = time.monotonic() - 1
        app.show = mock.Mock()
        with mock.patch.object(wc.events, "get", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                app.run()
        self.assertFalse(os.path.exists(wc.UPDATE_MARK))
        self.assertIsNone(app.trial_until)


class PhonePage(unittest.TestCase):
    def setUp(self):
        reset()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), wc.WebHandler)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        wc.WEB.update(pin="4821", tokens=set(), fails={})

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def request(self, method, path, form=None, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
        headers = {"Cookie": cookie} if cookie else {}
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body=body, headers=headers)
        r = conn.getresponse()
        result = r.status, dict(r.getheaders()), r.read().decode()
        conn.close()
        return result

    def login(self, pin="4821"):
        return self.request("POST", "/login", {"pin": pin})

    def test_needs_the_pin(self):
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("PIN shown on the clock", body)
        status, headers, _ = self.request("POST", "/save", {"theme": "Nord"})
        self.assertEqual((status, headers["Location"]), (303, "/"))
        self.assertEqual(wc.SET["theme"], "Midnight")

    def test_login_and_save(self):
        status, headers, _ = self.login()
        self.assertEqual(status, 303)
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        session = cookie.split(";")[0]
        status, _, body = self.request("GET", "/", cookie=session)
        self.assertIn("<select name=theme>", body)
        status, headers, _ = self.request("POST", "/save", {"theme": "Nord", "day_bright": "80"}, cookie=session)
        self.assertEqual((status, headers["Location"]), (303, "/?saved=1"))
        self.assertEqual((wc.SET["theme"], wc.SET["day_bright"]), ("Nord", 80))
        self.assertEqual(wc.events.get_nowait()[0], "web")

    def test_made_up_cookie_is_refused(self):
        status, _, body = self.request("GET", "/", cookie="sc=guess")
        self.assertIn("PIN shown on the clock", body)

    def test_locks_after_five_wrong_pins(self):
        for _ in range(5):
            self.assertIn("Wrong PIN.", self.login("0000")[2])
        status, _, body = self.login()
        self.assertEqual(status, 200)
        self.assertIn("Too many wrong PINs", body)
        self.assertEqual(wc.WEB["tokens"], set())

    def test_errors_are_escaped(self):
        self.assertNotIn("<b>", wc.login_html("<b>"))

    def test_rejects_outside_addresses(self):
        handler = wc.WebHandler.__new__(wc.WebHandler)
        handler.client_address = ("8.8.8.8", 1234)
        handler.request_version, handler.requestline = "HTTP/1.1", "GET / HTTP/1.1"
        handler.wfile = io.BytesIO()
        handler._headers_buffer = []
        self.assertFalse(handler.from_home())
        self.assertIn(b"403", handler.wfile.getvalue())
        handler.client_address = ("192.168.1.20", 1234)
        self.assertTrue(handler.from_home())


if __name__ == "__main__":
    unittest.main()
