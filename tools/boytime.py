from playwright.sync_api import sync_playwright
import time
import json
import urllib.parse

# All 47 game URLs from your list
game_urls = [
    "https://online-fix.me/games/arcade/16220-gang-beasts-po-seti.html",
    "https://online-fix.me/games/adventures/17920-mimesis-po-seti.html",
    "https://online-fix.me/games/arcade/16254-human-fall-flat-po-seti.html",
    "https://online-fix.me/games/adventures/18079-gamble-with-your-friends-online.html",
    "https://online-fix.me/games/officialservers/18138-meccha-chameleon-po-seti.html",
    "https://online-fix.me/games/shooter/16450-among-us-po-seti.html",
    "https://online-fix.me/games/simulator/18142-hardware-store-simulator-online.html",
    "https://online-fix.me/games/simulator/17601-liars-bar-po-seti.html",
    "https://online-fix.me/games/officialservers/17903-backseat-drivers-po-seti.html",
    "https://online-fix.me/games/horror/18063-night-shippers-po-seti.html",
    "https://online-fix.me/games/sandbox/18058-rewind-99-po-seti.html",
    "https://online-fix.me/games/officialservers/17940-cut-that-wire-po-seti.html",
    "https://online-fix.me/games/adventures/18088-golf-gambit-online.html",
    "https://online-fix.me/games/simulator/16412-keep-talking-and-nobody-explodes-po-seti.html",
    "https://online-fix.me/games/adventures/18066-knights-end-po-seti.html",
    "https://online-fix.me/games/fighting/17825-mage-arena-po-seti.html",
    "https://online-fix.me/games/officialservers/17924-the-jackbox-party-pack-11-po-seti.html",
    "https://online-fix.me/games/horror/17712-repo-po-seti.html",
    "https://online-fix.me/games/puzzles/17621-buckshot-roulette-po-seti.html",
    "https://online-fix.me/games/horror/17388-midnight-heist-po-seti.html",
    "https://online-fix.me/games/survival/17465-backrooms-rec-po-seti.html",
    "https://online-fix.me/games/survival/17365-lethal-company-po-seti.html",
    "https://online-fix.me/games/simulator/18133-airport-security-sucks-po-seti.html",
    "https://online-fix.me/games/adventures/16553-the-game-of-life-2-po-seti.html",
    "https://online-fix.me/games/adventures/18141-fears-to-fathom-scratch-creek-online.html",
    "https://online-fix.me/games/horror/18168-funnel-runners-po-seti.html",
    "https://online-fix.me/games/officialservers/18163-forest-escape-last-train-po-seti.html",
    "https://online-fix.me/games/horror/18154-the-backrooms-rescue-expedition-po-seti.html",
    "https://online-fix.me/games/simulator/18169-happys-humble-burger-cult-online.html",
    "https://online-fix.me/games/officialservers/18156-scamper-po-seti.html",
    "https://online-fix.me/games/adventures/18158-pyramidion-online.html",
    "https://online-fix.me/games/simulator/18173-shift-at-midnight-online.html",
    "https://online-fix.me/games/officialservers/18184-big-walk-po-seti.html",
    "https://online-fix.me/games/adventures/18278-party-animals-po-seti.html",
    "https://online-fix.me/games/sandbox/18274-happy-wheels-po-seti.html",
    "https://online-fix.me/games/simulator/18259-dub-together-po-seti.html",
    "https://online-fix.me/games/adventures/18205-how-to-fish-po-seti.html",
    "https://online-fix.me/games/arcade/18180-machine-party-po-seti.html",
    "https://online-fix.me/games/arcade/18215-mimic-party-po-seti.html",
    "https://online-fix.me/games/adventures/18223-bombanana-po-seti.html",
    "https://online-fix.me/games/simulator/18284-scam-with-your-friends-online.html",
    "https://online-fix.me/games/simulator/18236-how-to-train-your-cock-po-seti.html",
    "https://online-fix.me/games/horror/18282-lucid-cats-online.html",
    "https://online-fix.me/games/adventures/18283-mogged-looksmaxx-or-die-online.html",
    "https://online-fix.me/games/adventures/18275-needle-in-a-haystack-po-seti.html",
    "https://online-fix.me/games/arcade/18221-cheaters-table-po-seti.html",
    "https://online-fix.me/games/simulator/18241-supermarket-chaos-po-seti.html"
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
