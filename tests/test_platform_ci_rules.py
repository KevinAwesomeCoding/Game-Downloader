"""Regression checks for platform build paths using Git's path globbing."""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PlatformRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        text = (ROOT / '.gitlab-ci.yml').read_text(encoding='utf-8')
        cls.matches = {}
        for platform, next_job in [('windows', 'build_macos'), ('macos', 'publish_release')]:
            section = text.split(f'build_{platform}:', 1)[1].split(f'\n{next_job}:', 1)[0]
            changes = re.split(r'\n  (?=\S)', section.split('    - changes:\n', 1)[1], maxsplit=1)[0]
            patterns = [m.group(1).strip('"\'') for m in re.finditer(r'^        - (.+)$', changes, re.M)]
            if not patterns:
                raise AssertionError(f'No change patterns for {platform}')
            matches = set()
            for pattern in patterns:
                output = subprocess.check_output(['git', 'ls-files', '--', f':(glob){pattern}'], cwd=ROOT, text=True)
                matches.update(output.splitlines())
            cls.matches[platform] = matches

    def test_platform_only_and_shared_inputs(self):
        for path, expected in {
            'main.py': (True, False),
            'tests/test_card_effects.py': (True, False),
            'tests/test_owned_games_ui.py': (True, False),
            'tests/test_hosted_downloads.py': (True, True),
            'ci/build_windows.ps1': (True, False),
            'macos/GameDownloaderMac/Sources/GameDownloaderMac/Updater.swift': (False, True),
            'ci/build_macos.sh': (False, True),
            'download_support.py': (True, True),
            'host_resolver.py': (True, True),
            '.gitlab-ci.yml': (True, True),
        }.items():
            with self.subTest(path=path):
                self.assertEqual((path in self.matches['windows'], path in self.matches['macos']), expected)

    def test_windows_only_override_excludes_macos(self):
        text = (ROOT / '.gitlab-ci.yml').read_text(encoding='utf-8')
        windows = text.split('build_windows:', 1)[1].split('\nbuild_macos:', 1)[0]
        macos = text.split('build_macos:', 1)[1].split('\npublish_release:', 1)[0]
        override = '    - if: \'$WINDOWS_ONLY == "true" || ($CI_PIPELINE_SOURCE == "web" && $CI_COMMIT_TITLE =~ /\\[windows-only\\]/)\''
        self.assertIn(override + '\n', windows)
        self.assertIn(override + '\n      when: never', macos)

    def test_platform_builds_retry_once(self):
        text = (ROOT / '.gitlab-ci.yml').read_text(encoding='utf-8')
        for platform, next_job in [('windows', 'build_macos'), ('macos', 'publish_release')]:
            section = text.split(f'build_{platform}:', 1)[1].split(f'\n{next_job}:', 1)[0]
            with self.subTest(platform=platform):
                self.assertRegex(section, r'(?m)^  retry: 1$')

    def test_tracked_files_do_not_fall_through_both_platforms(self):
        tracked = set(subprocess.check_output(['git', 'ls-files'], cwd=ROOT, text=True).splitlines())
        missing = tracked - self.matches['windows'] - self.matches['macos']
        self.assertFalse(missing, f'Tracked paths without a build rule: {sorted(missing)}')


if __name__ == '__main__': unittest.main()
