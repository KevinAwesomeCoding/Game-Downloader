import io
import json
import os
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'ci' / 'publish_gitlab_release.py'


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class PublisherTests(unittest.TestCase):
    def simulate(self, platforms, split=True, prior=None):
        uploads = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'dist').mkdir()
            (root / 'games.json').write_text('[]')
            for name in platforms:
                (root / 'dist' / name).write_bytes(b'fixture')
            environment = {'RELEASE_TAG': 'v1.4-20261114-20', 'RELEASE_VERSION': '1.4',
                           'CI_API_V4_URL': 'https://example.invalid/api/v4', 'CI_PROJECT_ID': '1',
                           'CI_PROJECT_URL': 'https://example.invalid/project', 'CI_COMMIT_SHA': 'abc',
                           'CI_JOB_TOKEN': 'test', 'PLATFORM_SPLIT_ENABLED': 'true' if split else 'false'}
            def fake_open(request, timeout=None):
                if request.get_method() == 'GET': return Response(json.dumps(prior).encode())
                uploads.append(request)
                return Response(b'{"tag_name":"ok"}')
            old = os.getcwd()
            try:
                os.chdir(root)
                with patch.dict(os.environ, environment), patch('urllib.request.urlopen', fake_open), patch('subprocess.check_output', return_value=''):
                    runpy.run_path(str(SCRIPT))
                return json.loads((root / 'dist' / 'release.json').read_text()), uploads
            finally:
                os.chdir(old)

    def test_migration_then_single_platform_releases(self):
        win = ['GameInstaller.zip', 'GameInstaller-windows-x64.exe']
        mac = ['GameInstaller-macOS-arm64.zip']
        previous, _ = self.simulate(win + mac, split=False)
        for build, unchanged in ((win, 'macos'), (mac, 'windows')):
            result, uploads = self.simulate(build, prior=previous)
            self.assertEqual(result['platforms'][unchanged], previous['platforms'][unchanged])
            old_names = {x['name'] for x in previous['platforms'][unchanged]['assets']}
            self.assertTrue(old_names.issubset({x['name'] for x in result['assets']}))
            payload = json.loads(uploads[-1].data)
            self.assertTrue(old_names.issubset({x['name'] for x in payload['assets']['links']}))
            checksums = next(x for x in uploads if x.full_url.endswith('/checksums.json'))
            self.assertTrue(old_names.issubset(json.loads(checksums.data)))

    def test_split_fails_closed_without_previous_metadata(self):
        with self.assertRaises(SystemExit):
            self.simulate(['GameInstaller-macOS-arm64.zip'], prior={})

    def test_split_rejects_invalid_carried_digest(self):
        previous, _ = self.simulate(['GameInstaller.zip', 'GameInstaller-windows-x64.exe',
                                     'GameInstaller-macOS-arm64.zip'], split=False)
        previous['platforms']['windows']['assets'][0]['digest'] = 'sha256:' + 'z' * 64
        with self.assertRaises(SystemExit):
            self.simulate(['GameInstaller-macOS-arm64.zip'], prior=previous)
