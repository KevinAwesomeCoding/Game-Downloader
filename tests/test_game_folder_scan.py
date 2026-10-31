import tempfile
import unittest
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from game_library import scan_installed_games


class CatalogGame:
    def __init__(self, game_id, name, *, is_patch=False):
        self.id = game_id
        self.name = name
        self.is_patch = is_patch
        self.safe_folder_name = name


class GameFolderScanTests(unittest.TestCase):
    def test_reports_catalog_games_as_found_or_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed = root / "Installed Game"
            installed.mkdir()
            (installed / "Installed Game.exe").write_bytes(b"game")
            (installed / "data.bin").write_bytes(b"data")

            empty = root / "Empty Game"
            empty.mkdir()
            hidden = root / ".Hidden Game"
            hidden.mkdir()
            (hidden / "Hidden Game.exe").write_bytes(b"game")

            games = [
                CatalogGame("installed", "Installed Game"),
                CatalogGame("missing", "Missing Game"),
                CatalogGame("empty", "Empty Game"),
                CatalogGame("patch", "Patch Entry", is_patch=True),
            ]
            results = scan_installed_games(games, str(root))

        by_id = {record["game"].id: record for record in results}
        self.assertEqual(set(by_id), {"installed", "missing", "empty"})
        self.assertEqual(by_id["installed"]["folder"], str(installed))
        self.assertEqual(by_id["installed"]["exe"], str(installed / "Installed Game.exe"))
        self.assertIsNone(by_id["missing"]["folder"])
        self.assertIsNone(by_id["empty"]["folder"])

    @unittest.skipUnless(os.name == "nt", "Windows installer extraction code")
    def test_chunked_copy_reports_progress_without_partial_target(self):
        from main import InstallWorker

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.bin"
            target = root / "target.bin"
            payload = b"x" * (10 * 1024 * 1024)
            source.write_bytes(payload)
            progress = []

            copied = InstallWorker._copy_file_tolerant(
                str(source), str(target), progress_callback=progress.append,
            )

            self.assertTrue(copied)
            self.assertEqual(target.read_bytes(), payload)
            self.assertGreater(len(progress), 1)
            self.assertEqual(progress[-1], len(payload))

    @unittest.skipUnless(os.name == "nt", "Windows installer extraction code")
    def test_zip_extraction_fails_if_an_extracted_file_is_quarantined(self):
        from main import InstallWorker

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "game.zip"
            destination = root / "staging"
            destination.mkdir()
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr("How to Fish/winmm.dll", b"fixture")

            worker = InstallWorker(SimpleNamespace(has_fix=False), str(root / "game"))
            errors = []
            worker.error.connect(errors.append)
            original_extract = zipfile.ZipFile.extract

            def extract_then_remove(zipped, member, path=None, pwd=None):
                extracted = original_extract(zipped, member, path, pwd)
                os.remove(extracted)
                return extracted

            with patch.object(zipfile.ZipFile, "extract", extract_then_remove):
                extracted = worker._extract_zip(
                    str(archive), str(destination), worker.extract_progress,
                )

            self.assertFalse(extracted)
            self.assertEqual(len(errors), 1)
            self.assertIn("Extraction finished, but 1 file(s) never landed", errors[0])
            self.assertIn("winmm.dll", errors[0])
            self.assertIn("quarantined", errors[0])


if __name__ == "__main__":
    unittest.main()
