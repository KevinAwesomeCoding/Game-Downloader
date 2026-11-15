"""No installed Highball, Wine, network, or real bottles are used by these tests."""

import hashlib
import contextlib
import io
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from probe import HELP_CHECKS, REVIEWED_COMMIT, REVIEWED_VERSION, detect_app, main, probe


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.app = self.root / "Custom Apps/Highball.app"
        self.gui = self.app / "Contents/MacOS/Highball"
        self.gui.parent.mkdir(parents=True)
        self.gui.write_bytes(b"GUI fixture must never execute")
        self.info = {"CFBundleIdentifier": "app.highball.Highball",
                     "CFBundleShortVersionString": REVIEWED_VERSION,
                     "CFBundleExecutable": "Highball"}
        self.write_info()
        self.helper = self.root / "Helper With Spaces/highball"
        self.helper.parent.mkdir()
        self.helper.write_bytes(b"helper fixture must never execute")
        self.helper.chmod(0o700)
        self.metadata = {"schema_version": 1, "highball_version": REVIEWED_VERSION,
                         "source_commit": REVIEWED_COMMIT, "steam_integration": "steam-path-v1",
                         "binary_sha256": hashlib.sha256(self.helper.read_bytes()).hexdigest()}
        self.manifest = self.root / "helper-build.json"
        self.write_manifest()
        self.runner = Mock(side_effect=self.help_response)

    def write_info(self):
        with (self.app / "Contents/Info.plist").open("wb") as stream:
            plistlib.dump(self.info, stream)

    def write_manifest(self):
        self.manifest.write_text(json.dumps(self.metadata), encoding="utf-8")

    @staticmethod
    def help_response(command, **kwargs):
        arguments = tuple(command[1:])
        for expected, tokens in HELP_CHECKS.values():
            if arguments == expected:
                return subprocess.CompletedProcess(command, 0, "USAGE: highball " + "\n".join(tokens), "")
        raise AssertionError(f"Non-help or unexpected command: {command}")

    def check(self, **overrides):
        options = dict(apps=[self.app], helper=self.helper, manifest=self.manifest, runner=self.runner)
        options.update(overrides)
        return probe(**options)

    def assert_blocked(self, status, **overrides):
        report = self.check(**overrides)
        self.assertEqual(report["status"], status)
        self.assertFalse(report["helper_ready"])
        self.runner.assert_not_called()

    def test_missing_app(self):
        self.assert_blocked("app_missing", apps=[self.root / "absent.app"])

    def test_invalid_app(self):
        self.info["CFBundleIdentifier"] = "another.app"
        self.write_info()
        self.assert_blocked("app_invalid")

    def test_corrupt_plist(self):
        for invalid in [b"not a plist", b"<plist><dict>"]:
            (self.app / "Contents/Info.plist").write_bytes(invalid)
            self.assert_blocked("app_invalid")

    def test_missing_bundle_executable(self):
        self.gui.unlink()
        self.assert_blocked("app_invalid")

    def test_custom_app_location(self):
        self.assertEqual(detect_app([self.root / "missing", self.app])["version"], REVIEWED_VERSION)

    def test_reviewed_newer_release_checks_capabilities(self):
        self.info["CFBundleShortVersionString"] = "0.10.26"
        self.metadata.update(highball_version="0.10.26", source_commit="dbc36ebc67f75944fce0dd7994b060dcd68a67d1")
        self.write_info()
        self.write_manifest()
        self.assertTrue(self.check()["helper_ready"])

    def test_unknown_future_release_is_not_trusted(self):
        self.metadata.update(highball_version="0.10.99", source_commit="unknown")
        self.write_manifest()
        self.assert_blocked("unreviewed_build")

    def test_missing_helper_and_no_path_fallback(self):
        self.assert_blocked("helper_missing", helper=None)

    def test_nonexistent_helper(self):
        self.assert_blocked("helper_missing", helper=self.root / "missing")

    def test_symlink_loop_helper(self):
        link = self.root / "loop"
        link.symlink_to(link)
        self.assert_blocked("helper_invalid", helper=link)

    def test_gui_is_never_executed_even_through_symlink(self):
        link = self.root / "looks-like-cli"
        link.symlink_to(self.gui)
        self.assert_blocked("gui_rejected", helper=link)

    def test_non_executable_helper(self):
        self.helper.chmod(0o600)
        self.assert_blocked("helper_not_executable")

    def test_no_version_is_invented_without_manifest(self):
        self.assert_blocked("provenance_required", manifest=None)

    def test_malformed_or_missing_manifest(self):
        self.manifest.write_text("[]")
        self.assert_blocked("invalid_manifest")
        self.manifest.write_text("{")
        self.assert_blocked("invalid_manifest")
        self.manifest.unlink()
        self.assert_blocked("invalid_manifest")

    def test_unreviewed_commit(self):
        self.metadata["source_commit"] = "0" * 40
        self.write_manifest()
        self.assert_blocked("unreviewed_build")

    def test_old_helper_without_steam_path_integration_is_rejected(self):
        del self.metadata["steam_integration"]
        self.write_manifest()
        self.assert_blocked("unreviewed_build")

    def test_unreviewed_release(self):
        self.metadata["highball_version"] = "0.10.23"
        self.write_manifest()
        self.assert_blocked("unreviewed_build")

    def test_app_helper_version_mismatch(self):
        self.info["CFBundleShortVersionString"] = "0.10.23"
        self.write_info()
        self.assert_blocked("version_mismatch")

    def test_modified_binary(self):
        self.helper.write_bytes(b"different bytes")
        self.assert_blocked("hash_mismatch")

    def test_help_success_is_only_interface_readiness(self):
        before = (self.app / "Contents/Info.plist").read_bytes()
        report = self.check()
        self.assertTrue(report["helper_ready"])
        self.assertEqual(report["status"], "helper_ready")
        self.assertEqual(len(self.runner.call_args_list), len(HELP_CHECKS))
        temporary_homes = set()
        for call in self.runner.call_args_list:
            command = call.args[0]
            self.assertEqual(command[0], str(self.helper.resolve()))
            self.assertEqual(command[-1], "--help")
            self.assertFalse(call.kwargs["shell"])
            self.assertEqual(call.kwargs["timeout"], 5)
            temporary_homes.add(call.kwargs["env"]["HIGHBALL_HOME"])
            self.assertTrue(call.kwargs["env"]["HIGHBALL_HOME"].startswith(call.kwargs["cwd"]))
        self.assertTrue(all(not Path(path).exists() for path in temporary_homes))
        self.assertEqual((self.app / "Contents/Info.plist").read_bytes(), before)
        self.assertIn("not checked", report["scope"])

    def test_missing_capability(self):
        self.runner.side_effect = None
        self.runner.return_value = subprocess.CompletedProcess([], 0, "USAGE: unrelated tool", "")
        self.assertEqual(self.check()["status"], "capability_missing")

    def test_timeout_os_error_and_nonzero_exit(self):
        for failure in [subprocess.TimeoutExpired("helper", 5), OSError("bad architecture")]:
            with self.subTest(failure=failure):
                self.runner.side_effect = failure
                self.assertEqual(self.check()["status"], "probe_failed")
        self.runner.side_effect = None
        self.runner.return_value = subprocess.CompletedProcess([], 1, "", "dyld failure")
        report = self.check()
        self.assertEqual(report["status"], "probe_failed")
        self.assertIn("dyld failure", report["detail"])

    def test_entrypoint_reports_missing_helper_as_json_and_exit_two(self):
        output = io.StringIO()
        with patch("probe.sys.platform", "darwin"), contextlib.redirect_stdout(output):
            status = main(["--app", str(self.app)])
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "helper_missing")

    def test_entrypoint_does_not_execute_helpers_off_mac(self):
        output = io.StringIO()
        with patch("probe.sys.platform", "win32"), contextlib.redirect_stdout(output):
            status = main(["--helper", str(self.helper)])
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "unsupported_platform")


if __name__ == "__main__":
    unittest.main()
