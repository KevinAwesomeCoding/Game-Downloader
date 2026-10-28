"""Exercise the real Highball CLI against disposable metadata; never start Wine.

Set HIGHBALL_STEAM_TEST_CLI to a CLI built with macos/highball-steam.patch or
from the nested Highball repository. Dispatch itself is captured, not executed.
"""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from highball import Highball


@unittest.skipUnless(os.environ.get("HIGHBALL_STEAM_TEST_CLI"), "set HIGHBALL_STEAM_TEST_CLI to the built CLI")
class SteamIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="steam integration ")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.bottle = self.home / "bottles/Selected Games"
        self.drive = self.bottle / "drive_c"
        self.drive.mkdir(parents=True)
        self.settings = {"formatVersion": 3, "name": "Selected Games", "engineID": "unused",
                         "pins": [{"name": "Steam", "path": "Program Files (x86)/Steam/steam.exe"}]}
        self.save()
        self.cli = str(Path(os.environ["HIGHBALL_STEAM_TEST_CLI"]).resolve())
        self.launcher = Mock(return_value=Mock(pid=123, wait=Mock(return_value=0)))
        self.client = Highball(helper=self.cli, inspector=lambda: {
            "readiness": {"home": str(self.home), "bottles": [{"name": "Selected Games", "ready": True}]}
        }, launcher=self.launcher, launch_directory=self.home / "logs")

    def save(self):
        (self.bottle / "bottle.json").write_text(json.dumps(self.settings))

    def executable(self, relative):
        path = self.drive / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        header = bytearray(128)
        header[:2] = b"MZ"
        header[0x3c] = 0x40
        header[0x40:0x44] = b"PE\0\0"
        path.write_bytes(header)
        return path

    def check(self):
        result = subprocess.run([self.cli, "steam", "launch", "Selected Games", "--uri", "steam://open/main", "--check"],
                                env=dict(os.environ, HIGHBALL_HOME=str(self.home)), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_both_locations_and_case_are_discovered_without_changing_pins(self):
        before = (self.bottle / "bottle.json").read_bytes()
        for relative in ["Program Files/Steam/Steam.exe", "Program Files (x86)/Steam/steam.exe"]:
            with self.subTest(path=relative):
                path = self.executable(relative)
                request = self.check()
                self.assertEqual(Path(request["executable"]).resolve(), path.resolve())
                self.assertEqual(request["arguments"], ["steam://open/main"])
                self.assertEqual(request["bottle"], "Selected Games")
                path.unlink()
        self.assertEqual((self.bottle / "bottle.json").read_bytes(), before)

    def test_saved_override_is_shared_by_highball_and_game_installer(self):
        path = self.executable("Chosen/Steam.exe")
        self.executable("Program Files/Steam/Steam.exe")
        self.settings["steamExecutableOverride"] = "Chosen/Steam.exe"
        self.save()
        for _ in range(2):  # Separate CLI processes must load the persisted choice.
            self.assertEqual(Path(self.check()["executable"]).resolve(), path.resolve())
        messages = []
        result = self.client.steam_install("Selected Games", "steam://open/main", messages.append)
        self.assertEqual(Path(result["steamExecutable"]).resolve(), path.resolve())
        self.assertIn(str(path), messages[-1])
        self.assertEqual(self.launcher.call_args.args[0], [self.cli, "steam", "launch", "Selected Games", "--uri", "steam://open/main"])

    def test_missing_and_invalid_explicit_paths_fail_before_dispatch(self):
        with self.assertRaisesRegex(ValueError, "Attempted paths:.*Program Files.*Advanced > Steam executable"):
            self.client.steam_install("Selected Games", "steam://open/main")
        self.executable("Program Files/Steam/Steam.exe")
        self.settings["steamExecutableOverride"] = "Missing/Steam.exe"
        self.save()
        with self.assertRaisesRegex(ValueError, "Missing/Steam.exe"):
            self.client.steam_install("Selected Games", "steam://open/main")
        invalid = self.drive / "Missing/Steam.exe"
        invalid.parent.mkdir()
        invalid.write_text("not a Windows executable")
        with self.assertRaisesRegex(ValueError, "not a Windows Steam.exe"):
            self.client.steam_install("Selected Games", "steam://open/main")
        self.launcher.assert_not_called()

    def test_other_bottle_cannot_inherit_the_selected_bottles_override(self):
        self.executable("Custom/Steam.exe")
        self.settings["steamExecutableOverride"] = "Custom/Steam.exe"
        self.save()
        other = self.home / "bottles/Other"
        other.mkdir()
        (other / "bottle.json").write_text(json.dumps({"formatVersion": 3, "name": "Other", "engineID": "unused"}))
        result = subprocess.run([self.cli, "steam", "resolve", "Other"], env=dict(os.environ, HIGHBALL_HOME=str(self.home)), capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(other), result.stderr)

    def test_only_exact_steam_installation_uses_official_app_launch_route(self):
        steam = self.executable("Program Files/Steam/Steam.exe").parent
        game = steam / "steamapps/common/Owned Game"
        game.mkdir(parents=True)
        (steam / "steamapps/appmanifest_480.acf").write_text(
            r'"AppState" { "appid" "480" "name" "Owned Game" "StateFlags" "4" "installdir" "Owned Game" "LauncherPath" "C:\\Program Files\\Steam\\Steam.exe" }')
        result = self.client.launch_steam_game({"installPath": str(game)}, "480", "Selected Games")
        self.assertTrue(result["steamGame"])
        self.assertEqual(self.launcher.call_args.args[0], [self.cli, "steam", "game", "Selected Games", "480", "--install-path", str(game)])
        # Play reaches this route before asking for a local game pin or changing its receipt.
        from worker import main
        job_file = self.home / "steam-game.json"
        cache = self.home / "steam-game"
        cache.mkdir()
        receipt = cache / "receipt.json"
        receipt.write_text(json.dumps({"installPath": str(game)}))
        before = receipt.read_bytes()
        job_file.write_text(json.dumps({"id": "steam-game", "game": {"steamAppId": "480"}}))
        events = []
        with patch("worker.Highball", return_value=self.client), patch("worker.emit", side_effect=lambda *a, **kw: events.append((a, kw))), \
                patch.object(sys, "argv", ["worker", "launch", "--job", str(job_file), "--bottle", "Selected Games"]), \
                patch.object(sys, "stdin", io.StringIO("")):
            self.assertEqual(main(), 0)
        self.assertEqual(events[-1][0][0], "launchRequested")
        self.assertTrue(events[-1][1]["steamGame"])
        self.assertEqual(receipt.read_bytes(), before)
        self.launcher.reset_mock()
        self.assertIsNone(self.client.launch_steam_game({"installPath": str(self.home / "local copy")}, "480", "Selected Games"))
        self.launcher.assert_not_called()
