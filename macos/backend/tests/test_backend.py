import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from downloads import Downloader, Stopped, atomic_json, canonical_source, drive_confirmation, parts, sha256
from archives import extract, merge_repair, safe_path, validate_entries, find_tool
from highball import Highball, parse_pins, same_external_path
from worker import (ArchiveSetProgress, install, publish_directory, highball_recipe,
                    prepare_workspace, validate_workspace_volume)
from universal_setup import apply_universal_setup, recipe_catalog
import universal_setup


class Response(io.BytesIO):
    def __init__(self, data, status=200, headers=None, url="https://fixture/game"):
        super().__init__(data)
        self.status, self.headers, self.url = status, headers or {}, url

    def geturl(self):
        return self.url


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class TemporaryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)


class StorageWorkspaceTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.volume = self.root / "external-volume"
        self.volume.mkdir()
        self.job = {"id": "fixture-job", "installFolder": str(self.volume)}
        self.workspace = self.volume / ".game-installer-jobs" / self.job["id"]

    def test_external_target_uses_job_workspace_and_keeps_legacy_data(self):
        legacy = self.root / "internal-support" / self.job["id"]
        legacy.mkdir(parents=True)
        (legacy / "main.part1.rar.part").write_bytes(b"resumable bytes")
        validate_workspace_volume(self.job, self.workspace, minimum_free=0)
        prepare_workspace(self.job, legacy, self.workspace)
        self.assertEqual((self.workspace / "main.part1.rar.part").read_bytes(), b"resumable bytes")
        self.assertEqual((legacy / "main.part1.rar.part").read_bytes(), b"resumable bytes")

    def test_missing_selected_volume_fails_before_workspace_creation(self):
        missing = self.root / "ejected-volume"
        job = {"id": "gone", "installFolder": str(missing)}
        with self.assertRaisesRegex(OSError, "unavailable"):
            validate_workspace_volume(job, missing / ".game-installer-jobs" / "gone")
        self.assertFalse(missing.exists())

    def test_read_only_target_fails_clearly(self):
        original_open = Path.open
        def deny_probe(path, *args, **kwargs):
            if ".game-installer-write-check-" in path.name:
                raise PermissionError("read only")
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", deny_probe):
            with self.assertRaisesRegex(OSError, "not writable"):
                validate_workspace_volume(self.job, self.workspace)

    def test_low_space_target_fails_before_workspace_creation(self):
        usage = shutil.disk_usage(self.volume)
        low = shutil._ntuple_diskusage(usage.total, usage.used, 0)
        with patch("worker.shutil.disk_usage", return_value=low):
            with self.assertRaisesRegex(OSError, "Free at least 1 GB"):
                validate_workspace_volume(self.job, self.workspace)
        self.assertFalse(self.workspace.exists())


class DownloadTests(TemporaryTest):
    payload = b"PK\x03\x04" + bytes(range(256)) * 1200
    url = "https://fixture/no-extension"

    def test_archive_set_counts_cached_parts_and_unknown_sizes(self):
        sources = [{"url": f"https://fixture/{index}"} for index in range(3)]
        first = self.root / "main.part1.rar"
        first.write_bytes(b"a" * 100)
        Path(str(first) + ".json").write_text(json.dumps({"source": sources[0]["url"], "total": 100, "complete": True}))
        second = self.root / "main.part2.rar"
        Path(str(second) + ".json").write_text(json.dumps({"source": sources[1]["url"], "total": 200, "etag": '"v1"'}))
        Path(str(second) + ".part").write_bytes(b"b" * 50)
        with patch.object(ArchiveSetProgress, "_probe"):
            progress = ArchiveSetProgress("main", sources, [], self.root, lambda: False)
        self.assertEqual(progress.fields(1, 50, 200)["archiveTotal"], 0)
        progress.sizes[2] = 300
        fields = progress.fields(1, 75, 200)
        self.assertEqual(fields["archiveDone"], 175)
        self.assertEqual(fields["archiveTotal"], 600)
        self.assertEqual(fields["archiveRemaining"], 425)
        progress.cached[2] = 30
        fields = progress.fields(1, 75, 200)
        self.assertEqual(fields["archiveDone"], 205)
        self.assertEqual(fields["archiveRemaining"], 395)
        self.assertEqual(progress.fields(2, 20, 300)["archiveRemaining"], 280)

    def partial(self):
        destination = self.root / "main.archive"
        halted = False
        def progress(done, total):
            nonlocal halted
            halted = True
        opener = Opener(Response(self.payload, headers={"ETag": '"build1"', "Content-Length": str(len(self.payload))}))
        with self.assertRaises(Stopped):
            Downloader(opener, stop=lambda: halted, progress=progress).fetch(self.url, destination)
        return destination, destination.with_name(destination.name + ".part")

    def test_cancel_preserves_partial_and_retry_resumes_exact_bytes(self):
        destination, partial = self.partial()
        offset = partial.stat().st_size
        remaining = self.payload[offset:]
        opener = Opener(Response(remaining, 206, {"ETag": '"build1"', "Content-Length": str(len(remaining)),
                         "Content-Range": f"bytes {offset}-{len(self.payload)-1}/{len(self.payload)}"}))
        digest = Downloader(opener).fetch(self.url, destination)
        self.assertEqual(destination.read_bytes(), self.payload)
        self.assertEqual(digest, hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(opener.requests[0].get_header("Range"), f"bytes={offset}-")
        self.assertEqual(opener.requests[0].get_header("If-range"), '"build1"')

    def test_ignored_range_restarts_without_appending_and_keeps_old_partial(self):
        destination, partial = self.partial()
        old = partial.read_bytes()
        replacement = b"PKnewbuild"
        Downloader(Opener(Response(replacement, headers={"Content-Length": str(len(replacement))}))).fetch(self.url, destination)
        self.assertEqual(destination.read_bytes(), replacement)
        self.assertEqual(next(self.root.glob("*.part.previous-*")).read_bytes(), old)

    def test_invalid_range_or_changed_validator_leaves_partial(self):
        for invalid in ["range", "etag"]:
            with self.subTest(invalid=invalid):
                folder = self.root / invalid
                folder.mkdir()
                destination = folder / "main"
                partial = folder / "main.part"
                partial.write_bytes(b"123")
                atomic_json(folder / "main.json", {"source": self.url, "etag": '"one"'})
                response = Response(b"456", 206, {"Content-Range": "bytes 2-5/6" if invalid == "range" else "bytes 3-5/6",
                                                 "ETag": '"two"' if invalid == "etag" else '"one"'})
                with self.assertRaises(ValueError):
                    Downloader(Opener(response)).fetch(self.url, destination)
                self.assertEqual(partial.read_bytes(), b"123")


    def test_416_is_not_promoted_to_success(self):
        destination, partial = self.partial()
        error = urllib.error.HTTPError(self.url, 416, "range", {}, io.BytesIO(b""))
        opener = Opener(error, Response(self.payload, headers={"Content-Length": str(len(self.payload))}))
        Downloader(opener).fetch(self.url, destination)
        self.assertEqual(len(opener.requests), 2)
        self.assertIsNone(opener.requests[1].get_header("Range"))
        self.assertTrue(list(self.root.glob("*.part.previous-*")))

    def test_unidentified_partial_is_preserved_and_not_resumed(self):
        destination = self.root / "main"
        (self.root / "main.part").write_bytes(b"unknown")
        opener = Opener(Response(b"new"))
        Downloader(opener).fetch(self.url, destination)
        self.assertIsNone(opener.requests[0].get_header("Range"))
        self.assertTrue(list(self.root.glob("*.part.previous-*")))

    def test_short_transfer_and_wrong_hash_never_complete(self):
        with self.assertRaises(ValueError):
            Downloader(Opener(Response(b"short", headers={"Content-Length": "100"}))).fetch(self.url, self.root / "short")
        self.assertTrue((self.root / "short.part").exists())
        with self.assertRaises(ValueError):
            Downloader(Opener(Response(b"wrong"))).fetch(self.url, self.root / "hash", "0" * 64)
        self.assertFalse((self.root / "hash").exists())

    def test_cached_completed_file_is_rehashed(self):
        destination = self.root / "main"
        opener = Opener(Response(b"complete"))
        Downloader(opener).fetch(self.url, destination)
        Downloader(opener).fetch(self.url, destination)
        self.assertEqual(len(opener.requests), 1)
        destination.write_bytes(b"damaged")
        with self.assertRaises(ValueError):
            Downloader(opener).fetch(self.url, destination)

    def test_google_drive_form_and_extensionless_download(self):
        url = "https://drive.google.com/file/d/abc/view"
        html = b'<html><form action="https://drive.usercontent.google.com/download"><input name="id" value="abc"><input name="uuid" value="token"></form></html>'
        opener = Opener(Response(html, headers={"Content-Type": "text/html"}), Response(self.payload))
        destination = self.root / "main"
        Downloader(opener).fetch(url, destination)
        self.assertEqual(destination.read_bytes(), self.payload)
        self.assertIn("uuid=token", opener.requests[1].full_url)
        self.assertIn("export=download", canonical_source(url))

    def test_html_quota_and_untrusted_confirmation_are_explicit_errors(self):
        for content, url in [(b"<html>quota exceeded</html>", "https://drive.usercontent.google.com/download?id=x"),
                             (b"<html>login</html>", self.url),
                             (b'<html><form action="https://evil.example/download"></form></html>', "https://drive.google.com/open?id=x")]:
            with self.subTest(content=content), self.assertRaises(ValueError):
                Downloader(Opener(Response(content, headers={"Content-Type": "text/html"}))).fetch(url, self.root / "bad")
        self.assertFalse((self.root / "bad").exists())

    def test_http_quota_and_network_errors_keep_partial(self):
        destination, partial = self.partial()
        old = partial.read_bytes()
        error = urllib.error.HTTPError(self.url, 403, "quota", {}, io.BytesIO(b"download quota exceeded"))
        with self.assertRaisesRegex(ValueError, "quota"):
            Downloader(Opener(error)).fetch(self.url, destination)
        with self.assertRaises(urllib.error.URLError):
            Downloader(Opener(urllib.error.URLError("offline"))).fetch(self.url, destination)
        self.assertEqual(partial.read_bytes(), old)

    def test_https_only_and_source_forms(self):
        with self.assertRaises(ValueError):
            canonical_source("http://example.com/file")
        self.assertEqual(len(parts("https://a/1, https://a/2\nhttps://a/3")), 3)
        self.assertEqual(parts([{"url": "https://a/1", "name": "a.part1.rar"}])[0]["name"], "a.part1.rar")


class UniversalSetupTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.catalog = recipe_catalog()
        self.build = self.catalog["builds"]["party-animals"]
        self.install = self.root / "Party Animals"
        self.install.mkdir()
        (self.install / "PartyAnimals.exe").write_bytes(b"exe")
        (self.install / "UnityPlayer.dll").write_bytes(b"fixture")
        (self.install / "PartyAnimals_Data").mkdir()
        (self.install / "winhttp.dll").write_bytes(b"proxy")

    def game(self, records=None):
        return {"gameID": "party-animals", "installPath": self.install,
                "mainArchives": records or self.build["main"], "repairArchives": self.build["repair"],
                "fixZipUrl": "https://fixture/fix", "steamAppId": "12345"}

    def test_verified_build_prepares_universal_profile_and_only_scans_dlls(self):
        result = apply_universal_setup(self.game())
        self.assertTrue(result["readyForHighball"])
        self.assertEqual(result["settings"]["renderer"], "d3dmetal")
        self.assertEqual(result["settings"]["arguments"], ["-windowed", "-ResX=1280", "-ResY=720"])
        self.assertTrue(result["settings"]["requiresSteamFirst"])
        self.assertEqual(result["collidingDlls"], ["winhttp.dll"])
        self.assertFalse((self.root / "steamapps/appmanifest_12345.acf").exists())
        self.assertEqual(apply_universal_setup(self.game()), result)

    def test_mismatched_archive_applies_no_settings_and_skips_scan(self):
        bad = [dict(self.build["main"][0], sha256="0" * 64)]
        result = apply_universal_setup(self.game(records=bad))
        self.assertFalse(result["buildMatched"])
        self.assertFalse(result["readyForHighball"])
        self.assertIsNone(result["settings"])
        self.assertEqual(result["collidingDlls"], [])

    def test_party_animals_delta_checks_expected_files(self):
        (self.install / "UnityPlayer.dll").unlink()
        result = apply_universal_setup(self.game())
        self.assertTrue(result["buildMatched"])
        self.assertFalse(result["readyForHighball"])
        self.assertIsNone(result["settings"])
        self.assertTrue(any(item["step"] == "Game files" and item["status"] == "skipped"
                            for item in result["report"]))

    def test_recipe_catalog_is_universal_plus_delta_with_no_duplicated_settings(self):
        catalog = recipe_catalog()
        self.assertEqual(set(catalog["deltas"]["meccha-chameleon"]), {"executable", "requiredRecipes"})
        self.assertEqual(catalog["deltas"]["meccha-chameleon"]["requiredRecipes"], ["steam", "vcrun2022"])
        self.assertNotIn("renderer", catalog["deltas"]["meccha-chameleon"])
        self.assertNotIn("arguments", catalog["deltas"]["meccha-chameleon"])
        receipt = {"gameID": "meccha-chameleon", "mainHashes": ["main"], "repairHashes": ["repair"],
                   "universalSetup": {"readyForHighball": True,
                       "settings": {"renderer": "d3dmetal", "arguments": ["-windowed"],
                                    "requiresSteamFirst": True, "engine": "fixture"}}}
        combined = highball_recipe(receipt)
        self.assertEqual(combined["executable"], catalog["deltas"]["meccha-chameleon"]["executable"])
        self.assertEqual(combined["renderer"], "d3dmetal")
        self.assertEqual(combined["arguments"], ["-windowed"])


class ArchiveTests(TemporaryTest):
    def zipped(self, name, entries):
        path = self.root / name
        with zipfile.ZipFile(path, "w") as archive:
            for key, value in entries.items():
                archive.writestr(key, value)
        return path

    def test_zip_and_repair_wrapper_merge(self):
        main = self.zipped("base", {"wrapper/Game.exe": b"exe", "wrapper/old.dll": b"original"})
        repair = self.zipped("repair", {"patch/wrapper/old.dll": b"fixed", "patch/wrapper/sub/new.ini": b"config"})
        extract(main, self.root / "base-out")
        extract(repair, self.root / "repair-out")
        merge_repair(self.root / "repair-out", self.root / "base-out/wrapper")
        self.assertEqual((self.root / "base-out/wrapper/old.dll").read_bytes(), b"fixed")
        self.assertEqual((self.root / "base-out/wrapper/sub/new.ini").read_bytes(), b"config")

    def test_path_traversal_absolute_windows_and_nul_rejected(self):
        for name in ["../outside", "/outside", "C:\\outside", "a/../../outside", "\\\\server\\file", "nul\0file"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                safe_path(name)
        with self.assertRaises(ValueError):
            extract(self.zipped("bad.zip", {"../outside": b"bad"}), self.root / "out")
        self.assertFalse((self.root / "outside").exists())

    def test_links_and_case_unicode_collisions_rejected(self):
        for entries in [[("link", True)], [("File", False), ("file", False)], [("é", False), ("e\u0301", False)]]:
            with self.assertRaises(ValueError):
                validate_entries(entries)
        path = self.root / "link.zip"
        with zipfile.ZipFile(path, "w") as zipped:
            info = zipfile.ZipInfo("link")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zipped.writestr(info, "../outside")
        with self.assertRaises(ValueError):
            extract(path, self.root / "links")

    def test_missing_rar_tool_and_unknown_format(self):
        rar = self.root / "rar"
        rar.write_bytes(b"Rar!\x1a\x07\x00dummy")
        with patch("archives.find_tool", return_value=None), self.assertRaisesRegex(ValueError, "bundled archive helper"):
            extract(rar, self.root / "rar-out")
        rar.write_bytes(b"HTML or garbage")
        with self.assertRaisesRegex(ValueError, "not a supported"):
            extract(rar, self.root / "unknown-out")

    def test_cancellation_does_not_publish_install_or_touch_existing_folder(self):
        archive = self.zipped("main", {"Game.exe": b"exe"})
        job = {"id": "fixture-job", "game": {"id": "fixture", "name": "Fixture", "zipUrl": "https://fixture/main"},
               "installFolder": str(self.root / "Games")}
        downloader = Mock()
        def fetch(url, destination):
            shutil.copyfile(archive, destination)
            return sha256(destination)
        downloader.fetch.side_effect = fetch
        counter = 0
        def stop():
            nonlocal counter
            counter += 1
            return counter > 1
        with self.assertRaises(Stopped):
            install(job, self.root / "cache", stop, notify=Mock(), downloader=downloader)
        self.assertFalse((self.root / "Games/Fixture").exists())
        self.assertTrue((self.root / "cache/main.archive").exists())

    def test_install_applies_only_catalog_repair_and_refuses_existing_target(self):
        main = self.zipped("base.zip", {"wrap/Game.exe": b"exe", "wrap/runtime.dll": b"old"})
        main_extra = self.zipped("base-extra.zip", {"wrap/UnityPlayer.dll": b"matching runtime"})
        repair = self.zipped("fix.zip", {"wrap/runtime.dll": b"new"})
        files = {"https://fixture/main": main, "https://fixture/main-extra": main_extra,
                 "https://fixture/repair": repair}
        downloader = Mock()
        def fetch(url, destination):
            shutil.copyfile(files[url], destination)
            return sha256(destination)
        downloader.fetch.side_effect = fetch
        steamapps = self.root / "Steam/steamapps"
        steamapps.mkdir(parents=True)
        job = {"id": "fixture", "game": {"id": "fixture", "name": "Fixture",
               "steamAppId": "12345", "zipUrl": "https://fixture/main, https://fixture/main-extra",
               "fixZipUrl": "https://fixture/repair"},
               "installFolder": str(self.root / "Games")}
        with patch.dict(os.environ, {"HIGHBALL_STEAMAPPS": str(steamapps)}):
            receipt = install(job, self.root / "cache", notify=Mock(), downloader=downloader)
        self.assertEqual((Path(receipt["installPath"]) / "runtime.dll").read_bytes(), b"new")
        self.assertEqual((Path(receipt["installPath"]) / "UnityPlayer.dll").read_bytes(), b"matching runtime")
        self.assertFalse((steamapps / "appmanifest_12345.acf").exists())
        self.assertFalse(receipt["recordedRecipeAvailable"])
        # A new job cannot overwrite a prior game or borrow its ownership.
        with self.assertRaisesRegex(ValueError, "already exists"):
            install(dict(job, id="other"), self.root / "other-cache", notify=Mock(), downloader=downloader)

    def test_normal_and_more_players_install_independently(self):
        normal = self.zipped("normal.zip", {"Game/Game.exe": b"normal"})
        modified = self.zipped("modified.zip", {"Game/Game.exe": b"modified"})
        repair = self.zipped("repair.zip", {"Game/repair.txt": b"normal repair"})
        files = {"https://fixture/normal": normal, "https://fixture/more": modified,
                 "https://fixture/repair": repair}
        downloader = Mock()
        def fetch(url, destination):
            shutil.copyfile(files[url], destination)
            return sha256(destination)
        downloader.fetch.side_effect = fetch
        base = str(self.root / "Games")
        original = {"id": "normal-job", "game": {"id": "fixture", "name": "Fixture",
                    "zipUrl": "https://fixture/normal", "fixZipUrl": "https://fixture/repair"},
                    "installFolder": base}
        more = {"id": "more-job", "game": {"id": "fixture--more-players", "name": "Fixture (More Players)",
                "zipUrl": "https://fixture/more"}, "installFolder": base}
        first = install(original, self.root / "normal-cache", notify=Mock(), downloader=downloader)
        second = install(more, self.root / "more-cache", notify=Mock(), downloader=downloader)
        original_path, more_path = Path(first["installPath"]), Path(second["installPath"])
        self.assertNotEqual(original_path, more_path)
        self.assertEqual((original_path / "Game.exe").read_bytes(), b"normal")
        self.assertEqual((more_path / "Game.exe").read_bytes(), b"modified")
        self.assertTrue((original_path / "repair.txt").is_file())
        self.assertFalse((more_path / "repair.txt").exists())

    def test_download_status_names_primary_and_active_backup_host(self):
        archive = self.zipped("host-fixture.zip", {"Fixture/Game.exe": b"fixture never executed"})
        data = archive.read_bytes()
        primary = "https://archive.org/download/fixture/game.zip"
        backup = "https://pixeldrain.com/api/file/fixture?download"
        error = urllib.error.HTTPError(primary, 503, "offline", {}, io.BytesIO(b""))
        opener = Opener(error, Response(data, headers={"Content-Length": str(len(data))}))
        notify = Mock()
        job = {"id": "host-label", "game": {"id": "fixture", "name": "Fixture",
               "zipUrl": primary, "zipBackupUrl": backup}, "installFolder": str(self.root / "Games")}
        with patch("worker.Downloader", side_effect=lambda **kwargs: Downloader(opener, **kwargs)):
            install(job, self.root / "cache", notify=notify)
        messages = [call.args[1] for call in notify.call_args_list if call.args[0] == "downloading"]
        self.assertTrue(any("via Internet Archive" in message for message in messages))
        self.assertTrue(any("trying backup 1 via Pixeldrain" in message for message in messages))
        transferred = [call.args[1] for call in notify.call_args_list
                       if call.args[0] == "downloading" and call.kwargs.get("done", 0) > 0]
        self.assertTrue(transferred)
        self.assertTrue(all("via Pixeldrain" in message for message in transferred))

    def test_fixture_install_runs_universal_setup_after_repair_and_reports_steps(self):
        main = self.zipped("fixture-main.zip", {"Fixture/Game.exe": b"exe", "Fixture/winhttp.dll": b"proxy"})
        repair = self.zipped("fixture-fix.zip", {"Fixture/fix.txt": b"fixed"})
        records = lambda path: [{"bytes": path.stat().st_size, "sha256": sha256(path)}]
        catalog = {"builds": {"fixture": {"main": records(main), "repair": records(repair),
                                           "engine": "x64-sikarugir10.0_6-r28"}},
                   "deltas": {"fixture": {"executable": "Game.exe"}}}
        files = {"https://fixture/main": main, "https://fixture/fix": repair}
        downloader = Mock()
        def fetch(url, destination):
            shutil.copyfile(files[url], destination)
            return sha256(destination)
        downloader.fetch.side_effect = fetch
        steamapps = self.root / "Highball/Test/drive_c/Program Files (x86)/Steam/steamapps"
        steamapps.mkdir(parents=True)
        job = {"id": "fixture-e2e", "game": {"id": "fixture", "name": "Fixture",
               "steamAppId": "987654", "zipUrl": "https://fixture/main", "fixZipUrl": "https://fixture/fix"},
               "installFolder": str(self.root / "Games")}
        notifications = []
        with patch.object(universal_setup, "recipe_catalog", return_value=catalog), \
                patch.dict(os.environ, {"HIGHBALL_STEAMAPPS": str(steamapps)}):
            receipt = install(job, self.root / "cache-e2e", notify=lambda *a, **kw: notifications.append((a, kw)),
                              downloader=downloader)
        self.assertTrue(receipt["universalSetup"]["readyForHighball"])
        self.assertTrue((Path(receipt["installPath"]) / "fix.txt").is_file())
        self.assertFalse((steamapps / "appmanifest_987654.acf").exists())
        status = next(args[1] for args, _ in notifications if args and args[0] == "installed")
        self.assertIn("DLL overrides", status)
        self.assertIn("D3DMetal prepared", status)

    def test_publish_refuses_even_an_empty_existing_directory(self):
        source, target = self.root / "source", self.root / "existing"
        source.mkdir()
        target.mkdir()
        (source / "owned.txt").write_text("owned")
        with self.assertRaises(OSError):
            publish_directory(source, target)
        self.assertTrue((source / "owned.txt").exists())
        self.assertEqual(list(target.iterdir()), [])

    def test_mocked_rar_listing_is_validated_before_native_extraction(self):
        archive = self.root / "fixture.rar"
        archive.write_bytes(b"Rar!\x1a\x07\x00fixture")
        listing = "Path = Game.exe\nSize = 3\nAttributes = A -rw-r--r--\n"
        def tool(command, stop):
            if command[1] == "l":
                return listing
            destination = Path(next(arg[2:] for arg in command if arg.startswith("-o")))
            (destination / "Game.exe").write_bytes(b"exe")
            return ""
        with patch("archives.find_tool", side_effect=lambda name: "/fixture/" + name), patch("archives.run_tool", side_effect=tool):
            extract(archive, self.root / "rar-out")
        self.assertEqual((self.root / "rar-out/Game.exe").read_bytes(), b"exe")
        def omit_extracted_file(command, stop):
            return listing if command[1] == "l" else ""
        with patch("archives.find_tool", side_effect=lambda name: "/fixture/" + name), patch("archives.run_tool", side_effect=omit_extracted_file):
            with self.assertRaisesRegex(ValueError, "did not extract completely"):
                extract(archive, self.root / "missing-rar-out")
        bad_listing = "Path = ../outside\nSize = 3\nAttributes = A -rw-r--r--\n"
        with patch("archives.find_tool", return_value="/fixture/tool"), patch("archives.run_tool", return_value=bad_listing) as mocked:
            with self.assertRaises(ValueError):
                extract(archive, self.root / "bad-rar")
            self.assertEqual(mocked.call_count, 1)

    def test_real_worker_process_installs_cached_fixtures_and_recovers_receipt(self):
        state = self.root / "state"
        cache = state / "fixture-process"
        cache.mkdir(parents=True)
        game = {"id": "fixture", "name": "Process Fixture", "zipUrl": "https://fixture/main", "updateZipUrl": "https://fixture/update", "fixZipUrl": "https://fixture/repair"}
        job = {"id": "fixture-process", "game": game, "installFolder": str(self.root / "Games")}
        atomic_json(state / "fixture-process.json", job)
        for label, url, entries in [("main", game["zipUrl"], {"Game.exe": b"fixture never executed", "runtime.dll": b"old"}),
                                    ("update", game["updateZipUrl"], {"update-only.txt": b"updated", "runtime.dll": b"update"}),
                                    ("repair", game["fixZipUrl"], {"runtime.dll": b"new"})]:
            archive = cache / (label + ".archive")
            with zipfile.ZipFile(archive, "w") as zipped:
                for name, data in entries.items():
                    zipped.writestr(name, data)
            atomic_json(cache / (label + ".archive.json"), {"source": url, "complete": True, "sha256": sha256(archive)})
        for attempt in range(2):
            process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve().parents[1] / "worker.py"),
                                        "install", "--job", str(state / "fixture-process.json")],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                process.wait(timeout=10)
                events = [json.loads(line) for line in process.stdout.read().splitlines()]
                self.assertEqual(process.returncode, 0, process.stderr.read())
                self.assertEqual(events[-1]["status"], "installed")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdin.close()
                process.stdout.close()
                process.stderr.close()
        self.assertEqual((self.root / "Games/Process Fixture/runtime.dll").read_bytes(), b"new")
        self.assertEqual((self.root / "Games/Process Fixture/update-only.txt").read_bytes(), b"updated")
        self.assertTrue((cache / "main.archive").exists())

    @unittest.skipUnless(Path("/usr/bin/zip").exists(), "requires macOS zip")
    def test_password_protected_zip_fixture(self):
        (self.root / "Game.exe").write_bytes(b"tiny fixture")
        subprocess.run(["/usr/bin/zip", "-q", "-P", "online-fix.me", "encrypted.zip", "Game.exe"], cwd=self.root, check=True)
        extract(self.root / "encrypted.zip", self.root / "encrypted-out")
        self.assertEqual((self.root / "encrypted-out/Game.exe").read_bytes(), b"tiny fixture")

    @unittest.skipUnless(find_tool("7zz"), "requires the bundled or local 7zz helper")
    def test_real_7z_fixture_uses_validated_native_extraction(self):
        (self.root / "Game.exe").write_bytes(b"tiny fixture")
        subprocess.run([find_tool("7zz"), "a", "fixture.7z", "Game.exe"], cwd=self.root, check=True, stdout=subprocess.DEVNULL)
        extract(self.root / "fixture.7z", self.root / "7z-out")
        self.assertEqual((self.root / "7z-out/Game.exe").read_bytes(), b"tiny fixture")


class HighballTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.game = self.root / "game"
        self.game.mkdir()
        (self.game / "Game.exe").write_bytes(b"not executed")
        self.receipt = {"gameName": "Game Installer · Fixture", "installPath": str(self.game), "mainHashes": ["main"], "repairHashes": ["repair"]}
        self.recipe = {"executable": "Game.exe", "arguments": ["-windowed"], "renderer": "d3dmetal", "environment": {"WINEDLLOVERRIDES": "winmm=n,b"},
                       "mainHashes": ["main"], "repairHashes": ["repair"], "requiredRecipes": ["steam", "vcrun2022"], "engine": "fixture-engine"}
        self.report = {"readiness": {"home": str(self.root / "Highball"), "engines": [{"id": "fixture-engine", "renderers": {"d3dmetal": {"available": True}}}],
                                    "bottles": [{"name": "Disposable", "engine": "fixture-engine", "recipes": "steam,vcrun2022", "ready": True}]}}
        self.commands = []
        self.pinned = False
        self.bad_settings = False
        def runner(command, **kwargs):
            self.commands.append(command[1:])
            self.assertFalse(kwargs["shell"])
            self.assertEqual(kwargs["env"]["HIGHBALL_HOME"], str(self.root / "Highball"))
            if command[1:4] == ["steam", "launch", "Disposable"] and command[-1] == "--check":
                return subprocess.CompletedProcess(command, 0, json.dumps({"bottle": "Disposable", "executable": "/fixture/Program Files/Steam/Steam.exe", "arguments": [command[command.index("--uri") + 1]]}), "")
            if command[1:3] == ["pin", "add"]:
                self.pinned = True
            if command[1:3] == ["pin", "list"]:
                output = "Steam\tProgram Files (x86)/Steam/steam.exe\n"
                if self.pinned:
                    extras = "" if self.bad_settings else "\targs=-windowed\tenv=WINEDLLOVERRIDES=winmm=native,builtin\trenderer=d3dmetal"
                    output += "Game Installer · Fixture\t" + str(self.game / "Game.exe") + extras + "\n"
            else:
                output = "ok\n"
            return subprocess.CompletedProcess(command, 0, output, "")
        self.runner = Mock(side_effect=runner)
        self.child = Mock(pid=1234)
        self.child.wait.return_value = 0
        def launcher(command, **kwargs):
            self.commands.append(command[1:])
            self.assertFalse(kwargs["shell"])
            self.assertTrue(kwargs["start_new_session"])
            return self.child
        self.client = Highball(helper=self.root / "fake-helper", inspector=lambda: self.report, runner=self.runner,
                               gui_running=lambda: False, launcher=launcher, launch_directory=self.root / "launch-logs")

    def test_version_mismatch_reports_actual_cause(self):
        self.report = {"interface": {"status": "version_mismatch", "app": {"version": "0.10.26"}}, "readiness": None}
        with self.assertRaisesRegex(ValueError, r"Highball 0\.10\.26.*selected Game Installer helper"):
            self.client.ready("Disposable")
        self.runner.assert_not_called()

    def test_recipe_sets_and_verifies_only_explicit_fields(self):
        (self.game / "winmm.dll").write_bytes(b"fixture DLL never executed")
        self.client.register(self.receipt, "Disposable", recipe=self.recipe)
        self.assertTrue(self.receipt["registration"]["complete"])
        self.assertIn(["pin", "set", "Disposable", "Game Installer · Fixture", "env", "WINEDLLOVERRIDES=winmm=native,builtin"], self.commands)
        self.assertFalse(any(command[0] == "bottle" for command in self.commands))

    def test_bundled_crewlink_requires_installer_and_verifies_pin(self):
        drive = self.root / "Highball/bottles/Disposable/drive_c"
        drive.mkdir(parents=True)
        setup = self.game / "Better-CrewLink-Setup-3.2.2.exe"
        setup.write_bytes(b"fixture")
        receipt = dict(self.receipt, gameID="among-us", registration={
            "complete": True, "bottle": "Disposable", "pin": "Among Us", "executable": "Game.exe"})
        target = drive / "users/test/AppData/Local/Programs/BetterCrewLink/BetterCrewLink.exe"
        commands = []
        pinned = [False]
        def command(args, timeout=30):
            commands.append(args)
            if args[:2] == ["pin", "list"]:
                return "Among Us\t" + str(self.game / "Game.exe") + "\n" + (
                    "BetterCrewLink\t" + str(target.relative_to(drive)) + "\n" if pinned[0] else "")
            if args[0] == "run":
                target.parent.mkdir(parents=True)
                target.write_bytes(b"installed fixture")
            if args[:2] == ["pin", "add"]:
                pinned[0] = True
            return "ok"
        self.client.command = command
        self.assertEqual(self.client.install_bettercrewlink(receipt), str(target.resolve()))
        self.assertIn(["run", "Disposable", str(setup.resolve())], commands)
        self.assertIn(["pin", "add", "Disposable", "BetterCrewLink", str(target.resolve())], commands)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.client.install_bettercrewlink(receipt)

    def test_bundled_crewlink_does_not_run_over_existing_files(self):
        drive = self.root / "Highball/bottles/Disposable/drive_c"
        drive.mkdir(parents=True)
        (drive / "BetterCrewLink.exe").write_bytes(b"existing")
        (self.game / "Better-CrewLink-Setup-3.2.2.exe").write_bytes(b"fixture")
        receipt = dict(self.receipt, gameID="among-us", registration={
            "complete": True, "bottle": "Disposable", "pin": "Among Us", "executable": "Game.exe"})
        self.client.command = Mock(return_value="Among Us\t" + str(self.game / "Game.exe") + "\n")
        with self.assertRaisesRegex(ValueError, "Existing BetterCrewLink"):
            self.client.install_bettercrewlink(receipt)
        self.assertFalse(any(call.args[0][0] == "run" for call in self.client.command.call_args_list))

    def test_bundled_crewlink_missing_installed_executable_is_not_success(self):
        (self.root / "Highball/bottles/Disposable/drive_c").mkdir(parents=True)
        (self.game / "Better-CrewLink-Setup-3.2.2.exe").write_bytes(b"fixture")
        receipt = dict(self.receipt, gameID="among-us", registration={
            "complete": True, "bottle": "Disposable", "pin": "Among Us", "executable": "Game.exe"})
        self.client.command = Mock(side_effect=lambda args, **kwargs:
                                   "Among Us\t" + str(self.game / "Game.exe") + "\n" if args[0] == "pin" else "ok")
        with self.assertRaisesRegex(ValueError, "could not be found"):
            self.client.install_bettercrewlink(receipt)
        self.assertFalse(any(call.args[0][:2] == ["pin", "add"] for call in self.client.command.call_args_list))

    def test_unknown_build_engine_dependency_or_renderer_does_not_mutate(self):
        for failure in ["hash", "engine", "dependency", "renderer"]:
            with self.subTest(failure=failure):
                import copy
                recipe = copy.deepcopy(self.recipe)
                if failure == "hash": recipe["mainHashes"] = ["other"]
                elif failure == "engine": recipe["engine"] = "other"
                elif failure == "dependency": recipe["requiredRecipes"] = ["other"]
                else: recipe["renderer"] = "dxvk"
                with self.assertRaises(ValueError):
                    self.client.register(self.receipt, "Disposable", recipe=recipe)
                self.assertFalse(self.commands)

    def test_gui_running_blocks_registration(self):
        self.client.gui_running = lambda: True
        with self.assertRaisesRegex(ValueError, "Close Highball"):
            self.client.register(self.receipt, "Disposable", executable="Game.exe")
        self.assertFalse(self.commands)

    def test_manual_executable_uses_defaults_and_never_infers_fixes(self):
        self.client.register(self.receipt, "Disposable", executable="Game.exe")
        self.assertFalse(any(command[:2] == ["pin", "set"] for command in self.commands))

    def test_existing_unowned_or_conflicting_pin_does_not_change(self):
        self.pinned = True
        with self.assertRaisesRegex(ValueError, "not owned"):
            self.client.register(self.receipt, "Disposable", executable="Game.exe")
        self.assertEqual(len(self.commands), 1)
        self.runner.side_effect = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "Game Installer · Fixture\tZ:\\elsewhere\\Game.exe\n", "")
        with self.assertRaisesRegex(ValueError, "elsewhere"):
            self.client.register(self.receipt, "Disposable", executable="Game.exe")

    def test_missing_saved_setting_is_not_marked_complete(self):
        self.bad_settings = True
        with self.assertRaisesRegex(ValueError, "every recorded"):
            self.client.register(self.receipt, "Disposable", recipe=self.recipe)
        self.assertFalse(self.receipt["registration"]["complete"])
        self.bad_settings = False
        self.client.register(self.receipt, "Disposable", recipe=self.recipe)
        self.assertTrue(self.receipt["registration"]["complete"])

    def test_steam_uri_is_routed_to_windows_steam_in_selected_bottle(self):
        self.client.steam_install("Disposable", "steam://install/480")
        self.assertEqual(self.commands[-1], ["steam", "launch", "Disposable", "--uri", "steam://install/480"])
        self.assertFalse(any(command[0] in ["open", "bottle"] for command in self.commands))
        with self.assertRaises(ValueError):
            self.client.steam_install("Disposable", "steam://run/480")

    def test_open_main_uses_highball_operation_with_selected_bottle_and_uri(self):
        result = self.client.steam_install("Disposable", "steam://open/main")
        self.assertEqual(self.commands[-1], ["steam", "launch", "Disposable", "--uri", "steam://open/main"])
        self.assertTrue(result["steamOpened"])

    def test_missing_steam_bottle_or_cli_error_stops_cleanly(self):
        with self.assertRaises(ValueError):
            self.client.steam_install("Missing", "steam://install/480")
        self.runner.side_effect = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "Windows Steam missing at /fixture/Steam.exe; Highball > Advanced > Steam executable")
        with self.assertRaisesRegex(ValueError, "/fixture/Steam.exe; Highball > Advanced > Steam executable"):
            self.client.steam_install("Disposable", "steam://install/480")
        self.assertFalse(any(command[:2] == ["steam", "launch"] and command[-1] != "--check" for command in self.commands))
        self.runner.side_effect = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "fixture error")
        with self.assertRaisesRegex(ValueError, "fixture error"):
            self.client.steam_install("Disposable", "steam://install/480")

    def test_steam_path_is_reported_without_editing_pins(self):
        messages = []
        result = self.client.steam_install("Disposable", "steam://open/main", messages.append)
        self.assertEqual(result["steamExecutable"], "/fixture/Program Files/Steam/Steam.exe")
        self.assertIn(result["steamExecutable"], messages[-1])
        self.assertIn("Highball > Settings", messages[-1])
        self.assertEqual(self.commands[0], ["steam", "launch", "Disposable", "--uri", "steam://open/main", "--check"])
        self.assertFalse(any(cmd[0] == "pin" for cmd in self.commands))

    def test_bad_steam_request_never_dispatches(self):
        self.runner.side_effect = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, json.dumps({"bottle": "Other", "executable": "/wrong/Steam.exe", "arguments": []}), "")
        self.client.dispatch = Mock()
        with self.assertRaisesRegex(ValueError, "confirm the Steam launch path"):
            self.client.steam_install("Disposable", "steam://open/main")
        self.client.dispatch.assert_not_called()

    def test_official_steam_install_routes_by_appid_without_game_pin(self):
        def response(cmd, **kw):
            if cmd[1:3] == ["steam", "resolve"]:
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"path": "/fixture/Program Files/Steam/Steam.exe"}), "")
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"installed": True}), "")
        self.runner.side_effect = response
        result = self.client.launch_steam_game(self.receipt, "480", "Disposable")
        self.assertTrue(result["steamGame"])
        self.assertEqual(self.commands[-1], ["steam", "game", "Disposable", "480", "--install-path", str(self.game)])
        self.assertNotIn("registration", self.receipt)

    def test_local_copy_is_not_treated_as_authenticated_steam_install(self):
        self.runner.side_effect = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, json.dumps({"installed": False}), "")
        self.client.dispatch = Mock()
        messages = []
        self.assertIsNone(self.client.launch_steam_game(self.receipt, "480", "Disposable", messages.append))
        self.assertEqual(messages, [])
        self.client.dispatch.assert_not_called()
        self.assertIsNone(self.client.launch_steam_game(self.receipt, "not-an-app-id", "Disposable"))

    def test_launch_requires_receipt_and_uses_registered_pin(self):
        with self.assertRaises(ValueError):
            self.client.launch(self.receipt)
        self.client.register(self.receipt, "Disposable", executable="Game.exe")
        self.client.launch(self.receipt)
        self.assertEqual(self.commands[-1], ["run", "Disposable", "Game Installer · Fixture"])

    def test_steam_first_opens_steam_then_waits_for_second_play_click(self):
        self.client.register(self.receipt, "Disposable", executable="Game.exe")
        self.receipt["universalSetup"] = {"settings": {"requiresSteamFirst": True}}
        self.client.dispatch = Mock(return_value={"pid": 123, "logPath": str(self.root / "launch.log")})
        first = self.client.launch(self.receipt)
        self.assertTrue(first["steamStarted"])
        self.assertTrue(self.receipt["registration"]["steamReadyForNextLaunch"])
        self.assertEqual(self.client.dispatch.call_args.args[0], ["steam", "launch", "Disposable", "--uri", "steam://open/main"])
        second = self.client.launch(self.receipt)
        self.assertNotIn("steamStarted", second)
        self.assertFalse(self.receipt["registration"]["steamReadyForNextLaunch"])
        self.assertEqual(self.client.dispatch.call_args.args[0], ["run", "Disposable", "Game Installer · Fixture"])

    def test_manual_executable_cannot_escape_install_folder(self):
        with self.assertRaises(ValueError):
            self.client.register(self.receipt, "Disposable", executable="../outside.exe")
        self.assertFalse(self.commands)

    def test_external_path_supports_current_unix_and_wine_spellings(self):
        path = self.game / "Game.exe"
        self.assertTrue(same_external_path(str(path), path))
        self.assertTrue(same_external_path("Z:" + str(path).replace("/", "\\"), path))
        self.assertFalse(same_external_path("C:\\other\\Game.exe", path))

    def test_long_running_steam_does_not_hold_worker_and_early_launch_failure_surfaces(self):
        self.child.wait.side_effect = subprocess.TimeoutExpired("fixture", 2)
        result = self.client.steam_install("Disposable", "steam://install/480")
        self.assertEqual(result["pid"], 1234)
        self.assertTrue(Path(result["logPath"]).is_file())
        self.child.wait.side_effect = None
        self.child.wait.return_value = 1
        with self.assertRaisesRegex(ValueError, "launch request exited 1"):
            self.client.steam_install("Disposable", "steam://install/480")


if __name__ == "__main__":
    unittest.main()
