import unittest
import base64
from unittest.mock import Mock, patch

from tools.fetch_download_links import steamrip_links, gamebounty_links


class SteamripLinkTests(unittest.TestCase):
    @patch('tools.fetch_download_links.requests.get')
    def test_gamebounty_extracts_multiple_safe_host_links(self, get):
        def link(target):
            token = base64.urlsafe_b64encode(target.encode()).decode().rstrip('=')
            return f'<a href="https://gamebounty.world/api/dl/game/{token}">Download</a>'
        first = 'https://filesyard.com/f/example'
        second = 'https://vikingfile.com/f/example'
        get.return_value = Mock(status_code=200, text=link(first) + link(second) + link('https://vikingfile.com.attacker.invalid/file'))
        self.assertEqual(gamebounty_links('https://gamebounty.world/game'), {'filesyard': [first], 'vikingfile': [second]})
        get.assert_called_once_with('https://gamebounty.world/game', timeout=30, allow_redirects=False)

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
