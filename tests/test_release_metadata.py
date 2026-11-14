import json
import os
import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'ci' / 'publish_gitlab_release.py'


class Response:
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return b'{"tag_name":"v1.4-20261114-54"}'


class ReleaseMetadataTests(unittest.TestCase):
    def test_publisher_includes_history_and_installed_tag_mapping(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=root, text=True, stderr=subprocess.DEVNULL).strip()
            git('init')
            git('config', 'user.email', 'test@example.invalid')
            git('config', 'user.name', 'Test')
            (path / 'tracked').write_text('one')
            git('add', 'tracked')
            git('commit', '-m', 'Installed version')
            old = git('rev-parse', 'HEAD')
            git('tag', 'v1.4-20261101-1')
            (path / 'tracked').write_text('two')
            git('commit', '-am', 'New feature')
            new = git('rev-parse', 'HEAD')
            (path / 'dist').mkdir()
            for name in ('GameInstaller.zip', 'GameInstaller-windows-x64.exe', 'GameInstaller-macOS-arm64.zip'):
                (path / 'dist' / name).write_bytes(b'test')
            (path / 'games.json').write_text('{}')
            env = {'RELEASE_TAG': 'v1.4-20261114-54', 'RELEASE_VERSION': '1.4',
                   'CI_API_V4_URL': 'https://example.invalid/api/v4', 'CI_PROJECT_ID': '1',
                   'CI_PROJECT_URL': 'https://example.invalid/project', 'CI_COMMIT_SHA': new,
                   'CI_COMMIT_TITLE': 'New feature', 'CI_JOB_TOKEN': 'test'}
            previous = os.getcwd()
            try:
                os.chdir(root)
                with patch.dict(os.environ, env), patch('urllib.request.urlopen', return_value=Response()):
                    runpy.run_path(str(SCRIPT), run_name='__main__')
                data = json.loads((path / 'dist' / 'release.json').read_text())
            finally:
                os.chdir(previous)
            self.assertEqual(data['tag_commits']['v1.4-20261101-1'], old)
            self.assertEqual(data['commit_history'][0], {'sha': new, 'title': 'New feature'})


if __name__ == '__main__': unittest.main()
