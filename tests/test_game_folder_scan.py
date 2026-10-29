import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from main import Game, scan_installed_games


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
                Game({"id": "installed", "name": "Installed Game", "zipUrl": "https://example.invalid/game.zip"}),
                Game({"id": "missing", "name": "Missing Game", "zipUrl": "https://example.invalid/game.zip"}),
                Game({"id": "empty", "name": "Empty Game", "zipUrl": "https://example.invalid/game.zip"}),
                Game({"id": "patch", "name": "Patch Entry", "type": "fix", "zipUrl": "https://example.invalid/patch.zip"}),
            ]

            with patch("main.load_prefs", return_value={}):
                results = scan_installed_games(games, str(root))

        by_id = {record["game"].id: record for record in results}
        self.assertEqual(set(by_id), {"installed", "missing", "empty"})
        self.assertEqual(by_id["installed"]["folder"], str(installed))
        self.assertEqual(by_id["installed"]["exe"], str(installed / "Installed Game.exe"))
        self.assertIsNone(by_id["missing"]["folder"])
        self.assertIsNone(by_id["empty"]["folder"])


if __name__ == "__main__":
    unittest.main()
