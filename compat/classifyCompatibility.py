"""
classifyCompatibility.py  (v2)
------------------------------
Pure classifier module for MacWarp's publish-time compatibility pipeline.

Usage (standalone):
    python classifyCompatibility.py --game-id gang-beasts --games games.json

Usage (as a module):
    from compat.classifyCompatibility import classify_game, load_rules
    rules = load_rules()
    result = classify_game(game_dict, rules)

Input:  a single game dict (as stored in games.json).
        Optional top-level fields that influence classification:
          - nativeBuildConfirmed  (bool)  — set manually by publisher; triggers "Native"
          - knownCompatibilityNotes (str) — manual note injected into reasoning; forces High confidence

Output: a macCompatibility dict:
    {
        "tier":         "Native" | "Runs great" | "Runs with a recipe" | "Partial" | "Not yet",
        "riskLevel":    "Standard" | "High",
        "reasoning":    "<short human-readable explanation>",
        "confidence":   "High" | "Medium" | "Low",
        "classifiedAt": "<ISO-8601 datetime string>"
    }

Classification priority (first match wins):
    1. Native          — nativeBuildConfirmed = true
    2. Not yet         — kernel anti-cheat, WinRT, desktop customizers, Vulkan-only, OpenGL 4.x
    3. Partial (High)  — DX12 / Vulkan + high-risk engine or publisher note
    4. Partial (Std)   — DX12 or Vulkan+DX, no high-risk signals
    5. Runs with a recipe — .NET, Access DB, runtime DLLs, fixZipUrl, specific Wine/Proton build
    6. Runs great      — DX 9/10/11, no blocking signals, or clean Win32 / no-DX app

Confidence scoring (post-hoc, applied after tier):
    - knownCompatibilityNotes present → always High
    - nativeBuildConfirmed            → always High
    - Anti-cheat / WinRT              → always High
    - Explicit DX field (great/partial) → High
    - Engine keyword detected         → Medium (if base would be Low)
    - Early Access + no strong signals → Low
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Rule loading
# ---------------------------------------------------------------------------

_RULES_PATH = os.path.join(os.path.dirname(__file__), "compatibilityRules.json")


def load_rules(rules_path: str = _RULES_PATH) -> dict:
    """Load the compatibility rules JSON from disk."""
    with open(rules_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Internal signal helpers
# ---------------------------------------------------------------------------

def _any_keyword(haystack: str, keywords: list[str]) -> str | None:
    """
    Return the first keyword found (case-insensitive) in *haystack*, or None.
    The returned string is the matched keyword (used in reasoning messages).
    """
    h = haystack.lower()
    for kw in keywords:
        if kw.lower() in h:
            return kw
    return None


def _collect_signals(game: dict) -> str:
    """
    Build one searchable string from all textual fields of the game entry
    (name, description, version, requirements values, plus extra metadata).
    knownCompatibilityNotes is intentionally included so its keywords
    contribute to risk-level and engine detection.
    """
    parts: list[str] = []

    for field in ("name", "description", "version", "knownCompatibilityNotes"):
        val = game.get(field)
        if isinstance(val, str):
            parts.append(val)

    reqs = game.get("requirements", {})
    if isinstance(reqs, dict):
        for v in reqs.values():
            if isinstance(v, str):
                parts.append(v)

    for field in ("antiCheat", "engine", "notes", "comment"):
        val = game.get(field)
        if isinstance(val, str):
            parts.append(val)

    return " ".join(parts)


def _get_directx(game: dict) -> str:
    """Return the directx requirement string, or empty string."""
    reqs = game.get("requirements", {})
    if isinstance(reqs, dict):
        return str(reqs.get("directx", "")).strip()
    return ""


def _get_gpu(game: dict) -> str:
    """Return the gpu requirement string, or empty string."""
    reqs = game.get("requirements", {})
    if isinstance(reqs, dict):
        return str(reqs.get("gpu", "")).strip()
    return ""


def _has_fix_url(game: dict) -> bool:
    """Return True if the game has a non-empty fixZipUrl."""
    for part_list in (game.get("fixZipUrl"), game.get("fix_parts")):
        if isinstance(part_list, str) and part_list.strip():
            return True
        if isinstance(part_list, list) and part_list:
            url = part_list[0].get("url", "") if isinstance(part_list[0], dict) else ""
            if url:
                return True
    return False


def _is_early_access(game: dict, rules: dict) -> bool:
    """Return True if the game appears to be Early Access / pre-release."""
    ea_keywords = rules.get("early_access_detection", {}).get("keywords", [])
    version = str(game.get("version", "")).strip()
    desc = str(game.get("description", "")).strip()
    for kw in ea_keywords:
        if kw.lower() in version.lower() or kw.lower() in desc.lower():
            return True
    return False


def _detect_engine(full_signal: str, rules: dict) -> str | None:
    """
    Return the first known-engine keyword found in the signal string, or None.
    Used for confidence scoring only — does not affect tier.
    """
    ed = rules.get("engine_detection", {})
    for key in ("unity_keywords", "unreal_keywords", "godot_keywords",
                "other_well_known_keywords"):
        kw = _any_keyword(full_signal, ed.get(key, []))
        if kw:
            return kw
    return None


# ---------------------------------------------------------------------------
# Tier checkers  (priority order: native → not_yet → partial → recipe → great)
# ---------------------------------------------------------------------------

def _check_native(game: dict, rules: dict) -> tuple[bool, str, str]:
    """
    Priority 1: check nativeBuildConfirmed flag.
    Returns (matched, reasoning, riskLevel_always_Standard).
    confidence is always "High" for native.
    """
    if not game.get("nativeBuildConfirmed", False):
        return False, "", ""

    notes = str(game.get("knownCompatibilityNotes", "")).strip()
    reasoning = notes if notes else "Official native macOS build confirmed."

    # Check if Apple Silicon / Rosetta 2 keywords appear in any field
    full = _collect_signals(game)
    silicon_kw = _any_keyword(
        full,
        rules.get("native", {}).get("apple_silicon_keywords", [])
    )
    if silicon_kw and "rosetta" not in reasoning.lower():
        reasoning += (
            f" Apple Silicon ({silicon_kw}) may require Rosetta 2 "
            f"if a native arm64 build is not available."
        )

    return True, reasoning, "High"


def _check_not_yet(
    full_signal: str, dx: str, gpu: str, rules: dict
) -> tuple[bool, str, str]:
    """
    Priority 2: blocking 'Not yet' conditions.
    Returns (matched, reasoning, confidence).
    """
    r = rules.get("not_yet", {})

    # Desktop / shell customizer
    kw = _any_keyword(full_signal, r.get("desktop_customizer_keywords", []))
    if kw:
        return True, (
            f"Nothing to modify — the desktop is macOS, not Windows "
            f"(detected: '{kw}')."
        ), "High"

    # Kernel-mode driver
    kw = _any_keyword(full_signal, r.get("kernel_driver_keywords", []))
    if kw:
        return True, (
            f"Requires kernel-mode driver ('{kw}') which Wine cannot load."
        ), "High"

    # Kernel anti-cheat
    kw = _any_keyword(full_signal, r.get("antiCheat", []))
    if kw:
        return True, (
            f"Uses '{kw}' kernel-level anti-cheat — not compatible with Wine/Mac."
        ), "High"

    # WinRT / UWP / MSIX
    kw = _any_keyword(full_signal, r.get("winrt_keywords", []))
    if kw:
        return True, (
            f"Requires WinRT/UWP runtime ('{kw}') — Wine has no WinRT support."
        ), "High"

    # OpenGL 4.x only (Wine on Mac exposes OpenGL 2.1)
    kw = _any_keyword(full_signal, r.get("opengl4_keywords", []))
    if kw:
        return True, (
            f"Requires '{kw}' — Wine on macOS exposes OpenGL 2.1 only."
        ), "High"

    # Vulkan-only renderer with no DirectX fallback
    kw = _any_keyword(gpu, r.get("vulkan_only_keywords", []))
    if kw and not dx:
        return True, (
            f"GPU requires Vulkan with no DirectX fallback ('{kw}') — "
            f"Wine's MoltenVK layer is not reliable enough for this title."
        ), "High"

    return False, "", ""


def _check_partial(
    full_signal: str, dx: str, gpu: str, rules: dict
) -> tuple[bool, str, str, str]:
    """
    Priority 3–4: Partial tier with riskLevel.
    Returns (matched, reasoning, confidence, riskLevel).
    """
    r_partial   = rules.get("partial", {})
    r_high_risk = rules.get("partial_high_risk", {})

    def _is_high_risk() -> bool:
        """True if any high-risk engine or publisher keyword is present."""
        for key in ("engine_keywords", "publisher_risk_keywords"):
            if _any_keyword(full_signal, r_high_risk.get(key, [])):
                return True
        return False

    # DX12 explicit in directx field
    kw = _any_keyword(dx, r_partial.get("directx", []))
    if kw:
        risk = "High" if _is_high_risk() else "Standard"
        risk_note = " Known high-risk engine/publisher — rendering issues are likely." if risk == "High" else ""
        return True, (
            f"Requires DirectX 12 — Wine's D3D12 translation is experimental; "
            f"game installs and runs but rendering quality varies.{risk_note}"
        ), "High", risk

    # DX12 mentioned in GPU description
    kw = _any_keyword(gpu, r_partial.get("directx", []))
    if kw:
        risk = "High" if _is_high_risk() else "Standard"
        risk_note = " Known high-risk engine/publisher — rendering issues are likely." if risk == "High" else ""
        return True, (
            f"GPU field mentions DirectX 12 compatibility — D3D12 via Wine is "
            f"experimental; core gameplay may work with caveats.{risk_note}"
        ), "Medium", risk

    # Vulkan alongside DirectX
    kw = _any_keyword(full_signal, r_partial.get("vulkan_keywords", []))
    if kw and dx:
        risk = "High" if _is_high_risk() else "Standard"
        risk_note = " Known high-risk engine/publisher — Vulkan path on macOS may fail." if risk == "High" else ""
        return True, (
            f"Uses Vulkan renderer alongside DirectX — Vulkan-path features may "
            f"not function; DirectX path should work.{risk_note}"
        ), "Medium", risk

    return False, "", "", "Standard"


def _check_recipe(
    game: dict, full_signal: str, rules: dict
) -> tuple[bool, str, str]:
    """
    Priority 5: 'Runs with a recipe' tier.
    Returns (matched, reasoning, confidence).
    """
    r = rules.get("runs_with_recipe", {})

    # .NET Framework
    kw = _any_keyword(full_signal, r.get("dotnet_keywords", []))
    if kw:
        return True, (
            f"Needs '{kw}' runtime — MacWarp installs it automatically via Winetricks."
        ), "High"

    # Access Database Engine
    kw = _any_keyword(full_signal, r.get("accessdb_keywords", []))
    if kw:
        return True, (
            f"Requires Microsoft Access / ACE OLE DB ('{kw}') — "
            f"MacWarp applies the Access Database Engine fix automatically."
        ), "High"

    # Other runtime DLLs / specific Wine/Proton builds
    kw = _any_keyword(full_signal, r.get("runtime_keywords", []))
    if kw:
        return True, (
            f"Requires runtime component '{kw}' — MacWarp installs it automatically."
        ), "High"

    # fixZipUrl present
    if _has_fix_url(game):
        return True, (
            "MacWarp applies an automatic fix/patch to resolve runtime "
            "dependencies before launching."
        ), "Medium"

    return False, "", ""


def _check_runs_great(dx: str, rules: dict) -> tuple[bool, str, str]:
    """
    Priority 6: 'Runs great' (always matches as the final fallback).
    Returns (matched, reasoning, confidence).
    """
    r_great = rules.get("directx_great", {})

    kw = _any_keyword(dx, r_great.get("versions", []))
    if kw:
        return True, (
            f"Uses DirectX {kw.replace('Version ', '')} — natively supported by Wine "
            f"on macOS via DXVK/D9VK. Expect full compatibility."
        ), "High"

    if not dx:
        return True, (
            "No DirectX requirement detected — likely a Win32/GDI application "
            "or uses an older graphics API fully supported by Wine."
        ), "Medium"

    return True, "No blocking compatibility signals detected.", "Low"


# ---------------------------------------------------------------------------
# Confidence post-processor
# ---------------------------------------------------------------------------

def _adjust_confidence(
    base_confidence: str,
    game: dict,
    full_signal: str,
    tier: str,
    rules: dict,
) -> str:
    """
    Post-hoc confidence adjuster.  Can only LOWER confidence — never raise it.
    The base_confidence from the tier checker is the ceiling.

    Rules (applied in order, first match that lowers wins):
    - nativeBuildConfirmed / knownCompatibilityNotes / not-yet signals  → forced High upstream
    - Early Access + no engine keyword + no fix url → Low (if base is Medium or below)
    - Engine keyword detected → Medium (prevents Low when base is Medium)

    The tier checkers already set High when they have strong signals, so
    this function mainly handles the Medium→Low demotion path.
    """
    # Never lower below what the tier checker already determined as High
    if base_confidence == "High":
        return "High"

    # knownCompatibilityNotes present → already locked to High by caller
    # (this branch shouldn't be reached, but guard anyway)
    if game.get("knownCompatibilityNotes", "").strip():
        return "High"

    engine_kw = _detect_engine(full_signal, rules)
    is_ea     = _is_early_access(game, rules)
    has_fix   = _has_fix_url(game)

    # Early Access with no engine knowledge and no fix → Low
    if is_ea and not engine_kw and not has_fix:
        return "Low"

    # Early Access with engine known → stay at Medium (engine gives us a baseline)
    if is_ea and engine_kw:
        return "Medium"

    return base_confidence


# ---------------------------------------------------------------------------
# Main public API
# ---------------------------------------------------------------------------

def classify_game(game: dict, rules: dict) -> dict:
    """
    Classify a single game entry and return a macCompatibility dict.

    Args:
        game:  A dict matching the games.json schema (v2).
        rules: The parsed compatibilityRules.json dict (from load_rules()).

    Returns:
        {
            "tier":         str,
            "riskLevel":    "Standard" | "High",
            "reasoning":    str,
            "confidence":   "High" | "Medium" | "Low",
            "classifiedAt": str   # ISO-8601
        }
    """
    full_signal = _collect_signals(game)
    dx          = _get_directx(game)
    gpu         = _get_gpu(game)

    risk_level  = "Standard"   # default; overridden by Partial checks
    confidence  = "Medium"     # default; overridden below

    # ── Priority 1: Native ──────────────────────────────────────────────────
    matched, reasoning, confidence = _check_native(game, rules)
    if matched:
        tier = "Native"

    else:
        # ── Priority 2: Not yet ─────────────────────────────────────────────
        matched, reasoning, confidence = _check_not_yet(full_signal, dx, gpu, rules)
        if matched:
            tier = "Not yet"

        else:
            # ── Priority 3–4: Partial ──────────────────────────────────────
            matched, reasoning, confidence, risk_level = _check_partial(
                full_signal, dx, gpu, rules
            )
            if matched:
                tier = "Partial"

            else:
                # ── Priority 5: Runs with a recipe ─────────────────────────
                matched, reasoning, confidence = _check_recipe(
                    game, full_signal, rules
                )
                if matched:
                    tier = "Runs with a recipe"

                else:
                    # ── Priority 6: Runs great ─────────────────────────────
                    _, reasoning, confidence = _check_runs_great(dx, rules)
                    tier = "Runs great"

    # ── knownCompatibilityNotes override ────────────────────────────────────
    # Inject the manual note into reasoning and lock confidence to High.
    # Tier is NOT overridden — it stays auto-computed from the rules above.
    notes = str(game.get("knownCompatibilityNotes", "")).strip()
    if notes:
        reasoning   = notes
        confidence  = "High"

    # ── Post-hoc confidence adjustment ──────────────────────────────────────
    # (only lowers, and only when knownCompatibilityNotes didn't lock to High)
    if not notes:
        confidence = _adjust_confidence(confidence, game, full_signal, tier, rules)

    classified_at = datetime.now(timezone.utc).astimezone().isoformat()

    return {
        "tier":         tier,
        "riskLevel":    risk_level,
        "reasoning":    reasoning,
        "confidence":   confidence,
        "classifiedAt": classified_at,
    }


# ---------------------------------------------------------------------------
# CLI entry point (for quick single-game testing)
# ---------------------------------------------------------------------------

def _cli():
    parser = argparse.ArgumentParser(
        description="Classify a single game from games.json by its id."
    )
    parser.add_argument("--game-id", required=True, help="The game id to classify.")
    parser.add_argument(
        "--games",
        default=os.path.join(os.path.dirname(__file__), "..", "games.json"),
        help="Path to games.json (default: ../games.json relative to this file).",
    )
    parser.add_argument(
        "--rules",
        default=_RULES_PATH,
        help="Path to compatibilityRules.json.",
    )
    args = parser.parse_args()

    with open(args.games, "r", encoding="utf-8") as fh:
        games = json.load(fh)

    game = next((g for g in games if g.get("id") == args.game_id), None)
    if game is None:
        print(
            f"ERROR: No game with id '{args.game_id}' found in {args.games}.",
            file=sys.stderr,
        )
        sys.exit(1)

    rules  = load_rules(args.rules)
    result = classify_game(game, rules)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
