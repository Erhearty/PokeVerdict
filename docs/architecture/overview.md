<!-- generated:start cap:overview-intro -->
# Architecture Overview

8 component(s) declared on the architecture canvas. Topology: [system-map.md](system-map.md).
<!-- generated:end cap:overview-intro -->

<!-- generated:start comp:shortcut-server -->
## Shortcut Server (`shortcut-server`, BACKEND)

Reads the Pokémon GO appraisal screen, solves exact IVs, applies Keep/Build/Buddy/Trade rules, and pushes a verdict. Personal tool, read-only, no game API — the flow is screenshot → POST → verdict. Stateless per-request; optionally records verdicts to Collection DB and pushes to ntfy. Non-negotiable facts pinned by tests: Pokémon max at level 50 (not 55); HP is displayed "current / max" and only max is used; appraisal badge stars are 0–3 across bands 0–22/23–29/30–36/37–45; CP is derived from species+stamina IV+max HP (pinning level uniquely 54.5% of the time), never OCR'd as an input; IVs and verdicts never depend on CP; bars are measured by rightmost saturated pixel, not read via OCR digits.

**Tech:** Python 3, http.server (stdlib), Pillow, numpy, pytesseract
<!-- generated:end comp:shortcut-server -->

<!-- generated:start comp:ios-shortcut -->
## iOS Shortcut (`ios-shortcut`, FRONTEND)

Apple Shortcuts app flow (SHORTCUTS.md), not code in this repo — no code signing, no developer account. "Appraise" shortcut: Take Screenshot → POST to Shortcut Server's /appraise?format=text with the screenshot as the request body → Show Result as a banner. Triggered by double back-tap. Untested end-to-end (no iPhone in the build environment); every action used is standard/documented.

**Tech:** Apple Shortcuts
<!-- generated:end comp:ios-shortcut -->

<!-- generated:start comp:iv-solver -->
## IV Solver (`iv-solver`, CUSTOM)

Authoritative implementation (tools/solver_ref.py) of IV solving, level pinning, PvP ranking, and OCR species resolution. GameData.pvp_rank() scores all 4096 IV combos by stat product at the highest level where CP ≤ league cap — raw IV total is the wrong metric for PvP. OCR species lookup goes through best_species_match() (exact → glyph-substitution exact → Levenshtein, budget 1 for names ≤5 chars else 2; raises on a tie), not the stable exact-match species(). Flags (isShiny/isShadow/isPurified/isLucky/isCostume/sizeClass) are None (unknown) until pixel detection exists — never coerce to False.

**Tech:** Python 3
<!-- generated:end comp:iv-solver -->

<!-- generated:start comp:game-data-builder -->
## Game Data Builder (`game-data-builder`, CUSTOM)

tools/build_gamedata.py — rebuilds gamedata.sqlite from PokeMiners' game_master.json (--fetch to pull fresh, or a local --input file). Tests pin a handful of species so a stat refresh that changes them fails loudly instead of silently.

**Tech:** Python 3
<!-- generated:end comp:game-data-builder -->

<!-- generated:start comp:game-data-db -->
## Game Data DB (`game-data-db`, DATABASE)

gamedata.sqlite: species base stats, CPM table (capped at level 51; 50.5/51 are Best-Buddy-only, 55 is Master League Battle Training only and would produce phantom candidates), powerup stardust bands (approximate, only narrows level search), evolutions, and moves. Built offline from PokeMiners game_master (gm.json) by Game Data Builder; the Shortcut Server and Calibration Tool only ever read it.
<!-- generated:end comp:game-data-db -->

<!-- generated:start comp:collection-db -->
## Collection DB (`collection-db`, DATABASE)

collection.sqlite (Python port of CollectionDatabase.swift's schema). Observations are facts; entities are the interpretation and stay revisable — identity is a guess (two identical Gabites are indistinguishable from a screenshot), so entity linking must never silently merge ambiguous cases. Entity resolution: same identity_key + non-decreasing CP → same entity. Stores per-appraisal observations, capture failures, raw screenshots, and disposable/taggable entities.
<!-- generated:end comp:collection-db -->

<!-- generated:start comp:calibration-tool -->
## Calibration Tool (`calibration-tool`, CUSTOM)

tools/calibrate.py — measures the appraisal bar geometry on real screenshots and checks it against Game Data DB. Geometry constants live in the Geometry dataclass. Used to re-derive screen coordinates when a device's screen geometry differs from the reference fixtures.

**Tech:** Python 3, Pillow, numpy
<!-- generated:end comp:calibration-tool -->

<!-- generated:start comp:ntfy-push -->
## ntfy Push (`ntfy-push`, GATEWAY)

Optional outbound push via ntfy.sh (push_ntfy in shortcut_server.py) — forwards each verdict to an ntfy topic so the ntfy iOS app can show it as a push notification. Written but unverified: ntfy.sh was unreachable from the build sandbox where it was authored. The local appraisal path works fully without it.
<!-- generated:end comp:ntfy-push -->
