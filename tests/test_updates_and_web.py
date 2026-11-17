"""The GitHub Release installer and the phone settings page."""

import http.client
import io
import os
import tarfile
import tempfile
import threading
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

    def install(self, files):
        raw = release_tarball(files)
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(raw)) as urlopen, \
                mock.patch.object(wc, "__file__", self.app), \
                mock.patch.object(wc.subprocess, "run") as run:
            try:
                wc.install_release("v1.3.0")
            finally:
                self.url = urlopen.call_args[0][0].full_url
                self.ran = run.call_args_list

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

    def test_refuses_code_that_does_not_compile(self):
        with self.assertRaises(SyntaxError):
            self.install({"sonic-clock-1.3.0/weatherclock/weatherclock.py": b"def broken(:\n"})
        self.assertEqual(self.current(), b'VERSION = "1.0.0"\n')
        self.assertFalse(os.path.exists(self.app + ".bak"))

    def test_refuses_a_release_without_the_app(self):
        with self.assertRaises(RuntimeError):
            self.install({"sonic-clock-1.3.0/README.md": b"docs"})
        self.assertEqual(self.current(), b'VERSION = "1.0.0"\n')

    def test_ignores_files_outside_the_app_folder(self):
        with self.assertRaises(RuntimeError):
            self.install({"sonic-clock-1.3.0/weatherclock.py": b"x = 1\n",
                          "sonic-clock-1.3.0/other/weatherclock/weatherclock.py": b"x = 2\n"})
        self.assertEqual(self.current(), b'VERSION = "1.0.0"\n')


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
