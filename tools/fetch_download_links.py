import argparse
from html.parser import HTMLParser
from pathlib import Path
import sys
import time
import json
import base64
import re
import urllib.parse
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host_resolver_client import provider_for


def gamebounty_links(url):
    """Extract public host-page links, without starting downloads."""
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != 'https' or parts.hostname not in ('gamebounty.world', 'www.gamebounty.world', 'mail.gamebounty.world')
            or parts.username or parts.password or parts.port not in (None, 443)):
        raise ValueError('Expected an HTTPS GameBounty game page.')
    response = requests.get(url, timeout=30, allow_redirects=False)
    response.raise_for_status()
    if response.status_code != 200:
        raise ValueError('GameBounty returned a redirect or unexpected response.')
    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.links = []
        def handle_starttag(self, tag, attrs):
            if tag.lower() == 'a':
                self.links.append(dict(attrs).get('href', ''))
    parser = Links()
    parser.feed(response.text)
    hosts = {'filesyard.com': 'filesyard', 'vikingfile.com': 'vikingfile',
             'itsfileditchfiles.st': 'fileditch', 'fileditchfiles.st': 'fileditch',
             'fileditchfiles.me': 'fileditch', 'gofile.io': 'gofile',
             'pixeldrain.com': 'pixeldrain', 'filekeeper.net': 'filekeeper',
             '0807.st': '0807'}
    result = {}
    for href in parser.links:
        link = urllib.parse.urlsplit(urllib.parse.urljoin(url, href))
        if (link.scheme != 'https' or link.hostname not in ('gamebounty.world', 'www.gamebounty.world', 'api.gamebounty.world', 'mail.gamebounty.world')
                or link.username or link.password or link.port not in (None, 443)):
            continue
        match = re.fullmatch(r'/api/dl/[^/]+/([A-Za-z0-9_-]+)', link.path)
        if not match:
            continue
        try:
            token = match.group(1)
            target = base64.b64decode(token + '=' * (-len(token) % 4), altchars=b'-_', validate=True).decode('utf-8')
            parsed = urllib.parse.urlsplit(target)
            provider = hosts.get(parsed.hostname)
            if (provider and parsed.scheme == 'https' and not parsed.username and not parsed.password
                    and parsed.port in (None, 443)):
                result.setdefault(provider, [])
                if target not in result[provider]:
                    result[provider].append(target)
        except (ValueError, UnicodeError):
            continue
    if not result:
        raise ValueError('No supported GameBounty host links found.')
    return result


def steamrip_links(url):
    """Return supported host page URLs from the public game page."""
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != 'https' or parts.hostname not in ('steamrip.com', 'www.steamrip.com')
            or parts.username or parts.password or parts.port not in (None, 443)):
        raise ValueError('Expected an HTTPS steamrip.com game page.')
    response = requests.get(url, timeout=30, allow_redirects=False)
    response.raise_for_status()
    if response.status_code != 200:
        raise ValueError('SteamRIP returned a redirect or unexpected response.')
    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.links = []
        def handle_starttag(self, tag, attrs):
            if tag.lower() == 'a':
                href = dict(attrs).get('href')
                if href:
                    self.links.append(urllib.parse.urljoin(url, href))
    parser = Links()
    parser.feed(response.text)
    result = {}
    for link in parser.links:
        parsed = urllib.parse.urlsplit(link)
        provider = provider_for(link)
        if (provider in ('bzzhr', 'fileditch') and parsed.scheme == 'https'
                and not parsed.username and not parsed.password and parsed.port in (None, 443)):
            result.setdefault(provider, link)
    if not result:
        raise ValueError('No supported host page links found; the page may require a browser challenge or use intermediary links.')
    return result

# All 47 game URLs from your list
game_urls = [
    "https://online-fix.me/games/sandbox/16274-stardew-valley-po-seti.html",
    "https://online-fix.me/games/adventures/17729-schedule-i-po-seti.html"
]

def run_scraper():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        page = context.new_page()

        with open("online_fix_hosters.txt", "w", encoding="utf-8") as f:
            f.write("=== ONLINE-FIX HOSTER LINKS ===\n\n")

            for index, url in enumerate(game_urls, 1):
                print(f"[{index}/47] Processing: {url}")
                f.write(f"Game {index}: {url}\n")

                try:
                    # 1. Go to the game page
                    page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    time.sleep(1)

                    # 2. Find the Hosters link
                    hoster_link = page.locator("a").filter(has_text="Hosters").first
                    if not hoster_link.is_visible():
                        hoster_link = page.locator("a").filter(has_text="Скачать").first

                    if hoster_link.is_visible():
                        hoster_url = hoster_link.get_attribute("href")

                        if hoster_url:
                            if not hoster_url.startswith("http"):
                                hoster_url = "https://online-fix.me" + hoster_url

                            # 3. Go to the Hosters page
                            page.goto(hoster_url, wait_until="domcontentloaded", timeout=15000)
                            time.sleep(1)

                            # 4. We don't need to click "Proceed" to get the data!
                            # The links are stored in the "data-links" attribute of the ".option" divs.
                            # We will extract them directly using JavaScript evaluation.

                            options_data = page.eval_on_selector_all(
                                ".option",
                                "elements => elements.map(e => e.getAttribute('data-links'))"
                            )

                            found_any = False
                            for data_str in options_data:
                                if not data_str:
                                    continue

                                # The data is an HTML-escaped JSON string. We need to unescape it.
                                # In Python, we can use html.unescape
                                import html
                                unescaped_json = html.unescape(data_str)

                                try:
                                    links_array = json.loads(unescaped_json)
                                    for link_data in links_array:
                                        direct_link = link_data.get("direct_link")
                                        file_name = link_data.get("file_name")

                                        if direct_link:
                                            output_str = f"  - {file_name}: {direct_link}"
                                            print(output_str)
                                            f.write(output_str + "\n")
                                            found_any = True
                                except json.JSONDecodeError as e:
                                    print(f"  - Failed to parse JSON: {e}")

                            if not found_any:
                                print("  - No links found in the data-links attributes.")
                                f.write("  - No links found.\n")
                        else:
                            print("  - Hosters link missing href.")
                    else:
                        print("  - Could not find 'Hosters' link on game page.")
                        f.write("  - Could not find 'Hosters' link.\n")

                except Exception as e:
                    error_msg = f"  - Error processing {url}: {str(e)[:100]}"
                    print(error_msg)
                    f.write(error_msg + "\n")

                f.write("\n")
                time.sleep(2)

        browser.close()
        print("=== DONE! Check online_fix_hosters.txt for results. ===")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Collect game host page links.')
    parser.add_argument('--steamrip', metavar='GAME_URL', help='Collect BZZHR and FileDitch page links')
    parser.add_argument('--gamebounty', metavar='GAME_URL', help='Collect supported GameBounty host links')
    args = parser.parse_args()
    if args.gamebounty:
        try:
            print(json.dumps(gamebounty_links(args.gamebounty), indent=2))
        except Exception as exc:
            print(f'Could not extract host page links: {exc}', file=sys.stderr)
            raise SystemExit(1)
    elif args.steamrip:
        try:
            print(json.dumps(steamrip_links(args.steamrip), indent=2))
        except Exception as exc:
            print(f'Could not extract host page links: {exc}', file=sys.stderr)
            raise SystemExit(1)
    else:
        run_scraper()
