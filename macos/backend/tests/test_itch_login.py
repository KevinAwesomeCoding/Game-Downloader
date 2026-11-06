import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit, urlencode
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from itch_login import sign_in


class ItchLoginTests(unittest.TestCase):
    def test_copyable_link_survives_browser_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "user/AppData/LocalLow/Innersloth/Among Us/itch"
            destination.parents[4].mkdir()
            links = []
            def receive_link(url):
                links.append(url)
                params = parse_qs(urlsplit(url).query)
                callback = params["redirect_uri"][0] + "?" + urlencode({"state": params["state"][0], "code": "test-code"})
                threading.Thread(target=lambda: urlopen(callback).read(), daemon=True).start()
            def exchange(request, timeout):
                return io.BytesIO(json.dumps({"access_token": "fixture-secret"}).encode())
            sign_in("fixture-client", destination, opener=lambda _: False,
                    authorization_url=receive_link, exchange=exchange)
            self.assertEqual(len(links), 1)
            self.assertEqual(destination.read_text(), "fixture-secret")

    def test_clean_login_callback_and_token_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "user/AppData/LocalLow/Innersloth/Among Us/itch"
            destination.parents[4].mkdir()
            def open_browser(url):
                params = parse_qs(urlsplit(url).query)
                self.assertEqual(params["code_challenge_method"], ["S256"])
                callback = params["redirect_uri"][0] + "?" + urlencode({"state": params["state"][0], "code": "test-code"})
                threading.Thread(target=lambda: urlopen(callback).read(), daemon=True).start()
                return True
            def exchange(request, timeout):
                form = parse_qs(request.data.decode())
                self.assertEqual(form["code"], ["test-code"])
                self.assertEqual(request.full_url, "https://api.itch.io/oauth/token")
                return io.BytesIO(json.dumps({"access_token": "fixture-secret"}).encode())
            sign_in("fixture-client", destination, opener=open_browser, exchange=exchange)
            self.assertEqual(destination.read_text(), "fixture-secret")
            if os.name != "nt":
                self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(ValueError, "already exists"):
                sign_in("fixture-client", destination, opener=lambda _: self.fail("browser opened"))
            self.assertEqual(destination.read_text(), "fixture-secret")

    def test_missing_client_never_opens_browser(self):
        with self.assertRaisesRegex(ValueError, "client ID"):
            sign_in("", "/tmp/unused-itch-test", opener=lambda _: self.fail("browser opened"))
