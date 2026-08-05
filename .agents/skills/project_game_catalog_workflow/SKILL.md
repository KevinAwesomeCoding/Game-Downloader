---
name: project_game_catalog_workflow
description: End-to-end workflow for adding new games to games.json from raw Google Drive share links
metadata: 
  node_type: memory
  type: project
  originSessionId: 0837c1a1-5465-438c-955f-a0d32a6456d8
---

Recurring task: the user pastes one or more Google Drive "view?usp=sharing" links with no other labels — sometimes a whole batch at once, sometimes a single game/fix link one message at a time — and expects each to be identified, grouped by game, researched, and added as a full entry to games.json in [Game Downloader](D:\Users\Owner\Desktop\Game Downloader).

**Step 1 — Identify each file.** Drive share links don't expose the filename in the URL. Use WebFetch on the `view?usp=sharing` URL with a prompt like "What is the file name shown on this Google Drive preview page?" — the filename is embedded in the page title/metadata even though WebFetch can't download the actual bytes. If WebFetch returns a 401, the file isn't shared as "Anyone with the link" — tell the user and ask them to fix sharing and resend (don't guess or fabricate a filename).

**Step 2 — Parse and group filenames.** Observed naming conventions from this repo's uploader:
- `Game.Name.vX.Y-GROUP.partN.rar` or `Game.Name.Build.DDMMYYYY-GROUP.partN.rar` → one volume of a multi-part main archive; the game name is everything before the version/build tag, dots become spaces.
- Same pattern without `.partN` → a single-volume main archive.
- `GameName_Fix_Repair_Steam_Generic.rar` (spaces sometimes collapsed, no dots) → the fix/repair patch for that game, not a separate game.
Group all files that share a game name; order multi-part volumes by their partN number (part1 first).

**Step 3 — Convert links to direct-download form.** Every Drive file ID becomes:
`https://drive.usercontent.google.com/download?id={ID}&export=download&confirm=t`
For multi-part sets, join the converted URLs into **one comma-separated string** in part order for `zipUrl`/`fixZipUrl` — see [[feedback_multipart_archives]] for why this format works with `_normalize_parts` in main.py.

**Step 4 — Research each game.** WebSearch `"<Game Name>" game steam` to find its Steam store page, then WebFetch that store page asking for: description, minimum system requirements (os/cpu/ram/gpu/directx/storage), player count/co-op info, version or release status, and the header image URL (ask specifically for the akamai `store_item_assets/steam/apps/{id}/{hash}/header.jpg?t=...` URL — guessing the plain `cdn.cloudflare.steamstatic.com/.../header.jpg` pattern often 404s for newer titles).

**Step 5 — Verify the thumbnail resolves.** `curl -s -o /dev/null -w "%{http_code}"` on the header image URL before using it; must return 200.

**Step 6 — Write the entry.** Match the existing games.json schema exactly: `id` (kebab-case), `name`, `description`, `version`, `size`, `players`, `thumbnail`, `zipUrl`, `fixZipUrl`, `requirements` (see [[feedback_requirements_field]]), `macCompatibility`. Apply the macCompatibility heuristic seen consistently across existing entries:
- Requires DirectX 12 → tier "Partial", confidence "High", reasoning about Wine's experimental D3D12 translation.
- Requires DirectX 10/11 → tier "Runs with a recipe", confidence "Medium", reasoning about MacWarp's automatic fix/patch.
- No DirectX requirement listed at all → tier "Runs great", confidence "Medium", reasoning about no DirectX detected.
- GPU requires Vulkan with no DirectX fallback → tier "Not yet", confidence "High".
`classifiedAt` uses the current date/time in the same `YYYY-MM-DDTHH:MM:SS.ffffff-05:00` format as existing entries. Apply [[feedback_game_type_fix]] if an entry turns out to be fix-only (no zipUrl).

**Step 7 — Validate.** After every edit run `python -c "import json; json.load(open('games.json', encoding='utf-8'))"` to confirm the file is still valid JSON before reporting done.

**Step 8 — Git.** Only `git add`/`commit`/`push` when the user explicitly asks in that message — never proactively, even right after finishing the json edits. Commit message should name the games added; keep to `games.json` only (`git add games.json`, not `-A`).

**Why:** This sequence was worked out and confirmed across several rounds of adding games (Funnel Runners, then a batch of 5 more from 12 unlabeled Drive links) — the user wants the exact same process repeatable in a fresh session with no prior context.

**How to apply:** Follow this whenever the user drops Drive links (with or without saying which game they're for) and asks to add them to the catalog.

Always put these in the games.json files and before making changes, make sure you read the structure of games.json before procceding
