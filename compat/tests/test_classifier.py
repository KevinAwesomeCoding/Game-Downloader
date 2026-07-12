"""
test_classifier.py  (v2)
------------------------
Unit tests for the MacWarp compatibility classifier v2.
Covers all 5 tiers, riskLevel (Standard/High), confidence scoring
(High/Medium/Low), knownCompatibilityNotes override, and edge cases.

Run with:
    python -m pytest compat/tests/test_classifier.py -v
    # or from inside compat/tests/:
    python -m pytest test_classifier.py -v
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime

# Support running from project root, compat/, or compat/tests/
_HERE   = os.path.dirname(os.path.abspath(__file__))
_COMPAT = os.path.dirname(_HERE)
_ROOT   = os.path.dirname(_COMPAT)

for _path in [_ROOT, _COMPAT]:
    if _path not in sys.path:
        sys.path.insert(0, _path)

try:
    from compat.classifyCompatibility import classify_game, load_rules
except ModuleNotFoundError:
    from classifyCompatibility import classify_game, load_rules  # type: ignore[no-redef]

_RULES = load_rules(os.path.join(_COMPAT, "compatibilityRules.json"))


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _game(
    *,
    id: str = "test-game",
    name: str = "Test Game",
    description: str = "A test game.",
    version: str = "1.0",
    directx: str | None = None,
    gpu: str = "NVIDIA GTX 1060",
    os_req: str = "Windows 10",
    ram: str = "8 GB",
    storage: str = "4 GB",
    fix_url: str = "",
    anti_cheat: str | None = None,
    engine: str | None = None,
    notes: str | None = None,
    native_confirmed: bool = False,
    known_notes: str | None = None,
) -> dict:
    """Build a minimal game dict for testing."""
    reqs: dict = {"os": os_req, "gpu": gpu, "ram": ram, "storage": storage}
    if directx is not None:
        reqs["directx"] = directx

    g: dict = {
        "id": id,
        "name": name,
        "description": description,
        "version": version,
        "fixZipUrl": fix_url,
        "requirements": reqs,
    }
    if anti_cheat is not None:
        g["antiCheat"] = anti_cheat
    if engine is not None:
        g["engine"] = engine
    if notes is not None:
        g["notes"] = notes
    if native_confirmed:
        g["nativeBuildConfirmed"] = True
    if known_notes is not None:
        g["knownCompatibilityNotes"] = known_notes
    return g


# ===========================================================================
# Tier 0 — Native
# ===========================================================================

class TestNative(unittest.TestCase):
    """nativeBuildConfirmed = True short-circuits everything else."""

    def test_native_basic(self):
        """Confirmed native build → tier = Native, confidence = High."""
        game = _game(
            id="native-game",
            name="Some Game With Mac Build",
            native_confirmed=True,
        )
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Native")
        self.assertEqual(r["confidence"], "High")
        self.assertEqual(r["riskLevel"], "Standard")

    def test_native_beats_dx12(self):
        """Native flag overrides DX12 Partial signals."""
        game = _game(
            id="native-dx12",
            directx="Version 12",
            native_confirmed=True,
        )
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Native")

    def test_native_beats_anticheat(self):
        """Native flag overrides anti-cheat Not yet signals."""
        game = _game(
            id="native-eac",
            anti_cheat="EasyAntiCheat",
            native_confirmed=True,
        )
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Native")

    def test_native_uses_known_notes(self):
        """When knownCompatibilityNotes is present, it becomes the reasoning."""
        game = _game(
            id="native-with-notes",
            native_confirmed=True,
            known_notes="Official macOS build on Steam (Intel + Rosetta 2 for M1).",
        )
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Native")
        self.assertIn("Rosetta 2", r["reasoning"])
        self.assertIn("Steam", r["reasoning"])

    def test_native_apple_silicon_note_injected(self):
        """Apple Silicon keyword in name → Rosetta 2 sub-note appended."""
        game = _game(
            id="native-m1",
            name="Cool Game",
            description="Supports Apple Silicon M1 natively.",
            native_confirmed=True,
        )
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Native")
        # Should mention Rosetta
        self.assertIn("Rosetta", r["reasoning"])

    def test_native_without_silicon_no_rosetta_note(self):
        """No Apple Silicon keyword → no Rosetta note injected."""
        game = _game(
            id="native-intel-only",
            name="Intel-only Mac Game",
            description="Has a native macOS build (Intel only).",
            native_confirmed=True,
        )
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Native")
        self.assertNotIn("Rosetta", r["reasoning"])


# ===========================================================================
# Tier 1 — Runs great
# ===========================================================================

class TestRunsGreat(unittest.TestCase):

    def test_dx9_explicit(self):
        game = _game(id="ktane", directx="Version 9", gpu="256MB DirectX 9", fix_url="")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Runs great")
        self.assertEqual(r["confidence"], "High")
        self.assertEqual(r["riskLevel"], "Standard")

    def test_dx10_explicit(self):
        game = _game(id="golf", directx="Version 10", fix_url="")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Runs great")
        self.assertEqual(r["confidence"], "High")

    def test_dx11_no_fix(self):
        game = _game(id="mage", directx="Version 11", fix_url="")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Runs great")

    def test_no_directx_full_release(self):
        """No DX field, full release → Runs great, Medium confidence."""
        game = _game(id="mimesis", gpu="NVIDIA GTX 1050 Ti 4GB", fix_url="", version="0.2.6")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Runs great")
        self.assertEqual(r["confidence"], "Medium")

    def test_no_directx_among_us(self):
        game = _game(id="among-us", gpu="Intel HD Graphics 4600", fix_url="", version="17.3I")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Runs great")


# ===========================================================================
# Tier 2 — Runs with a recipe
# ===========================================================================

class TestRunsWithRecipe(unittest.TestCase):

    def test_fix_zip_present(self):
        game = _game(id="liars-bar", directx="Version 11",
                     fix_url="https://files.catbox.moe/15p7h7.zip")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Runs with a recipe")
        self.assertEqual(r["riskLevel"], "Standard")

    def test_dotnet_framework(self):
        game = _game(id="dotnet-app",
                     description="Requires Microsoft .NET Framework 4.8",
                     directx="Version 9", fix_url="")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Runs with a recipe")
        self.assertEqual(r["confidence"], "High")

    def test_access_db(self):
        game = _game(id="access-app",
                     description="Uses Access Database backend.",
                     directx="Version 9", fix_url="")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Runs with a recipe")

    def test_vcredist(self):
        game = _game(id="vc-game",
                     description="Install vcredist_x64.exe first.",
                     directx="Version 11", fix_url="")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Runs with a recipe")

    def test_proton_ge_specific_build(self):
        """Explicit Proton-GE requirement → recipe."""
        game = _game(id="proton-ge-game",
                     description="Requires Proton-GE for mic-capture to work.",
                     directx="Version 11", fix_url="")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Runs with a recipe")
        self.assertEqual(r["confidence"], "High")


# ===========================================================================
# Tier 3 — Partial (Standard risk)
# ===========================================================================

class TestPartialStandard(unittest.TestCase):

    def test_dx12_explicit_no_risk_engine(self):
        """DX12 in directx field, no high-risk engine → Partial / Standard."""
        game = _game(id="cut-that-wire", directx="Version 12",
                     fix_url="https://example.com/fix.zip")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Partial")
        self.assertEqual(r["riskLevel"], "Standard")
        self.assertEqual(r["confidence"], "High")

    def test_dx12_knights_end(self):
        game = _game(id="knights-end", directx="Version 12",
                     fix_url="https://example.com/fix.zip")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Partial")
        self.assertEqual(r["riskLevel"], "Standard")

    def test_dx12_in_gpu_field(self):
        """'DirectX 12' substring in GPU field → Partial / Standard / Medium."""
        game = _game(id="dx12-gpu", directx="Version 11",
                     gpu="DirectX 12 compatible GPU required", fix_url="")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Partial")
        self.assertEqual(r["riskLevel"], "Standard")
        self.assertEqual(r["confidence"], "Medium")

    def test_vulkan_with_dx_standard(self):
        """Vulkan + DX both present, no high-risk engine → Partial / Standard."""
        game = _game(id="vulkan-dx",
                     description="Supports DirectX 11 and Vulkan.",
                     directx="Version 11", fix_url="")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Partial")
        self.assertEqual(r["riskLevel"], "Standard")


# ===========================================================================
# Tier 3 — Partial (High risk)
# ===========================================================================

class TestPartialHighRisk(unittest.TestCase):

    def test_dx12_plus_cryengine(self):
        """DX12 + CryEngine → Partial, riskLevel = High."""
        game = _game(id="cry-dx12", directx="Version 12",
                     engine="CryEngine", fix_url="")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Partial")
        self.assertEqual(r["riskLevel"], "High")
        self.assertEqual(r["confidence"], "High")

    def test_dx12_plus_idtech7(self):
        """DX12 + id Tech 7 engine → High risk."""
        game = _game(id="idtech7-game",
                     description="Powered by id Tech 7 engine.",
                     directx="Version 12", fix_url="")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Partial")
        self.assertEqual(r["riskLevel"], "High")

    def test_vulkan_plus_publisher_disclaimer(self):
        """Vulkan + DX + publisher says 'poor Vulkan compatibility' → High risk."""
        game = _game(id="poor-vulkan",
                     description="Note: poor Vulkan compatibility on macOS.",
                     directx="Version 11", fix_url="")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Partial")
        self.assertEqual(r["riskLevel"], "High")

    def test_dx12_plus_known_notes_risk(self):
        """knownCompatibilityNotes contains 'poor macOS' → riskLevel High on Partial."""
        game = _game(id="noted-partial",
                     directx="Version 12",
                     known_notes="Developer confirmed poor macOS Vulkan support.",
                     fix_url="")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Partial")
        self.assertEqual(r["riskLevel"], "High")
        self.assertEqual(r["confidence"], "High")
        self.assertIn("poor macOS", r["reasoning"])

    def test_dx12_no_risk_signals(self):
        """Explicit DX12, no risk signals → Standard (not High)."""
        game = _game(id="clean-dx12", directx="Version 12", fix_url="")
        r = classify_game(game, _RULES)
        self.assertEqual(r["riskLevel"], "Standard")


# ===========================================================================
# Tier 4 — Not yet
# ===========================================================================

class TestNotYet(unittest.TestCase):

    def test_vulkan_only_no_dx(self):
        """Buckshot Roulette profile — GPU 'Vulkan support required', no DX."""
        game = _game(id="buckshot", gpu="Vulkan support required", fix_url="")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Not yet")
        self.assertEqual(r["confidence"], "High")

    def test_easyanticheat(self):
        game = _game(id="eac-game", directx="Version 11",
                     description="Protected by EasyAntiCheat.", fix_url="")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Not yet")

    def test_battleye(self):
        game = _game(id="be-game", anti_cheat="BattlEye", directx="Version 11")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Not yet")

    def test_vanguard(self):
        game = _game(id="vanguard-game",
                     description="Uses Vanguard anti-cheat at the kernel level.",
                     directx="Version 11")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Not yet")

    def test_winrt(self):
        game = _game(id="uwp-game",
                     description="A UWP application from the Microsoft Store.",
                     directx="Version 12")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Not yet")

    def test_msix(self):
        game = _game(id="msix-app",
                     description="Distributed as MSIX from Windows Store.")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Not yet")

    def test_desktop_customizer(self):
        game = _game(id="taskbar-tool",
                     name="Ultimate Taskbar Enhancer",
                     description="Extends the Windows taskbar with extra icons.")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Not yet")
        self.assertIn("desktop", r["reasoning"].lower())
        self.assertIn("macos", r["reasoning"].lower())

    def test_opengl4(self):
        game = _game(id="ogl4",
                     description="Requires OpenGL 4.5 for rendering.",
                     gpu="Any GPU with OpenGL 4.5 support")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Not yet")

    def test_kernel_driver(self):
        game = _game(id="kd-game",
                     description="Installs a kernel-mode driver for telemetry.")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Not yet")

    def test_not_yet_riskLevel_is_standard(self):
        """Not yet entries always have riskLevel = Standard."""
        game = _game(id="eac2", directx="Version 11",
                     description="EasyAntiCheat protected.")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Not yet")
        self.assertEqual(r["riskLevel"], "Standard")


# ===========================================================================
# Confidence scoring
# ===========================================================================

class TestConfidenceScoring(unittest.TestCase):

    def test_high_confidence_dx_explicit(self):
        """Explicit DX9 → High regardless of release status."""
        game = _game(id="high-dx9", directx="Version 9", version="1.0", fix_url="")
        r = classify_game(game, _RULES)
        self.assertEqual(r["confidence"], "High")

    def test_medium_confidence_engine_known(self):
        """No DX, full release, Unity engine detected → Medium."""
        game = _game(id="unity-full",
                     description="Built with Unity Engine.",
                     version="2.0", fix_url="")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Runs great")
        self.assertEqual(r["confidence"], "Medium")

    def test_low_confidence_early_access_no_signals(self):
        """Early Access, no DX, no engine, no fix → Low confidence."""
        game = _game(
            id="ea-game",
            name="Unknown Early Access",
            description="An Early Access title.",
            version="Early Access",
            gpu="Unknown GPU",
            fix_url="",
        )
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Runs great")
        self.assertEqual(r["confidence"], "Low")

    def test_low_confidence_early_access_in_description(self):
        """'Early Access' in description → Low when no engine or fix."""
        game = _game(
            id="ea-desc",
            description="This is an Early Access game. Expect bugs.",
            version="0.1",
            fix_url="",
        )
        r = classify_game(game, _RULES)
        self.assertEqual(r["confidence"], "Low")

    def test_early_access_with_engine_stays_medium(self):
        """Early Access but Unreal Engine detected → stays Medium, not Low."""
        game = _game(
            id="ue5-ea",
            description="Built with Unreal Engine 5. Early Access.",
            version="Early Access",
            fix_url="",
        )
        r = classify_game(game, _RULES)
        self.assertEqual(r["confidence"], "Medium")

    def test_early_access_with_fix_stays_medium(self):
        """Early Access + fixZipUrl → recipe tier, Medium confidence."""
        game = _game(
            id="ea-fix",
            version="Early Access",
            directx="Version 11",
            fix_url="https://example.com/fix.zip",
        )
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Runs with a recipe")
        self.assertEqual(r["confidence"], "Medium")

    def test_known_notes_forces_high(self):
        """knownCompatibilityNotes present → confidence = High regardless of EA."""
        game = _game(
            id="noted-ea",
            version="Early Access",
            known_notes="CrossOver 24 report: runs well with DX11 path.",
            fix_url="",
        )
        r = classify_game(game, _RULES)
        self.assertEqual(r["confidence"], "High")
        self.assertIn("CrossOver", r["reasoning"])

    def test_full_release_no_dx_no_engine_medium(self):
        """Full release, no DX, no engine → Medium (not Low)."""
        game = _game(
            id="full-no-signals",
            version="2.3",
            fix_url="",
        )
        r = classify_game(game, _RULES)
        self.assertEqual(r["confidence"], "Medium")


# ===========================================================================
# knownCompatibilityNotes override
# ===========================================================================

class TestKnownNotes(unittest.TestCase):

    def test_notes_override_reasoning(self):
        """knownCompatibilityNotes replaces auto-generated reasoning."""
        game = _game(
            id="noted-game",
            directx="Version 11",
            known_notes="ProtonDB Gold: runs with minor graphical glitches.",
            fix_url="",
        )
        r = classify_game(game, _RULES)
        self.assertEqual(r["reasoning"], "ProtonDB Gold: runs with minor graphical glitches.")
        self.assertEqual(r["confidence"], "High")

    def test_notes_do_not_change_tier(self):
        """knownCompatibilityNotes should not change the auto-computed tier."""
        game = _game(
            id="noted-dx12",
            directx="Version 12",
            known_notes="DX12 required; game uses Partial rendering on Wine.",
            fix_url="",
        )
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Partial")   # still auto-computed

    def test_notes_on_not_yet_tier(self):
        """knownCompatibilityNotes + EAC → Not yet, reasoning from notes."""
        game = _game(
            id="noted-eac",
            description="EasyAntiCheat protected.",
            known_notes="EAC kernel driver confirmed — incompatible with Wine.",
            directx="Version 11",
            fix_url="",
        )
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Not yet")
        self.assertIn("EAC kernel driver", r["reasoning"])


# ===========================================================================
# Output schema / edge cases
# ===========================================================================

class TestOutputSchema(unittest.TestCase):

    def _assert_schema(self, result: dict):
        for key in ("tier", "riskLevel", "reasoning", "confidence", "classifiedAt"):
            self.assertIn(key, result, f"Missing field: {key}")
        self.assertIn(result["tier"],
                      {"Native", "Runs great", "Runs with a recipe", "Partial", "Not yet"})
        self.assertIn(result["riskLevel"], {"Standard", "High"})
        self.assertIn(result["confidence"], {"High", "Medium", "Low"})
        # classifiedAt must be parseable ISO-8601
        dt_str = result["classifiedAt"].replace("Z", "+00:00")
        datetime.fromisoformat(dt_str)

    def test_schema_runs_great(self):
        self._assert_schema(classify_game(
            _game(id="s1", directx="Version 9", fix_url=""), _RULES))

    def test_schema_recipe(self):
        self._assert_schema(classify_game(
            _game(id="s2", fix_url="https://x.com/fix.zip"), _RULES))

    def test_schema_partial_standard(self):
        self._assert_schema(classify_game(
            _game(id="s3", directx="Version 12", fix_url=""), _RULES))

    def test_schema_partial_high(self):
        self._assert_schema(classify_game(
            _game(id="s4", directx="Version 12", engine="CryEngine", fix_url=""), _RULES))

    def test_schema_not_yet(self):
        self._assert_schema(classify_game(
            _game(id="s5", gpu="Vulkan support required", fix_url=""), _RULES))

    def test_schema_native(self):
        self._assert_schema(classify_game(
            _game(id="s6", native_confirmed=True), _RULES))

    def test_empty_game_no_crash(self):
        r = classify_game({}, _RULES)
        self._assert_schema(r)

    def test_missing_requirements_no_crash(self):
        r = classify_game({"id": "bare", "name": "Bare Game", "fixZipUrl": ""}, _RULES)
        self._assert_schema(r)

    def test_priority_native_over_all(self):
        """Native flag overrides every other signal."""
        game = _game(id="pri1", directx="Version 12",
                     description="EasyAntiCheat protected.",
                     native_confirmed=True)
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Native")

    def test_priority_not_yet_over_partial(self):
        """Anti-cheat + DX12 → Not yet wins over Partial."""
        game = _game(id="pri2", directx="Version 12",
                     description="EasyAntiCheat protected.")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Not yet")

    def test_priority_partial_over_recipe(self):
        """DX12 + fixZipUrl → Partial wins over recipe."""
        game = _game(id="pri3", directx="Version 12",
                     fix_url="https://x.com/fix.zip")
        r = classify_game(game, _RULES)
        self.assertEqual(r["tier"], "Partial")

    def test_non_high_never_becomes_high_via_adjuster(self):
        """Confidence adjuster can only lower, never raise."""
        game = _game(id="adj1", fix_url="https://x.com/fix.zip",
                     version="Full Release")
        r = classify_game(game, _RULES)
        # fixZipUrl → recipe / Medium; adjuster should not raise it to High
        self.assertIn(r["confidence"], {"Medium", "Low"})

    def test_all_tier_riskLevel_standard_except_partial_high(self):
        """Native, Runs great, Recipe, Not yet, Partial-standard → riskLevel Standard."""
        fixtures = [
            _game(id="r1", native_confirmed=True),
            _game(id="r2", directx="Version 9", fix_url=""),
            _game(id="r3", fix_url="https://x.com/fix.zip"),
            _game(id="r4", gpu="Vulkan support required"),
            _game(id="r5", directx="Version 12"),   # Partial, Standard
        ]
        for g in fixtures:
            r = classify_game(g, _RULES)
            if r["tier"] == "Partial" and r["riskLevel"] == "High":
                self.fail(f"Unexpected High risk for {g['id']}: {r}")
            if r["tier"] != "Partial":
                self.assertEqual(r["riskLevel"], "Standard",
                                 f"Expected Standard for {g['id']}, got {r['riskLevel']}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
