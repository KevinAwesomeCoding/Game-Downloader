# Highball prerequisite recipes

These CC0 recipe data files are derived from `gauthierpiarrette/highball-db`
commit `a8ef0485994e57408084cc1ec9642357f97ad91c` (2026-10-26):

- `steam.json` — `recipes/launchers/steam.json`
- `vcrun2022.json` — `recipes/tweaks/vcrun2022.json`

They are applied only to the bottle selected by the user, through the pinned
Highball CLI's supported `recipe apply` command. Steam sign-in remains manual.
The Steam recipe changes bottle-level sync settings and suggests DXMT; per-game
renderer settings are applied to the game's pin afterward. The Visual C++
runtime and its Wine DLL overrides are bottle-wide and therefore shared with
other programs in that bottle.
