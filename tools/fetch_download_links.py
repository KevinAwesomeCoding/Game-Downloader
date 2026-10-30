from playwright.sync_api import sync_playwright
import time
import json
import urllib.parse

# All 47 game URLs from your list
game_urls = [
    "https://online-fix.me/games/sandbox/16274-stardew-valley-po-seti.html",
    "https://online-fix.me/games/adventures/17729-schedule-i-po-seti.html"
]

def run_scraper():
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
    run_scraper()
