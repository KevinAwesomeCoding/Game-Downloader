"""Read-only readiness parser and subprocess-boundary tests."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from readiness import READINESS_COMMANDS, ReadinessError, _parse_bottles, _parse_engines, _parse_home, runtime_readiness


ENGINE_ID = "x64-sikarugir10.0_6-r28"
ENGINE_OK = f"{ENGINE_ID}\t9.0.0\trenderers: wined3d,dxmt,d3dmetal"
BOTTLE_OK = f"Games\tengine={ENGINE_ID}\trenderer=d3dmetal\tpins=7\trecipes=steam,vcrun2022"


class ReadinessTests(unittest.TestCase):
    def test_parse_home_with_spaces_and_unavailable_configured_home(self):
        home, warning = _parse_home(
            "home: /Users/kevin/My Highball (default)\n"
            "chosen location not available right now: /Volumes/Games Folder; running on the default meanwhile\n"
        )
        self.assertEqual(home, "/Users/kevin/My Highball")
        self.assertEqual(warning, "Configured Highball home is unavailable: /Volumes/Games Folder")

    def test_parse_engines_tracks_incomplete_and_unlicensed_renderers(self):
        output = (
            ENGINE_OK + "\n"
            "old-engine\t?\trenderers: wined3d,d3dmetal (licence not accepted)\tINCOMPLETE, missing: wine, wineserver\n"
        )
        engines = _parse_engines(output)
        self.assertTrue(engines[ENGINE_ID]["complete"])
        self.assertTrue(engines[ENGINE_ID]["renderers"]["d3dmetal"]["available"])
        self.assertFalse(engines["old-engine"]["complete"])
        self.assertFalse(engines["old-engine"]["renderers"]["d3dmetal"]["license_accepted"])

    def test_parse_bottle_ready_and_failure_reasons(self):
        engines = _parse_engines(
            ENGINE_OK + "\n" +
            "incomplete\t?\trenderers: wined3d,d3dmetal\tINCOMPLETE, missing: wine\n" +
            "unlicensed\t9.0.0\trenderers: wined3d,d3dmetal (license not accepted)\n"
        )
        output = "\n".join([
            BOTTLE_OK,
            f"Broken\tengine=missing\trenderer=wined3d\tpins=0\trecipes=",
            f"Partial\tengine=incomplete\trenderer=wined3d\tpins=0\trecipes=",
            f"Needs GPTK\tengine=unlicensed\trenderer=d3dmetal\tpins=0\trecipes=",
            "Damaged\tDAMAGED — invalid settings (delete it to free the name)",
        ])
        bottles, damaged = _parse_bottles(output, engines)
        self.assertTrue(bottles[0]["ready"])
        self.assertEqual(bottles[0]["pins"], 7)
        self.assertIn("assigned engine is not installed", bottles[1]["issues"])
        self.assertIn("assigned engine is incomplete", bottles[2]["issues"])
        self.assertIn("needs its license accepted", bottles[3]["issues"][0])
        self.assertEqual(damaged[0]["name"], "Damaged")

    def test_runtime_readiness_uses_only_readiness_commands(self):
        helper = Path("/tmp/highball")
        outputs = [
            "home: /Users/kevin/Library/Application Support/Highball (default)\n",
            ENGINE_OK + "\n",
            BOTTLE_OK + "\n",
        ]
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, outputs[len(calls) - 1], "")

        with patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True):
            report = runtime_readiness(helper, runner=runner)
        self.assertEqual(report["status"], "ready")
        self.assertTrue(report["mutations_performed"] is False)
        self.assertTrue(report["engine_list_runs_wine_version"])
        self.assertEqual(tuple(tuple(call[0][1:]) for call in calls), READINESS_COMMANDS)
        self.assertFalse(any(call[1]["shell"] for call in calls))
        self.assertEqual(calls[0][1]["env"].get("HIGHBALL_HOME"), None)
        self.assertEqual(calls[1][1]["env"]["HIGHBALL_HOME"], report["home"])
        self.assertTrue(all(call[1]["timeout"] == 20 for call in calls))

    def test_missing_engine_and_damaged_bottle_status(self):
        outputs = ["home: /tmp/HB (default)", "no engines installed — try install", "Damaged\tDAMAGED — bad"]
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, outputs.pop(0), "")
        report = runtime_readiness(Path("/tmp/highball"), runner=runner)
        self.assertEqual(report["status"], "no_engines")
        self.assertEqual(report["damaged_bottles"][0]["name"], "Damaged")

    def test_unrecognized_output_and_command_failure_fail_closed(self):
        with self.assertRaises(ReadinessError):
            _parse_engines("some future output format")
        responses = [
            subprocess.CompletedProcess([], 0, "home: /tmp/HB (default)", ""),
            subprocess.CompletedProcess([], 1, "", "permission denied"),
        ]
        def failing_runner(command, **kwargs):
            return responses.pop(0)
        with self.assertRaisesRegex(ReadinessError, "permission denied"):
            runtime_readiness(Path("/tmp/highball"), runner=failing_runner)

    def test_timeout_fails_closed(self):
        calls = 0
        def runner(command, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return subprocess.CompletedProcess(command, 0, "home: /tmp/HB (default)", "")
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        with self.assertRaisesRegex(ReadinessError, "engine list failed"):
            runtime_readiness(Path("/tmp/highball"), runner=runner)


if __name__ == "__main__":
    unittest.main()
