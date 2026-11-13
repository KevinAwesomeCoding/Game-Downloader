---
name: project_game_catalog_workflow
description: Add and maintain Game-Downloader catalog entries using fetch_download_links.py for automated link retrieval, accurate Windows requirements, and evidence-based macOS compatibility.
metadata:
  node_type: memory
  type: project
---

# Game catalog workflow

Use this workflow whenever adding or revising games in `games.json`. Read the root catalog and `macos/GameDownloaderMac/Sources/GameDownloaderMac/Resources/games.json` first. Append new games to the end of both catalogs. Preserve existing schema and the order of existing entries unless the user asks otherwise.

## 1. Link Acquisition & fetch_download_links.py Integration
- For new games or batch updates, use **`fetch_download_links.py`** to process and generate the required download and backup links.
- Apply the link-prioritization rules strictly based on the source type:
  - **Archive.org / Google Drive links**: Keep as the primary link (`zipUrl` / `fixZipUrl`), and assign the corresponding **GoFile** link as the secondary backup (`zipBackupUrl` / `fixBackupUrl`).
  - **Pixel Drain links**: Replace them entirely—use **GoFile** as the primary link (`zipUrl` / `fixZipUrl`) and **FileKeeper** (formatted as direct-download links via `https://filekeeper.net/{file_code}/{filename}`) as the secondary backup (`zipBackupUrl` / `fixBackupUrl`).
- Group multipart archives by game in part order. A `*_Fix_*` archive is a repair file, not a separate game.

## 2. Research factual fields
- Use the official Steam store/API page for the title’s current name, short description, Windows minimum requirements, player count, release state, and platform flags. Prefer official developer/publisher sources if Steam lacks a fact.
- `requirements` contains Windows minimum specs only. Add `requirementsPlatform: "Windows"` so the platform is explicit.
- Copy only requirements explicitly listed. Omit unknown values; never invent CPU, RAM, DirectX, GPU, or storage values. Do not turn a Vulkan requirement into a DirectX version. If Steam’s requirements are malformed, keep only fields that can be read reliably and note the omission.
- If Steam lists a native macOS build and publishes Mac requirements, store those separately as `macRequirements`. Native store support and running the Windows build through Highball are different facts.
- Write a concise, original description that preserves the game’s actual play loop and player count without adding unsupported claims.

## 3. Set macOS compatibility from evidence
- Check `GAME-COMPATIBILITY-REPORT.md` and the saved notes in `~/Documents/Game-Downloader Mac compatibility/` before classifying. Apply the result to the tested build and setup, not to every Mac or future build.
- Use `Native` only when an official store/developer source lists a native macOS version.
- Use `Runs with a recipe` when a recorded Highball launch reached a usable game/menu; state the tested outcome and setup needed.
- Use `Partial` for a verified launch with a material remaining failure.
- Use `Not yet` only for a repeatable failure on the recorded build/setup.
- Use `Unverified` when no reliable result exists or the test was blocked.
- `confidence` reflects evidence quality: High, Medium, or Low. Put exact observations in `reasoning`.

## 4. Keep catalog copies and validate
- Preserve `zipUrl`, `fixZipUrl`, backup URLs, `thumbnail`, `id`, and `steamAppId` unless updating via `fetch_download_links.py`.
- Keep the root and bundled Mac catalog in sync byte-for-byte after catalog edits.
- Validate JSON parses, every ID is unique, all entries have required core keys, and the bundled copy matches the root.
- Review a focused diff for invented claims and accidental URL changes. Do not commit or push unless the user asks.

## 5. Add new archive entries
- Run `fetch_download_links.py` to fetch, format, and structure the primary and backup URLs correctly into the entry.
- Get `steamAppId` from the official Steam page/asset URL; never guess it. Use `null` when there is no verified match.
- Verify a thumbnail URL before adding it. Record requirements and compatibility, then validate both catalog copies.
- Visually inspect the actual thumbnail when adding a game or changing its artwork, and choose a representative card color based on its overall visual identity. Set `cardColor` to a six-digit hex color such as `#9055bd` in both catalog copies. Do not rely solely on the runtime pixel sampler: a small saturated detail can outweigh the color that visually defines the artwork. Preserve a deliberate existing override unless the artwork changes or the user requests a different color. If the artwork cannot be inspected, omit a new override and let the app use its automatic color or fallback; do not guess.
