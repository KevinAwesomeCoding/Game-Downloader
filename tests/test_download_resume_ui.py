import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if sys.platform != 'win32':
    raise unittest.SkipTest('Windows installer tests')

from PyQt6.QtWidgets import QApplication
from main import (Game, DownloadManager, DownloadsPage, cache_set_dir,
                  _resume_job_path, _save_resume_job)


class ResumeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_reopen_shows_paused_job_and_resume_queues_it(self):
        with tempfile.TemporaryDirectory() as root, patch('main._prefs_dir', return_value=root), patch('main.default_game_directory', return_value=''):
            game = Game({'id': 'fixture', 'name': 'Fixture', 'zipUrl': 'https://example.test/file.zip'})
            target = str(Path(root) / 'custom' / 'Fixture')
            _save_resume_job(game, target)
            manager = DownloadManager(DownloadsPage(), max_concurrent=0)
            manager.restore_interrupted([game])
            self.assertEqual(len(manager.cards), 1)
            card = manager.cards[0]
            self.assertEqual(card.state, 'paused')
            self.assertEqual(card.retry_btn.text(), 'Resume')
            card._retry()
            self.assertEqual(card.state, 'queued')
            self.assertTrue(Path(_resume_job_path(game.id, target)).exists())
            manager.remove_card(card)
            self.assertFalse(Path(_resume_job_path(game.id, target)).exists())

    def test_legacy_cache_and_changed_catalog(self):
        with tempfile.TemporaryDirectory() as root, patch('main._prefs_dir', return_value=str(Path(root) / 'prefs')), patch('main.default_game_directory', return_value=root):
            game = Game({'id': 'fixture', 'name': 'Fixture', 'zipUrl': 'https://example.test/a'})
            target = str(Path(root) / 'Fixture')
            folder = cache_set_dir(game.id, 'main', game.zip_parts, target)
            (Path(folder) / 'volume.part').write_bytes(b'partial')
            manager = DownloadManager(DownloadsPage(), max_concurrent=0)
            manager.restore_interrupted([game])
            self.assertEqual(len(manager.cards), 1)
            self.assertTrue(manager.cards[0].retry_btn.isEnabled())
            changed = Game({'id': 'fixture', 'name': 'Fixture', 'zipUrl': 'https://example.test/b'})
            reopened = DownloadManager(DownloadsPage(), max_concurrent=0)
            reopened.restore_interrupted([changed])
            self.assertEqual(len(reopened.cards), 1)
            self.assertFalse(reopened.cards[0].retry_btn.isEnabled())
            self.assertTrue((Path(folder) / 'volume.part').exists())

    def test_fix_stage_state_is_remembered(self):
        with tempfile.TemporaryDirectory() as root, patch('main._prefs_dir', return_value=root), patch('main.default_game_directory', return_value=''):
            game = Game({'id': 'fixture', 'name': 'Fixture', 'zipUrl': 'https://example.test/a', 'fixZipUrl': 'https://example.test/f'})
            target = str(Path(root) / 'Fixture')
            os.mkdir(target)
            _save_resume_job(game, target, main_done=True)
            manager = DownloadManager(DownloadsPage(), max_concurrent=0)
            manager.restore_interrupted([game])
            self.assertTrue(manager.cards[0]._main_done)
