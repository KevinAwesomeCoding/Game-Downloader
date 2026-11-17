import unittest
from unittest.mock import Mock, patch

from tools.fetch_download_links import onlinefix_links


class OnlineFixLinksTests(unittest.TestCase):
    @patch('tools.fetch_download_links.requests.get')
    def test_extracts_host_listing(self, get):
        page = Mock(text='<a href="https://hosters.online-fix.me:2053/Test">Hosters</a>')
        listing = Mock(text='<div data-links="[{&quot;direct_link&quot;:&quot;https://gofile.io/d/abc&quot;,&quot;file_name&quot;:&quot;game.rar&quot;}]"></div>')
        get.side_effect = [page, listing]
        self.assertEqual(onlinefix_links('https://online-fix.me/games/test/1-example.html'),
                         [{'direct_link': 'https://gofile.io/d/abc', 'file_name': 'game.rar'}])

    def test_rejects_other_source(self):
        with self.assertRaises(ValueError):
            onlinefix_links('https://example.com/games/test')


if __name__ == '__main__':
    unittest.main()
