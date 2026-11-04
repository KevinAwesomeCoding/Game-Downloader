import unittest
from unittest.mock import patch
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))


class InteractiveDownloadTests(unittest.TestCase):
    def test_supported_pages(self):
        # macOS CI does not install Playwright for the regular unit suite.
        try:
            from interactive_host_download import supported_page
        except ModuleNotFoundError as exc:
            if exc.name == 'playwright':
                self.skipTest('Playwright is not installed')
            raise
        for url in ('https://steamrip.com/peak-free-download/', 'https://bzzhr.to/example',
                    'https://itsfileditchfiles.st/example'):
            self.assertTrue(supported_page(url))
        for url in ('http://steamrip.com/example', 'https://steamrip.com.attacker.invalid/',
                    'https://name:secret@bzzhr.to/example', 'https://bzzhr.to:444/example'):
            self.assertFalse(supported_page(url))
