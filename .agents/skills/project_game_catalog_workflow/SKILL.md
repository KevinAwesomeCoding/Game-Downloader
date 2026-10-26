---
name: project_game_catalog_workflow
description: Add and maintain Game-Downloader catalog entries, including accurate Windows requirements and evidence-based macOS compatibility.
metadata:
  node_type: memory
  type: project
---

# Game catalog workflow

Use this workflow whenever adding or revising games in `games.json`. Read the root catalog and `macos/GameDownloaderMac/Sources/GameDownloaderMac/Resources/games.json` first. Preserve existing schema and ordering unless the user asks otherwise.

## 1. Identify and group files
- For Google Drive share links, inspect the preview page for the filename; if access is denied, ask for sharing to be fixed instead of guessing.
- Group multipart archives by game and order by part number. A `*_Fix_*` archive is a repair file, not a separate game.
- Convert source links only when adding or fixing links. When the user asks for metadata or wording, do not alter any download, repair, backup, thumbnail, or store URL.

## 2. Research factual fields
- Use the official Steam store/API page for the title’s current name, short description, Windows minimum requirements, player count, release state, and platform flags. Prefer official developer/publisher sources if Steam lacks a fact.
- `requirements` contains Windows minimum specs only. Add `requirementsPlatform: "Windows"` so the platform is explicit.
- Copy only requirements explicitly listed. Omit unknown values; never invent CPU, RAM, DirectX, GPU, or storage values. Do not turn a Vulkan requirement into a DirectX version. If Steam’s requirements are malformed, keep only fields that can be read reliably and note the omission.
- If Steam lists a native macOS build and publishes Mac requirements, store those separately as `macRequirements`. Native store support and running the Windows build through Highball are different facts.
- Write a concise, original description that preserves the game’s actual play loop and player count without adding unsupported claims. If a Steam summary and its detailed description conflict, do not guess a count; use a general player label or explain the conflict.

## 3. Set macOS compatibility from evidence
- Check `GAME-COMPATIBILITY-REPORT.md` and the saved notes in `~/Documents/Game-Downloader Mac compatibility/` before classifying. Apply the result to the tested build and setup, not to every Mac or future build.
- Use `Native` only when an official store/developer source lists a native macOS version. Explain the source and distinguish it from any Windows-build test.
- Use `Runs with a recipe` when a recorded Highball launch reached a usable game/menu; state the tested outcome and any setup needed. A menu or open window does not prove complete gameplay or online play.
- Use `Partial` for a verified launch with a material remaining failure, such as a crash before the menu or a reproducible rendering/network limitation. State what failed and what still worked.
- Use `Not yet` only for a repeatable failure on the recorded build/setup. Do not generalize a single failed attempt into universal incompatibility.
- Use `Unverified` when no reliable result exists, the test was blocked before launch, or the cause is unclear. A DirectX/Vulkan requirement, missing DirectX field, patch URL, empty friends list, or opened invite panel alone does not prove compatibility.
- `confidence` reflects evidence quality: High for an official native-platform listing or a user-confirmed outcome; Medium for a single observed launch; Low for unverified/inferred results. Put the exact observation and its limits in `reasoning`.
- Never infer that invites, joining, voice, saves, or full gameplay work just because a friends window or menu opened.
- Do not run `compat/batchClassify.py --force` over manually reviewed entries; its heuristic can replace evidence-based results.

## 4. Keep catalog copies and validate
- Preserve `zipUrl`, `fixZipUrl`, backup URLs, `thumbnail`, `id`, and `steamAppId` unless the request explicitly concerns those fields.
- Keep the root and bundled Mac catalog in sync byte-for-byte after catalog edits.
- Validate JSON parses, every ID is unique, all entries have the required core keys, URL/source values are unchanged for metadata-only work, and the bundled copy matches the root.
- Review a focused diff for invented claims and accidental URL changes. Do not commit or push unless the user asks.

## 5. Add new archive entries
- Convert each Drive file ID to `https://drive.usercontent.google.com/download?id={ID}&export=download&confirm=t` only when the user asks to add files; join multipart URLs in part order using the format already supported by the downloader.
- Get `steamAppId` from the official Steam page/asset URL; never guess it. Use `null` when there is no verified match.
- Verify a thumbnail URL before adding it. Record the game’s requirements and compatibility under the rules above, then validate both catalog copies.
