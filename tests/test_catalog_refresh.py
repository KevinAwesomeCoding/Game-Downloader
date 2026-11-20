import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

if sys.platform != "win32":
    raise unittest.SkipTest("Windows catalog loader tests")

from main import CATALOG_AUTO_REFRESH_INTERVAL_MS, CONFIG, ManifestLoader, load_prefs


class CatalogRefreshTests(unittest.TestCase):
    def test_catalog_auto_refresh_interval_is_five_seconds(self):
        self.assertEqual(CATALOG_AUTO_REFRESH_INTERVAL_MS, 5_000)

    def test_old_defaults_migrate_but_custom_urls_are_preserved(self):
        with tempfile.TemporaryDirectory() as root, patch('main._prefs_dir', return_value=root):
            prefs = Path(root) / 'prefs.json'
            for url, expected in (
                ('https://gitlab.com/kevinanil-projects/Game-Downloader/-/raw/main/games.json', CONFIG['manifest_url']),
                ('https://example.com/my-games.json', 'https://example.com/my-games.json'),
            ):
                prefs.write_text(json.dumps({'catalog_url': url}), encoding='utf-8')
                self.assertEqual(load_prefs()['catalog_url'], expected)

    def test_repeated_refresh_ignores_old_cooldowns_and_keeps_saved_copy_on_failure(self):
        with tempfile.TemporaryDirectory() as root, patch('main.app_dir', return_value=root), patch('main._prefs_dir', return_value=root):
            Path(root, 'prefs.json').write_text(json.dumps({'manifest_next_check': 9999999999, 'manifest_last_check': 9999999999}), encoding='utf-8')
            first = json.dumps([{'id': 'one', 'name': 'First'}]).encode()
            second = json.dumps([{'id': 'two', 'name': 'Second'}]).encode()
            error = urllib.error.HTTPError(CONFIG['manifest_url'], 429, 'limited', {}, None)
            with patch('main.urllib.request.urlopen', side_effect=[io.BytesIO(first), io.BytesIO(second), error]) as fetch:
                observed = []
                for _ in range(3):
                    loader = ManifestLoader(CONFIG['manifest_url'])
                    loader.loaded.connect(lambda games, source: observed.append((games, source)))
                    loader.run()
            self.assertEqual(fetch.call_count, 3)
            self.assertEqual([games[0].id for games, _ in observed], ['one', 'two', 'two'])
            self.assertIn('online source unavailable', observed[-1][1])

    def test_unchanged_remote_catalog_uses_etag_without_reloading_games(self):
        with tempfile.TemporaryDirectory() as root, patch('main.app_dir', return_value=root), patch('main._prefs_dir', return_value=root):
            Path(root, 'prefs.json').write_text(json.dumps({
                'catalog_etag': '"catalog-v1"',
                'catalog_etag_url': CONFIG['manifest_url'],
            }), encoding='utf-8')
            response = urllib.error.HTTPError(CONFIG['manifest_url'], 304, 'not modified', {}, None)
            with patch('main.urllib.request.urlopen', side_effect=response) as fetch:
                loader = ManifestLoader(CONFIG['manifest_url'])
                unchanged = []
                loaded = []
                loader.unchanged.connect(lambda: unchanged.append(True))
                loader.loaded.connect(lambda games, source: loaded.append((games, source)))
                loader.run()

            request = fetch.call_args.args[0]
            self.assertEqual(request.get_header('If-none-match'), '"catalog-v1"')
            self.assertEqual(unchanged, [True])
            self.assertEqual(loaded, [])


if __name__ == '__main__':
    unittest.main()
