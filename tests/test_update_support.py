import hashlib
import os
import tempfile
import unittest

from update_support import (
    UpdateNoticeState,
    compatible_release,
    download_verified_file,
    schedule_background_check,
    windows_update_script,
)
from search_support import matches_game_search


class Response:
    def __init__(self, data, status=200):
        self.data = data
        self.status = status
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    def iter_content(self, chunk_size):
        for start in range(0, len(self.data), chunk_size):
            yield self.data[start:start + chunk_size]


class UpdateFlowTests(unittest.TestCase):
    def setUp(self):
        self.payload = b"verified Windows x64 release payload"
        self.release = {
            "tag_name": "v1.4-20261027-22",
            "html_url": "https://github.com/example/project/releases/tag/v1.4-20261027-22",
            "body": "Release notes",
            "assets": [{
                "name": "GameInstaller-windows-x64.exe",
                "browser_download_url": "https://example.invalid/GameInstaller-windows-x64.exe",
                "digest": "sha256:" + hashlib.sha256(self.payload).hexdigest(),
            }],
        }

    def test_no_update_or_incompatible_target(self):
        self.assertIsNone(compatible_release(self.release, "v1.4-20261027-22", "Windows", "AMD64"))
        self.assertIsNotNone(compatible_release(self.release, "v1.4-20261026-21", "Windows", "AMD64"))
        self.assertIsNone(compatible_release(self.release, "v1.3", "Darwin", "arm64"))
        self.assertIsNone(compatible_release(self.release, "v1.3", "Windows", "ARM64"))

    def test_newer_compatible_update_found(self):
        update = compatible_release(self.release, "v1.3", "Windows", "AMD64")
        self.assertIsNotNone(update)
        self.assertEqual(update.tag, "v1.4-20261027-22")
        self.assertEqual(update.notes, "Release notes")
        self.assertEqual(update.sha256, self.release["assets"][0]["digest"].removeprefix("sha256:"))
        self.assertTrue(update.self_updatable)

    def test_missing_asset_checksum_or_source_build_use_manual_release_page(self):
        no_digest = dict(self.release, assets=[dict(self.release["assets"][0], digest=None)])
        update = compatible_release(no_digest, "v1.3", "Windows", "AMD64")
        self.assertFalse(update.self_updatable)
        self.assertIn("checksum", update.manual_reason)
        source_run = compatible_release(self.release, "v1.3", "Windows", "AMD64", packaged=False)
        self.assertFalse(source_run.self_updatable)
        no_asset = dict(self.release, assets=[])
        manual = compatible_release(no_asset, "v1.3", "Windows", "AMD64")
        self.assertFalse(manual.self_updatable)
        self.assertTrue(manual.release_url)

    def test_previous_windows_asset_name_remains_compatible(self):
        legacy = dict(self.release, assets=[dict(
            self.release["assets"][0], name="GameInstaller.exe"
        )])
        update = compatible_release(legacy, "v1.3", "Windows", "AMD64")
        self.assertTrue(update.self_updatable)

    def test_update_notice_later_preserves_indicator_and_reopens(self):
        update = compatible_release(self.release, "v1.3", "Windows", "AMD64")
        state = UpdateNoticeState()
        self.assertFalse(state.indicator_visible)
        state.offer(update)
        self.assertTrue(state.indicator_visible)
        self.assertIs(state.open_details(), update)
        state.later()
        self.assertFalse(state.details_open)
        self.assertTrue(state.indicator_visible)
        self.assertIs(state.open_details(), update)
        self.assertTrue(state.details_open)

    def test_background_check_is_scheduled_off_the_call_stack(self):
        queued = []
        called = []
        schedule_background_check(lambda delay, callback: queued.append((delay, callback)),
                                  lambda: called.append(True))
        self.assertEqual(called, [])
        self.assertEqual(queued[0][0], 15_000)
        queued[0][1]()
        self.assertEqual(called, [True])

    def test_verified_download_succeeds_and_bad_checksum_deletes_partial(self):
        with tempfile.TemporaryDirectory() as folder:
            good = os.path.join(folder, "good.exe")
            get = lambda *args, **kwargs: Response(self.payload)
            download_verified_file("https://example.invalid/file", good,
                                  hashlib.sha256(self.payload).hexdigest(), get=get)
            with open(good, "rb") as fh:
                self.assertEqual(fh.read(), self.payload)

            bad = os.path.join(folder, "bad.exe")
            with self.assertRaisesRegex(ValueError, "checksum"):
                download_verified_file("https://example.invalid/file", bad, "0" * 64, get=get)
            self.assertFalse(os.path.exists(bad))

    def test_windows_apply_script_waits_for_exit_then_swaps_and_relaunches(self):
        script = windows_update_script(
            r"C:\Temp\new.exe", r"C:\Apps\GameInstaller.exe", 4242,
            r"C:\Users\User\AppData\Roaming\GameInstaller\update-status.txt",
        )
        self.assertIn('tasklist /FI "PID eq 4242"', script)
        self.assertIn('move /y "C:\\Temp\\new.exe" "C:\\Apps\\GameInstaller.exe"', script)
        self.assertIn('start "" "C:\\Apps\\GameInstaller.exe"', script)
        self.assertIn('echo installed>"C:\\Users\\User\\AppData\\Roaming\\GameInstaller\\update-status.txt"', script)
        self.assertIn('echo failed>"C:\\Users\\User\\AppData\\Roaming\\GameInstaller\\update-status.txt"', script)


class LibrarySearchTests(unittest.TestCase):
    def test_search_matches_name_and_description_case_insensitively(self):
        self.assertTrue(matches_game_search("Café Racer", "Arcade racing", "CAFE"))
        self.assertTrue(matches_game_search("Example", "Online co-op adventure", "co-op online"))
        self.assertFalse(matches_game_search("Example", "Online co-op adventure", "co-op missing"))

    def test_search_trims_whitespace_and_empty_query_shows_everything(self):
        self.assertTrue(matches_game_search("Example", "Description", "  "))
        self.assertTrue(matches_game_search("Café", "Racing", "  cafe  "))


if __name__ == "__main__":
    unittest.main()
