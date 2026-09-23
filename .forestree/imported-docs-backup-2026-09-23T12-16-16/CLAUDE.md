# PokeVerdict

Reads the Pokémon GO appraisal screen, solves exact IVs, applies Keep/Build/
Buddy/Trade rules, pushes a verdict. Personal tool, read-only, no game API.

## Commands

```bash
python3 tools/build_gamedata.py --fetch     # rebuild gamedata.sqlite
python3 tools/test_solver.py gamedata.sqlite  # property tests, 400-sample round trip
python3 tools/test_fixtures.py                # replays real screenshots
python3 tools/test_rules.py gamedata.sqlite   # rules engine unit tests
python3 tools/shortcut_server.py --selftest fixtures/shots/*.PNG
python3 tools/calibrate.py fixtures/shots/*.PNG  # measure bars and check geometry
```

Run all four test scripts before considering a change done. They pass on
`main`; a failure is a regression, not a flaky test.

Python deps: `pillow numpy pytesseract` plus the `tesseract-ocr` binary.

## Delivery path

**Shortcuts** (`tools/shortcut_server.py`, `SHORTCUTS.md`) — no code signing.
Screenshot → POST → verdict. Start with `run.sh` or
`python3 tools/shortcut_server.py --collection collection.sqlite`.
Web UI at `GET /` shows the appraised collection as a sortable table.

## Non-obvious facts, all verified — do not "correct" these

- **Pokémon max at level 50.** The CPM table in game_master runs to 55, but
  50.5/51 are Best-Buddy-only and 55 exists solely for team leaders in Master
  League Battle Training. `build_gamedata.py` caps at 51. Searching to 55
  produces phantom candidates. This was a real bug, fixed.
- **HP is displayed "current / max"** (e.g. `81 / 136 HP` on a damaged
  Pokémon). The solver needs max. Taking the first number silently corrupts
  IVs for anything that has been in a raid or gym. Pinned by
  `testCurrentHPIsRejected` and a case in `test_fixtures.py`.
- **Appraisal badge stars are 0–3, not 1–4.** Bands: 0–22 / 23–29 / 30–36 /
  37–45. Confirmed against screenshots where totals of 27 and 28 both show one
  star. Was off by one across the whole range.
- **CP is derived, not OCR'd.** CP is white text over an arbitrary photographic
  background; tesseract returns nothing on bright ones. Given species +
  stamina IV + max HP, the level is pinned directly (uniquely 54.5% of the
  time) and CP follows. When OCR does read CP it is a cross-check, never an
  input. **IVs and verdicts never depend on CP.**
- **Bars are measured, not read.** Three visual segments with gaps, but fill
  maps linearly across the whole track, so the rightmost saturated pixel is
  enough. Colour varies by tier (orange low, pink at 3 stars) — key on
  saturation, not hue. Reference shots land within 0.03 of an integer.
- **`POWERUP_BANDS` in `build_gamedata.py` is approximate.** Sources disagree
  on the 40→50 stardust total (250k vs 296k). Only used to narrow the level
  search; the solver is correct without it. Never surface these as fact.
- **OCR species lookup goes through `best_species_match()` in `solver_ref.py`,
  not `species()`.** Three-step resolution: exact → exact after glyph
  substitution (`! | 1 → l`, `0 → o`, `5 $ → s`, curly apostrophes → `'`) →
  Levenshtein edit distance. Budget: 1 for names ≤ 5 chars, 2 for longer. On
  a tie the function raises rather than guessing. `species()` is still the
  stable exact-match path; don't reroute callers that need exact semantics.
- **Raw IV total is the wrong metric for PvP.** `GameData.pvp_rank()` scores
  all 4096 IV combos by stat product (effective ATK × effective DEF ×
  floor(effective STA)) at the highest level where CP ≤ the league cap.
  Azumarill 0/15/15 is GL rank 1; 15/15/15 is ~rank 2558. Pinned by
  `test_pvp_rank` in `test_solver.py`.
- **Flags are `None`, not `False`.** `isShiny / isShadow / isPurified /
  isLucky / isCostume / sizeClass` all return `None` (unknown) until pixel
  detection is implemented. Rules check `is True`, never truthiness, so
  `None` is correctly ignored. Do not change `None` to `False` — it would
  silently suppress future detection.
- **`make_rules_fn` returns `{verdict, suggestedTag, reasons}`.** `reasons`
  is a list — all fired rules, not just the winning one. `verdict` is the
  strongest that fired (`KEEP > BUILD > BUDDY > TRADE > TRANSFER > UNDECIDED`).
  `suggestedTag` priority: Build > Buddy > Keep; only set for KEEP/BUDDY
  verdicts. The fallback is TRANSFER (not UNDECIDED) — if nothing in the meta
  list matches and the Pokémon is not a hundo, there is no reason to hold it.
- **High IVs alone do not KEEP.** Only a perfect 15/15/15 (hundo, 45/45) earns
  a KEEP verdict on its own. A 44/45 non-meta Pokémon is still TRANSFER.
  The 82%+ (3-star, 37/45) threshold from the appraisal badge is the
  `transferIVCeiling` — anything below it gets an immediate TRANSFER before any
  other rule fires, but for meta species (purposeRules BUILD) BUILD strength
  wins so the low-IV specimen is still flagged BUILD, not TRANSFER.
- **`purposeRules` in `rules.json` is the primary meta list.** Each entry has
  `{species, purposes, verdict}`. Family matching means only one member of an
  evolutionary line needs to be listed. The `buildSpecies`/`keepSpecies` lists
  still work but give no purpose context; `purposeRules` supersedes them.
  Pass `--rules rules.json` (or update `run.sh`) to activate the full meta list.

## Conventions

- `tools/solver_ref.py` is the **authoritative implementation**.
- Geometry constants live in the `Geometry` dataclass in `calibrate.py`.
- Base stats and CPM come from PokeMiners game_master. Tests pin a handful of
  species so a refresh that changes stats fails loudly rather than silently.
- Observations are the facts; entities are the interpretation. Identity is a
  guess (two identical Gabites are indistinguishable from a screenshot), so
  entity linking must stay revisable and ambiguous cases should prompt rather
  than silently merge.
- Verdicts must stay honest about incomplete knowledge — "best you've appraised
  so far", not "best you own". `undecided`/untagged is a valid outcome; don't
  add rules that force a decision.

## Validated against real data

| Species | CP | Max HP | IVs | Level |
|---|---|---|---|---|
| Mewtwo | 2387 | 136 | 15/15/15 | 20 |
| Heatmor | 1550 | 133 | 8/14/6 | 24 |
| Komala | 1525 | 108 | 8/9/10 | 22 |

Blind (CP+HP only) these give 10, 11 and 15 candidates. With bars, exactly one.
That gap is the whole reason capture happens on the appraisal screen — IVs do
not exist in the client until you appraise, so the flow cannot be passive.

## Likely next steps

1. Grow `fixtures/` — currently 3 screenshots. Want Shadow, Lucky, XXL/XXS,
   both game themes, a costume Pokémon. This is the corpus that catches a
   Niantic UI reskin — and the prerequisite for implementing flag detection.
2. Shiny / Shadow / Lucky / costume detection. Flags are returned as `None`
   (unknown); pixel-detection TODOs are in `appraise()` in `shortcut_server.py`.
   Each flag has a comment with the glyph or region to probe.
3. ~~Wire `--rules rules.json` into `run.sh`~~ — **done**.
4. `push_ntfy` is written but untested (no network access where authored).
