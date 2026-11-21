import sys
import unittest
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from functools import partial
from pathlib import Path
from unittest.mock import patch

if sys.platform != 'win32':
    raise unittest.SkipTest('Windows installer tests')

from main import (Game, InstallWorker, UpdateDialog, game_launch_executable,
                  ensure_destination_volume, _cache_job_id, cache_set_dir)
from update_support import UpdateRelease
from PyQt6.QtWidgets import QApplication, QLabel


class UpdateStageTests(unittest.TestCase):
    def test_external_volume_cache_is_job_scoped_and_staging_is_local(self):
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / 'external' / 'Games' / 'fixture'
            destination.parent.mkdir(parents=True)
            game = Game({'id': 'external-fixture', 'name': 'External fixture',
                         'zipUrl': 'https://example.com/game.zip'})
            worker = InstallWorker(game, str(destination))
            def download(url, path, *args, **kwargs):
                Path(path).write_bytes(b'archive')
                return True
            with patch.object(worker, '_download', side_effect=download):
                archive = Path(worker._download_parts(game.zip_parts, 'Download',
                                                       worker.download_progress))
            staging = Path(worker._new_temp_dir())
            workspace = destination.parent / '.gameinstaller-cache' / _cache_job_id(game.id, destination)
            self.assertTrue(archive.is_relative_to(workspace))
            self.assertTrue(staging.is_relative_to(workspace))
            self.assertEqual(archive.read_bytes(), b'archive')
            worker._cleanup_temp()
            self.assertTrue(archive.exists())
            worker._purge_cache()

    def test_unavailable_volume_fails_without_internal_fallback(self):
        import string
        used = {Path.cwd().drive.upper(), Path(tempfile.gettempdir()).drive.upper()}
        missing_drive = next((letter + ':' for letter in string.ascii_uppercase
                              if letter + ':' not in used and not Path(letter + '\\').is_dir()), None)
        if missing_drive is None:
            self.skipTest('No disconnected drive letter is available for this fixture')
        unavailable = Path(missing_drive + '\\Games\\fixture')
        with self.assertRaisesRegex(OSError, 'unavailable'):
            ensure_destination_volume(str(unavailable))

    def test_legacy_cache_is_copied_and_verified_without_deleting_original(self):
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / 'external' / 'game'
            destination.parent.mkdir()
            parts = [{'url': 'https://example.com/archive.zip'}]
            urls = '|'.join(item['url'] for item in parts)
            import hashlib
            import os
            identity = os.path.normcase(os.path.abspath(destination))
            key = hashlib.sha1(f"fixture|main|{urls}|{identity}".encode('utf-8')).hexdigest()[:16]
            legacy = destination.parent / '.gameinstaller-cache' / key
            legacy.mkdir(parents=True)
            (legacy / 'main.archive.part').write_bytes(b'resume me')
            migrated = Path(cache_set_dir('fixture', 'main', parts, str(destination)))
            self.assertEqual((migrated / 'main.archive.part').read_bytes(), b'resume me')
            self.assertEqual((legacy / 'main.archive.part').read_bytes(), b'resume me')

    def test_read_only_and_low_space_volumes_fail_clearly(self):
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / 'game'
            with patch('main.tempfile.mkstemp', side_effect=PermissionError('read only')):
                with self.assertRaisesRegex(OSError, 'not writable'):
                    ensure_destination_volume(str(destination))
            with patch('main.shutil.disk_usage', return_value=type('Usage', (), {
                    'free': 0, 'total': 100, 'used': 100})()):
                with self.assertRaisesRegex(OSError, 'Free at least 1 GB'):
                    ensure_destination_volume(str(destination))

    def test_cached_hosted_file_reports_verification(self):
        with tempfile.TemporaryDirectory() as root:
            archive = Path(root) / 'cached.rar'
            archive.write_bytes(b'cached')
            worker = InstallWorker(Game({'name': 'Fixture'}), str(Path(root) / 'game'))
            statuses = []
            worker.status.connect(statuses.append)
            with patch('main.HostedDownloader') as downloader:
                self.assertTrue(worker._download('https://gofile.io/d/example', str(archive),
                                                 'Downloading game (part 1/30)', worker.download_progress))
                downloader.return_value.fetch.assert_called_once()
            self.assertTrue(any('Verifying cached file' in value for value in statuses))

    def test_update_dialog_displays_versions_and_commit(self):
        app = QApplication.instance() or QApplication([])
        release = UpdateRelease('v1.4-20261114-54', 'Generic download text', '', None, None,
                                commit_title='Fix startup', commit_sha='1234567890')
        dialog = UpdateDialog(release)
        text = '\n'.join(label.text() for label in dialog.findChildren(QLabel))
        self.assertIn('Installed:', text)
        self.assertIn('Available: v1.4-20261114-54', text)
        self.assertIn('November 14, 2026', text)
        self.assertIn('Fix startup (12345678)', text)
        self.assertNotIn('Generic download text', text)
        dialog.close()

    def test_real_multipart_download_uses_isolated_http_files(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            source = base / 'source'
            source.mkdir()
            (source / 'part1.zip').write_bytes(b'a' * 4096)
            (source / 'part2.zip').write_bytes(b'b' * 8192)
            class QuietHandler(SimpleHTTPRequestHandler):
                def log_message(self, *args):
                    pass
            server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(source)))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f'http://127.0.0.1:{server.server_port}'
                game = Game({'id': 'local-test', 'name': 'Local test',
                             'zipUrl': [url + '/part1.zip', url + '/part2.zip']})
                worker = InstallWorker(game, str(base / 'isolated-games' / 'local-test'))
                progress, eta = [], []
                worker.download_progress.connect(progress.append)
                worker.eta_text.connect(eta.append)
                archive = worker._download_parts(game.zip_parts, 'Downloading', worker.download_progress)
                self.assertEqual(Path(archive).read_bytes(), b'a' * 4096)
                self.assertEqual(Path(worker._last_part_paths[1]).read_bytes(), b'b' * 8192)
                self.assertIn(100, progress)
                self.assertTrue(any('Archive set:' in value for value in eta))
                worker._purge_cache()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_total_eta_uses_all_known_remaining_parts(self):
        worker = InstallWorker(Game({'name': 'Fixture'}), 'fixture-destination')
        messages = []
        worker.eta_text.connect(messages.append)
        worker._part_sizes = [100, 200, 300]
        worker._part_index = 0
        worker._transfer_eta(50, 100, 10)
        self.assertEqual(messages[-1], 'Part: 5s  |  Archive set: 55s')
        worker._part_cached = [0, 100, 300]
        worker._transfer_eta(50, 100, 10)
        self.assertEqual(messages[-1], 'Part: 5s  |  Archive set: 15s')
        worker._part_sizes[2] = None
        worker._transfer_eta(50, 100, 10)
        self.assertEqual(messages[-1], 'Part: 5s  |  Archive set: unknown')
        worker._part_index = 2
        worker._transfer_eta(0, None, 0)
        self.assertIn('Archive set: unknown', messages[-1])

    def test_gofile_sizes_are_probed_without_downloading_future_part(self):
        with tempfile.TemporaryDirectory() as root:
            game = Game({'id': 'probe-test', 'name': 'Probe',
                         'zipUrl': ['https://gofile.io/d/first', 'https://gofile.io/d/second']})
            worker = InstallWorker(game, str(Path(root) / 'game'))
            found = threading.Event()
            def probe(url, **kwargs):
                if url.endswith('/second'):
                    found.set()
                    return {'size': 300}
                return {'size': 100}
            def download(url, path, *args, **kwargs):
                self.assertTrue(found.wait(3))
                Path(path).write_bytes(b'x')
                return True
            with patch('main.resolve_source', side_effect=probe), patch.object(worker, '_download', side_effect=download):
                result = worker._download_parts(game.zip_parts, 'Downloading', worker.download_progress)
            self.assertIsNotNone(result)
            self.assertEqual(worker._part_sizes, [100, 300])
            worker._purge_cache()

    def test_direct_host_size_probe_rejects_html(self):
        with tempfile.TemporaryDirectory() as root:
            game = Game({'id': 'head-test', 'name': 'Probe', 'zipUrl': [
                'https://files.catbox.moe/one.zip', 'https://pixeldrain.com/api/file/two?download']})
            worker = InstallWorker(game, str(Path(root) / 'game'))
            finished = threading.Event()
            class Response:
                status_code = 200
                def __init__(self, content_type):
                    self.headers = {'Content-Length': '1234', 'Content-Type': content_type}
                def __enter__(self): return self
                def __exit__(self, *args): return False
            def head(url, **kwargs):
                if 'pixeldrain' in url:
                    finished.set()
                    return Response('text/html')
                return Response('application/zip')
            def download(url, path, *args, **kwargs):
                self.assertTrue(finished.wait(3))
                Path(path).write_bytes(b'x')
                return True
            with patch('main.requests.head', side_effect=head), patch.object(worker, '_download', side_effect=download):
                worker._download_parts(game.zip_parts, 'Downloading', worker.download_progress)
            self.assertEqual(worker._part_sizes, [1234, None])
            worker._purge_cache()

    def test_filekeeper_probe_reads_total_from_one_byte_range(self):
        with tempfile.TemporaryDirectory() as root:
            game = Game({'id': 'filekeeper-test', 'name': 'Probe', 'zipUrl': [
                'https://filekeeper.net/first', 'https://filekeeper.net/second']})
            worker = InstallWorker(game, str(Path(root) / 'game'))
            finished = threading.Event()
            class Response:
                status_code = 206
                headers = {'Content-Range': 'bytes 0-0/4321', 'Content-Type': 'application/octet-stream'}
                def __enter__(self): return self
                def __exit__(self, *args): return False
            class Session:
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def get(self, *args, **kwargs):
                    self_test.assertEqual(kwargs['headers']['Range'], 'bytes=0-0')
                    finished.set()
                    return Response()
            self_test = self
            def download(url, path, *args, **kwargs):
                self.assertTrue(finished.wait(3))
                Path(path).write_bytes(b'x')
                return True
            metadata = {'url': 'https://filekeeper.net/file', 'cookies': [], 'user_agent': 'test'}
            with patch('main.resolve_source', return_value=metadata), patch('main.requests.Session', Session), patch.object(worker, '_download', side_effect=download):
                worker._download_parts(game.zip_parts, 'Downloading', worker.download_progress)
            self.assertIn(4321, worker._part_sizes)
            worker._purge_cache()

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
