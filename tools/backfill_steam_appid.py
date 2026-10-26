#!/usr/bin/env python3
"""Backfill Steam AppIDs from the thumbnail URLs in the game catalogs."""

import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
CATALOGS = (
    ROOT / "games.json",
    ROOT / "macos/GameDownloaderMac/Sources/GameDownloaderMac/Resources/games.json",
)
APP_ID_PATTERN = re.compile(r"/apps/([0-9]+)/")


def backfill(path):
    original = path.read_text(encoding="utf-8")
    entries = json.loads(original)
    if not isinstance(entries, list):
        raise ValueError(f"{path} must contain a JSON array.")

    updated = []
    for entry in entries:
        if not isinstance(entry, dict) or "id" not in entry:
            raise ValueError(f"{path} contains an entry without an id.")
        thumbnail = entry.get("thumbnail")
        match = APP_ID_PATTERN.search(thumbnail) if isinstance(thumbnail, str) else None
        app_id = match.group(1) if match else None

        ordered = {}
        for key, value in entry.items():
            if key == "steamAppId":
                continue
            ordered[key] = value
            if key == "id":
                ordered["steamAppId"] = app_id
        updated.append(ordered)

    serialized = json.dumps(updated, indent=2, ensure_ascii=False) + "\n"
    if serialized == original:
        return entries, False
    path.write_text(serialized, encoding="utf-8")
    return updated, True


def main():
    all_entries = None
    changed_paths = []
    for index, path in enumerate(CATALOGS):
        if index and not path.exists():
            continue
        entries, changed = backfill(path)
        if all_entries is None:
            all_entries = entries
        if changed:
            changed_paths.append(path.relative_to(ROOT))

    for entry in all_entries or []:
        app_id = entry["steamAppId"]
        flag = "  <-- NULL APPID" if app_id is None else ""
        print(f"{entry['id']} | {app_id if app_id is not None else 'null'}{flag}")

    if changed_paths:
        print("Updated: " + ", ".join(str(path) for path in changed_paths))
    else:
        print("No changes made.")

    null_ids = [entry["id"] for entry in all_entries or [] if entry["steamAppId"] is None]
    if null_ids:
        print("NULL APPID entries: " + ", ".join(null_ids))
    else:
        print("NULL APPID entries: none")


if __name__ == "__main__":
    main()
