import unittest
from unittest.mock import Mock, patch

from tools.fetch_download_links import steamrip_links


class SteamripLinkTests(unittest.TestCase):
    @patch('tools.fetch_download_links.requests.get')
    def test_extracts_host_pages_and_rejects_lookalikes(self, get):
        get.return_value = Mock(status_code=200, text='''
          <a href="//bzzhr.to/example">Download</a>
          <a href="https://itsfileditchfiles.st/file/test.rar">Download</a>
          <a href="https://bzzhr.to.attacker.invalid/test">Download</a>
        ''')
        self.assertEqual(steamrip_links('https://steamrip.com/game/'), {
            'bzzhr': 'https://bzzhr.to/example',
            'fileditch': 'https://itsfileditchfiles.st/file/test.rar',
        })

    @patch('tools.fetch_download_links.requests.get')
    def test_redirect_is_not_followed(self, get):
        get.return_value = Mock(status_code=302, text='')
        with self.assertRaises(ValueError):
            steamrip_links('https://steamrip.com/game/')


if __name__ == '__main__':
    unittest.main()
