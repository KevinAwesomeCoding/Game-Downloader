import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if sys.platform != "win32":
    raise unittest.SkipTest("Windows installer owned-games UI tests")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication, QMessageBox, QGraphicsBlurEffect
from PyQt6.QtCore import QEvent, QPointF
from PyQt6.QtGui import QEnterEvent, QImage, QColor
from main import Game, GameCard, GdkSetupWorker, MainWindow, artwork_accent


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
            self.assertEqual(self.window._library_heading.text(), "Your games")
            self.assertTrue(self.window.owned_btn.property("active"))
            self.assertEqual(self.window.cards_layout.count(), 1)
            self.window._search_bar.setText("not present")
            self.assertEqual(self.window.cards_layout.itemAt(0).widget().text(), "No installed games found in the selected games folder.")

    def test_search_remains_usable_at_minimum_window_width(self):
        self.window.resize(900, 600)
        self.window._show_library()
        self.window.show()
        self.app.processEvents()
        self.assertGreaterEqual(self.window._search_bar.width(), 200)

    def test_navigation_tracks_pages_and_filter(self):
        self.window._show_library()
        self.assertTrue(self.window.library_btn.property("active"))
        self.window._library_filter.setCurrentText("Installed games")
        self.assertTrue(self.window.owned_btn.property("active"))
        self.assertEqual(self.window._library_heading.text(), "Your games")
        self.window.stack.setCurrentWidget(self.window.settings_page)
        self.assertTrue(self.window.settings_btn.property("active"))
        self.assertFalse(self.window.owned_btn.property("active"))
        self.window.stack.setCurrentWidget(self.window.downloads_page)
        self.assertTrue(self.window.downloads_btn.property("active"))

    def test_owned_card_animates_blur_and_keeps_overlay_separate(self):
        with patch("main.default_game_directory", return_value=str(self.source)):
            self.window._show_owned_games()
            card = self.window.cards_layout.itemAt(0).widget()
            self.assertIsNone(card.thumb.graphicsEffect())
            card.enterEvent(QEnterEvent(QPointF(), QPointF(), QPointF()))
            self.assertTrue(card._hovered)
            self.assertEqual(card._hover_fade.endValue(), 1.0)
            card._on_hover_progress(0.5)
            self.assertFalse(card.thumb.pixmap().isNull())
            card.leaveEvent(QEvent(QEvent.Type.Leave))
            self.assertFalse(card._hovered)
            self.assertEqual(card._hover_fade.endValue(), 0.0)

    def test_artwork_color_and_neutral_fallback(self):
        image = QImage(32, 32, QImage.Format.Format_ARGB32)
        image.fill(QColor("#e62a24"))
        accent = artwork_accent(image)
        self.assertTrue(accent.hue() < 30 or accent.hue() > 330)
        image.fill(QColor("#888888"))
        self.assertEqual(artwork_accent(image).name(), "#2dd4bf")

    def test_catalog_color_overrides_artwork_and_survives_missing_image(self):
        game = Game({"id": "test-purple", "name": "Test", "cardColor": "#9055bd"})
        card = GameCard(game, lambda _: None)
        image = QImage(32, 32, QImage.Format.Format_ARGB32)
        image.fill(QColor("red"))
        card._apply_image(image)
        self.assertEqual(card._accent.name(), "#9055bd")
        card._show_placeholder()
        self.assertEqual(card._accent.name(), "#9055bd")
        self.assertEqual(Game({"cardColor": "invalid"}).card_color, "")

    def test_launches_detected_executable_with_game_working_directory(self):
        with patch("main.default_game_directory", return_value=str(self.source)), \
             patch("main.load_prefs", return_value={}), patch("main.subprocess.Popen") as launch:
            self.window._launch_owned_game(self.game)
        launch.assert_called_once_with([str(self.folder / "game.exe")], cwd=str(self.folder))

    def test_gdk_game_opens_helper_instead_of_executable(self):
        helper = self.folder / 'GDK_Helper.bat'
        helper.touch()
        with patch('main.default_game_directory', return_value=str(self.source)), \
             patch('main.load_prefs', return_value={}), \
             patch.object(self.window, '_start_gdk_game') as setup, \
             patch('main.subprocess.Popen') as launch:
            self.window._launch_owned_game(self.game)
        setup.assert_called_once_with(self.game, helper.resolve())
        launch.assert_not_called()

    def test_gdk_worker_does_not_launch_when_helper_fails(self):
        events = []
        worker = GdkSetupWorker(str(self.folder / 'GDK_Helper.bat'))
        worker.result_ready.connect(lambda app_id, error: events.append((app_id, error)))
        with patch('main.run_gdk_helper', side_effect=RuntimeError('setup cancelled')), \
             patch('main.registered_gdk_app', side_effect=RuntimeError('not registered')) as query:
            worker.run()
        query.assert_called_once()
        self.assertEqual(events, [('', 'setup cancelled')])

    def test_gdk_worker_verifies_registration_after_helper(self):
        events = []
        worker = GdkSetupWorker(str(self.folder / 'GDK_Helper.bat'))
        worker.result_ready.connect(lambda app_id, error: events.append((app_id, error)))
        with patch('main.run_gdk_helper') as run, \
             patch('main.registered_gdk_app', side_effect=[RuntimeError('not registered'), 'Example_family!Game']) as query:
            worker.run()
        run.assert_called_once()
        self.assertEqual(query.call_count, 2)
        self.assertEqual(events, [('Example_family!Game', '')])

    def test_already_registered_gdk_game_does_not_repeat_setup(self):
        events = []
        worker = GdkSetupWorker(str(self.folder / 'GDK_Helper.bat'))
        worker.result_ready.connect(lambda app_id, error: events.append((app_id, error)))
        with patch('main.run_gdk_helper') as setup, \
             patch('main.registered_gdk_app', return_value='Example_family!Game'):
            worker.run()
        setup.assert_not_called()
        self.assertEqual(events, [('Example_family!Game', '')])

    def test_gdk_result_launches_registered_windows_app(self):
        with patch('main.os.startfile') as start:
            self.window._gdk_setup_result('Example_family!Game', '')
        start.assert_called_once_with(r'shell:AppsFolder\Example_family!Game')

    def test_gdk_error_does_not_launch_anything(self):
        with patch('main.os.startfile') as start, patch('main.QMessageBox.warning') as warning:
            self.window._gdk_setup_result('', 'registration failed')
        start.assert_not_called()
        warning.assert_called_once()

    def test_missing_executable_can_be_cancelled(self):
        (self.folder / "game.exe").unlink()
        with patch("main.default_game_directory", return_value=str(self.source)), \
             patch("main.load_prefs", return_value={}), \
             patch("main.QMessageBox.question", return_value=QMessageBox.StandardButton.No), \
             patch("main.subprocess.Popen") as launch:
            self.window._launch_owned_game(self.game)
        launch.assert_not_called()

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
