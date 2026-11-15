import os
import sys
import unittest
from unittest.mock import patch

if sys.platform != 'win32':
    raise unittest.SkipTest('Windows UI tests')

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialog, QWidget, QMainWindow
from main import Game, GameCard, InstallDialog, MainWindow, AnimatedStack, game_detail_art_urls


class CardEffectsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_artwork_candidates_and_fallback(self):
        game = Game({'id': 'x', 'name': 'Example', 'steamAppId': '123', 'thumbnail': 'https://example.invalid/art.jpg'})
        urls = game_detail_art_urls(game)
        self.assertTrue(urls[0].endswith('/123/library_hero.jpg'))
        self.assertTrue(urls[1].endswith('/123/header.jpg'))
        self.assertEqual(urls[2], game.thumbnail)
        with patch('main.InstallDialog._load_detail_art'):
            dialog = InstallDialog(game)
        image = QImage(20, 10, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.blue)
        dialog._detail_art_done(urls[2], image)
        self.assertFalse(dialog._detail_header.isHidden())
        self.assertTrue(dialog._detail_placeholder.isHidden())
        dialog.close()

    def test_click_is_not_duplicated_during_flash(self):
        game = Game({'id': 'x', 'name': 'Example', 'cardColor': '#8844bb'})
        opened = []
        with patch('main.ui_motion_enabled', return_value=True):
            card = GameCard(game, lambda selected: opened.append(selected.id))
            card.show()
            QTest.mouseClick(card, Qt.MouseButton.LeftButton)
            QTest.mouseClick(card, Qt.MouseButton.LeftButton)
            QTest.qWait(130)
        self.assertEqual(opened, ['x'])
        card.close()

    def test_embedded_details_routes_accept_and_back(self):
        class Host(QMainWindow):
            _show_game_details = MainWindow._show_game_details
            _resize_details_overlay = MainWindow._resize_details_overlay
            def __init__(self):
                super().__init__()
                self.setCentralWidget(QWidget())
                self.stack = AnimatedStack(self.centralWidget())
                self.content_page = QWidget()
                self.downloads_page = QWidget()
                self.stack.addWidget(self.content_page)
                self.stack.addWidget(self.downloads_page)
        for accepted in (False, True):
            host = Host()
            dialog = QDialog()
            host._show_game_details(dialog)
            self.assertEqual(dialog.windowFlags() & Qt.WindowType.Window, Qt.WindowType.Widget)
            self.assertIs(host.stack.currentWidget(), host.content_page)
            dialog.accept() if accepted else dialog.reject()
            self.assertIs(host.stack.currentWidget(), host.downloads_page if accepted else host.content_page)
            host.close()
