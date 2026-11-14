import sys
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

if sys.platform != 'win32':
    raise unittest.SkipTest('Windows installer tests')

from main import Game, InstallWorker, game_launch_executable


class UpdateStageTests(unittest.TestCase):
    def test_explicit_launcher_does_not_fall_back_to_wrong_exe(self):
        game = Game({'name': 'Forza', 'launchExecutable': 'forzahorizon6_loader.exe'})
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / 'Forza.exe').touch()
            self.assertIsNone(game_launch_executable(root, game))
            loader = Path(root) / 'forzahorizon6_loader.exe'
            loader.touch()
            self.assertEqual(Path(game_launch_executable(root, game)), loader)

    def test_update_is_applied_before_repair(self):
        game = Game({'id': 'fixture', 'name': 'Fixture', 'zipUrl': 'https://example.com/base',
                     'updateZipUrl': 'https://example.com/update', 'fixZipUrl': 'https://example.com/fix'})
        worker = InstallWorker(game, 'fixture-destination')
        order = []
        def download(parts, *args, **kwargs):
            order.append('download-' + kwargs['kind'])
            return kwargs['kind']
        def apply(archive, *args, **kwargs):
            order.append('apply-' + archive)
            return True
        with patch.object(worker, '_prepare_destination', return_value=True), \
             patch.object(worker, '_download_parts', side_effect=download), \
             patch.object(worker, '_new_temp_dir', return_value='staging'), \
             patch.object(worker, '_extract_archive', return_value=True), \
             patch.object(worker, '_find_content_root', return_value='staging'), \
             patch.object(worker, '_merge_tree', return_value=True), \
             patch.object(worker, '_apply_fix', side_effect=apply), \
             patch.object(worker, '_remove_parts'), patch.object(worker, '_finish_cleanup'), \
             patch.object(worker, '_purge_cache'):
            worker.run()
        self.assertEqual(order, ['download-main', 'download-game-update', 'apply-game-update', 'download-fix', 'apply-fix'])

    def test_download_and_extraction_use_destination_volume(self):
        game = Game({'id': 'fixture', 'name': 'Fixture',
                     'zipUrl': 'https://example.com/game.zip'})
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / 'games' / 'fixture'
            destination.mkdir(parents=True)
            worker = InstallWorker(game, str(destination))
            def download(url, path, *args, **kwargs):
                Path(path).write_bytes(b'archive')
                return True
            with patch.object(worker, '_download', side_effect=download):
                archive = worker._download_parts(game.zip_parts, 'Download',
                                                 worker.download_progress)
            staging = Path(worker._new_temp_dir())
            cache_root = destination.parent / '.gameinstaller-cache'
            self.assertTrue(Path(archive).is_relative_to(cache_root))
            self.assertTrue(staging.is_relative_to(cache_root))
            worker._cleanup_temp()
            self.assertFalse(staging.exists())
            self.assertTrue(Path(archive).exists())  # resumable until successful install
            worker._purge_cache()
            self.assertFalse(Path(archive).exists())


if __name__ == '__main__':
    unittest.main()
