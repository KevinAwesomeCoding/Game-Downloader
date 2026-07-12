"""
batchClassify.py
----------------
Batch runner for the MacWarp compatibility classifier.

Loops through every entry in games.json, classifies games that are missing
a `macCompatibility` field (or match the --force / --since criteria), injects
the result back into each entry, writes updated games.json, and appends a log
line to compat/classifyLog.jsonl.

Usage:
    # Classify only games that don't yet have macCompatibility
    python compat/batchClassify.py

    # Re-classify ALL games (use when rules change)
    python compat/batchClassify.py --force

    # Re-classify entries classified before a given date
    python compat/batchClassify.py --since 2026-01-01

    # Dry-run — print what would change, don't write anything
    python compat/batchClassify.py --dry-run

    # Use non-default paths
    python compat/batchClassify.py --games path/to/games.json --rules path/to/rules.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

# Support running from either the project root OR from inside compat/
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

# Allow `from compat.classifyCompatibility import ...` when running from root
# and `from classifyCompatibility import ...` when running from inside compat/
sys.path.insert(0, _ROOT)
try:
    from compat.classifyCompatibility import classify_game, load_rules
except ModuleNotFoundError:
    sys.path.insert(0, _HERE)
    from classifyCompatibility import classify_game, load_rules  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_GAMES = os.path.join(_ROOT, "games.json")
DEFAULT_RULES = os.path.join(_HERE, "compatibilityRules.json")
DEFAULT_LOG   = os.path.join(_HERE, "classifyLog.jsonl")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_iso(dt_str: str) -> datetime | None:
    """Parse an ISO-8601 datetime string. Returns None on failure."""
    if not dt_str:
        return None
    try:
        # Python 3.11+ handles 'Z' suffix; older versions need manual replace.
        cleaned = dt_str.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def _needs_classify(game: dict, force: bool, since_dt: datetime | None) -> bool:
    """
    Return True if this game should be (re-)classified.

    Rules:
    - force=True           → always True
    - no macCompatibility  → True (never classified)
    - since_dt set         → True if classifiedAt < since_dt
    """
    if force:
        return True

    compat = game.get("macCompatibility")
    if not isinstance(compat, dict):
        return True  # never classified

    if since_dt is not None:
        classified_at = _parse_iso(compat.get("classifiedAt", ""))
        if classified_at is None:
            return True  # corrupt / missing timestamp → re-classify
        # Make since_dt timezone-aware if classified_at is aware
        if classified_at.tzinfo is not None and since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)
        return classified_at < since_dt

    return False  # already classified, no --force or --since trigger


def _append_log(log_path: str, entry: dict) -> None:
    """Append a single JSONL line to the classification log."""
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Main batch runner
# ---------------------------------------------------------------------------

def batch_classify(
    games_path: str = DEFAULT_GAMES,
    rules_path: str = DEFAULT_RULES,
    log_path: str = DEFAULT_LOG,
    force: bool = False,
    since_dt: datetime | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """
    Classify / re-classify games in games.json.

    Returns a list of log entries for games that were (re-)classified.
    """
    # --- Load inputs --------------------------------------------------------
    with open(games_path, "r", encoding="utf-8") as fh:
        games: list[dict] = json.load(fh)

    rules = load_rules(rules_path)
    run_ts = datetime.now(timezone.utc).astimezone().isoformat()

    classified_log: list[dict] = []
    changed = 0

    print(f"[batch] Loaded {len(games)} games from {games_path}")
    print(f"[batch] Rules:  {rules_path}")
    print(f"[batch] Mode:   {'DRY RUN — no files will be written' if dry_run else 'LIVE'}")
    if force:
        print("[batch] --force: re-classifying ALL games")
    elif since_dt:
        print(f"[batch] --since: re-classifying entries older than {since_dt.date()}")
    print()

    # --- Classify each game -------------------------------------------------
    for game in games:
        game_id   = game.get("id", "<unknown>")
        game_name = game.get("name", game_id)

        if not _needs_classify(game, force, since_dt):
            existing_tier = game.get("macCompatibility", {}).get("tier", "?")
            print(f"  [skip]      {game_name:<40}  (already: {existing_tier})")
            continue

        old_tier = (game.get("macCompatibility") or {}).get("tier")
        result   = classify_game(game, rules)
        new_tier = result["tier"]

        if not dry_run:
            game["macCompatibility"] = result

        action = "reclassified" if old_tier else "classified"
        tier_change = f"{old_tier} → {new_tier}" if old_tier and old_tier != new_tier else new_tier

        print(
            f"  [{action:<12}] {game_name:<40}  "
            f"{tier_change:<30}  ({result['confidence']})"
        )

        log_entry = {
            "timestamp":  run_ts,
            "id":         game_id,
            "name":       game_name,
            "tier":       new_tier,
            "confidence": result["confidence"],
            "action":     action,
            "reasoning":  result["reasoning"],
        }
        if old_tier and old_tier != new_tier:
            log_entry["previousTier"] = old_tier

        classified_log.append(log_entry)
        changed += 1

    # --- Write outputs ------------------------------------------------------
    print()
    print(f"[batch] {changed} game(s) classified/updated, {len(games) - changed} skipped.")

    if dry_run:
        print("[batch] DRY RUN — games.json not written.")
        return classified_log

    if changed > 0:
        with open(games_path, "w", encoding="utf-8") as fh:
            json.dump(games, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print(f"[batch] games.json updated -> {games_path}")

        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        for entry in classified_log:
            _append_log(log_path, entry)
        print(f"[batch] {len(classified_log)} log entries appended -> {log_path}")
    else:
        print("[batch] Nothing to write.")

    return classified_log


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _cli():
    parser = argparse.ArgumentParser(
        description="Batch-classify all games in games.json with MacWarp compatibility tiers."
    )
    parser.add_argument(
        "--games",
        default=DEFAULT_GAMES,
        help=f"Path to games.json (default: {DEFAULT_GAMES})",
    )
    parser.add_argument(
        "--rules",
        default=DEFAULT_RULES,
        help=f"Path to compatibilityRules.json (default: {DEFAULT_RULES})",
    )
    parser.add_argument(
        "--log",
        default=DEFAULT_LOG,
        help=f"Path to classifyLog.jsonl (default: {DEFAULT_LOG})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-classify ALL games, even those already classified.",
    )
    parser.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        default=None,
        help="Re-classify entries with classifiedAt older than this date.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be classified without writing any files.",
    )
    args = parser.parse_args()

    since_dt: datetime | None = None
    if args.since:
        try:
            since_dt = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"ERROR: --since value '{args.since}' is not a valid date (YYYY-MM-DD).", file=sys.stderr)
            sys.exit(1)

    batch_classify(
        games_path=args.games,
        rules_path=args.rules,
        log_path=args.log,
        force=args.force,
        since_dt=since_dt,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    _cli()
