import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if sys.platform != "win32":
    raise unittest.SkipTest("Windows installer owned-games UI tests")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication, QMessageBox
from main import Game, MainWindow


class OwnedGamesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.target = self.root / "target"
        self.source.mkdir()
        self.target.mkdir()
        self.game = Game({"id": "test", "name": "Test Game", "zipUrl": "https://example.com/game.zip"})
        self.folder = self.source / self.game.safe_folder_name
        self.folder.mkdir()
        (self.folder / "game.exe").write_bytes(b"test game")
        with patch.object(MainWindow, "load_manifest"), patch("main.schedule_background_check"), patch("main.consume_update_status", return_value=""):
            self.window = MainWindow()
        self.addCleanup(self.window.close)
        self.window._current_games = [self.game]

    def run_move(self):
        with patch("main.default_game_directory", return_value=str(self.source)), \
             patch("main.QFileDialog.getExistingDirectory", return_value=str(self.target)), \
             patch("main.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
             patch("main.QMessageBox.information"), patch("main.QMessageBox.warning") as warning, \
             patch("main.load_prefs", return_value={}), patch("main.save_prefs") as save:
            self.window._move_games_folder()
            return warning, save

    def test_owned_view_discovers_and_filters_folder(self):
        with patch("main.default_game_directory", return_value=str(self.source)):
            self.window._show_owned_games()
            self.assertEqual(self.window.cards_layout.count(), 1)
            self.window._search_bar.setText("not present")
            self.assertEqual(self.window.cards_layout.itemAt(0).widget().text(), "No installed games found in the selected games folder.")

    def test_moves_game_and_updates_folder(self):
        warning, save = self.run_move()
        warning.assert_not_called()
        self.assertFalse(self.folder.exists())
        self.assertEqual((self.target / self.game.safe_folder_name / "game.exe").read_bytes(), b"test game")
        self.assertEqual(save.call_args.args[0]["install_path"], str(self.target))

    def test_conflict_leaves_both_folders_untouched(self):
        (self.target / self.game.safe_folder_name).mkdir()
        warning, save = self.run_move()
        warning.assert_called_once()
        save.assert_not_called()
        self.assertTrue((self.folder / "game.exe").exists())

    def test_cross_drive_copy_retains_source_and_switches_folder(self):
        with patch("main.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
             patch("main.QMessageBox.information"), patch("main.QMessageBox.warning") as warning, \
             patch("main.load_prefs", return_value={}), patch("main.save_prefs") as save:
            self.window._copy_games_between_drives(str(self.source), str(self.target), [self.game.safe_folder_name])
        warning.assert_not_called()
        self.assertTrue((self.folder / "game.exe").exists())
        self.assertEqual((self.target / self.game.safe_folder_name / "game.exe").read_bytes(), b"test game")
        self.assertEqual(save.call_args.args[0]["install_path"], str(self.target))

    def test_copy_reports_failure_if_folder_preference_cannot_be_saved(self):
        with patch("main.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
             patch("main.QMessageBox.information") as success, \
             patch("main.QMessageBox.warning") as warning, \
             patch("main.load_prefs", return_value={}), patch("main.save_prefs", return_value=False):
            self.window._copy_games_between_drives(str(self.source), str(self.target), [self.game.safe_folder_name])
        success.assert_not_called()
        warning.assert_called_once()
        self.assertIn("could not be saved", warning.call_args.args[2])
        self.assertTrue((self.folder / "game.exe").exists())


if __name__ == "__main__":
    unittest.main()
