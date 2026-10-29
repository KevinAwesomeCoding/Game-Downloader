import tempfile
import unittest
import os
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
