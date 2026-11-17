import json
import os
import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import Mock, patch

if sys.platform != "win32":
    raise unittest.SkipTest("Windows installer edition tests")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication

from main import Game, InstallDialog


class GameEditionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.catalog = json.loads(Path("games.json").read_text(encoding="utf-8"))

    def test_trailing_periods_produce_usable_windows_folder(self):
        game = Game({"id": "semi", "name": "S.E.M.I. – Side Effects May Include..."})
        self.assertEqual(game.safe_folder_name, "S.E.M.I. – Side Effects May Include")
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / game.safe_folder_name
            folder.mkdir()
            (folder / "Manifest_DebugFiles_Win64.txt").write_text("ok", encoding="utf-8")
            self.assertTrue((folder / "Manifest_DebugFiles_Win64.txt").is_file())

    def test_more_players_has_own_download_identity_and_no_normal_repairs(self):
        for game_id in ("mimesis", "peak", "schedule-i"):
            with self.subTest(game_id=game_id):
                normal = Game(next(item for item in self.catalog if item["id"] == game_id))
                modified = normal.more_players_game()
                self.assertNotEqual(normal.id, modified.id)
                self.assertNotEqual(normal.safe_folder_name, modified.safe_folder_name)
                self.assertEqual(modified.zip_url, normal.more_players["zipUrl"])
                self.assertFalse(modified.fix_parts)
                self.assertFalse(modified.fix_backup_parts)
                self.assertFalse(modified.zip_backup_parts)
                self.assertEqual(modified.mac_compat["tier"], "Unverified")
        schedule = Game(next(item for item in self.catalog if item["id"] == "schedule-i"))
        self.assertEqual(schedule.zip_url, "https://archive.org/download/schedule-i-0.4.7f-12-12-player/Schedule-I-0.4.7f12-normal.zip")
        self.assertFalse(schedule.has_fix)

    def test_dialog_queues_selected_edition_and_separate_destination(self):
        game = Game(next(item for item in self.catalog if item["id"] == "schedule-i"))
        manager = Mock(cards=[])
        with tempfile.TemporaryDirectory() as folder, \
             patch("main.load_prefs", return_value={}), patch("main.save_prefs"), \
             patch.object(InstallDialog, "_build_system_check"):
            dialog = InstallDialog(game, manager=manager)
            dialog.dest_edit.setText(folder)
            normal_path = dialog._target_path()
            dialog.edition_combo.setCurrentIndex(1)
            modified_path = dialog._target_path()
            self.assertNotEqual(normal_path, modified_path)
            self.assertFalse(dialog.game.has_fix)
            dialog._start_install()
            queued_game, queued_path = manager.enqueue.call_args.args[:2]
            self.assertEqual(queued_path, modified_path)
            self.assertTrue(queued_game.id.endswith("--more-players"))
            self.assertFalse(queued_game.has_fix)

    def test_existing_edition_folder_is_not_queued_or_overwritten(self):
        game = Game(next(item for item in self.catalog if item["id"] == "peak"))
        manager = Mock(cards=[])
        with tempfile.TemporaryDirectory() as folder, \
             patch("main.load_prefs", return_value={}), patch("main.save_prefs"), \
             patch("main.QMessageBox.warning") as warning, \
             patch.object(InstallDialog, "_build_system_check"):
            dialog = InstallDialog(game, manager=manager)
            dialog.dest_edit.setText(folder)
            dialog.edition_combo.setCurrentIndex(1)
            destination = Path(dialog._target_path())
            destination.mkdir()
            (destination / "existing.txt").write_text("keep", encoding="utf-8")
            dialog._start_install()
            manager.enqueue.assert_not_called()
            warning.assert_called_once()
            self.assertEqual((destination / "existing.txt").read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
