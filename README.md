# PokeVerdict

Reads the Pokémon GO appraisal screen off a live screen broadcast, solves exact
IVs, applies your Keep/Build/Buddy/Trade rules, and pushes the verdict as a
notification. Keeps a local SQLite record of everything it has seen, browsable
with the same search syntax the game uses.

Personal tool, read-only, no game API calls.

---

## What runs where

| Piece | Target | Why |
|---|---|---|
| `PokeVerdictCore` | Swift package | Solver, rules, search, storage. Unit-testable without a device. |
| `BroadcastExtension` | Broadcast Upload Extension | The only sanctioned system-wide screen capture on iOS. |
| `PokeVerdictApp` | iOS app | Ingest, collection browser, rules editor. |

The extension never opens SQLite. It appends JSON lines to a shared file; the
app drains that file into the database on foreground. A killed extension costs
at most one truncated line.

---

## Status

**Verified on Linux:** the IV solver, the game data pipeline, the CP/HP maths,
and — since calibration against real screenshots — the bar measurement and
screen geometry.

```
$ python3 tools/test_solver.py gamedata.sqlite   # 400-sample round trip
$ python3 tools/test_fixtures.py                 # real screenshots end to end
all 3 fixtures passed
```

Measured against three actual appraisal screens:

| Species | CP | Max HP | Measured bars | Raw | Level | Solver |
|---|---|---|---|---|---|---|
| Mewtwo | 2387 | 136 | 15/15/15 | 15.00/15.00/15.00 | 20 | 1 candidate |
| Heatmor | 1550 | 133 | 8/14/6 | 8.01/13.98/5.97 | 24 | 1 candidate |
| Komala | 1525 | 108 | 8/9/10 | 8.01/9.03/9.88 | 22 | 1 candidate |

Raw measurements land within 0.03 of an integer, and every case collapses to
exactly one solution. Blind (CP + HP only) those same three give 10, 11 and 15
candidates respectively.

**Not compiled:** the Swift. Written without an Xcode toolchain, so expect to
fix imports and a few API details on first build. The logic is a direct port of
the tested Python and the Swift tests mirror the Python ones.

---

## Two ways to run this

**A. Shortcuts route — no signing, no Apple developer account.** Screenshot →
POST → verdict. Server tested end to end on Linux; see **SHORTCUTS.md**. Start
here: it works today with nothing installed on the phone but a Shortcut.

**B. Native app + broadcast extension.** Continuous screen capture, local
collection database. Needs Xcode and code signing; a free Apple ID works but
provisioning profiles expire every 7 days.

---

## Build

### 1. Generate game data

```bash
python3 tools/build_gamedata.py --fetch
python3 tools/test_solver.py gamedata.sqlite   # should print "all tests passed"
```

Produces `gamedata.sqlite` (~284 KB, 1099 species). Regenerate after any
balance patch.

**Level cap.** The published CPM table runs to level 55, but those entries are
not player-attainable. Pokémon max out at **50** by powering up; 50.5 and 51
are reachable only as a Best Buddy boost, and the level 55 multiplier exists
solely for team leaders in Master League Battle Training. The builder caps the
search at 51 — searching to 55 lets the solver return phantom candidates at
levels no real Pokémon can occupy.

### 2. Xcode project

Xcode can't create this layout from a template; do it once by hand.

1. New project → iOS App, SwiftUI, name `PokeVerdict`.
2. File → Add Package Dependencies → Add Local → select this folder.
3. File → New → Target → **Broadcast Upload Extension**, name it
   `PokeVerdictBroadcast`. Delete the sample UI extension Xcode also offers.
4. Both targets → Signing & Capabilities → **+ App Groups** → add
   `group.com.yourteam.pokeverdict` to *both*.
5. App target → **+ Push Notifications** is not needed; these are local
   notifications. Do add the usage description if Xcode prompts.
6. Drag `Resources/gamedata.sqlite` in, ticking **both** targets under Target
   Membership. The extension needs its own copy.
7. Copy `Sources/PokeVerdictApp/**` into the app target and
   `Sources/BroadcastExtension/**` into the extension target.
8. Replace two placeholder strings:
   - `ObservationQueue.appGroupID` in `ObservationQueue.swift`
   - `preferredExtension` in `RootView.swift` — must equal the extension's
     bundle identifier exactly, or the picker silently shows nothing.

### 3. Smoke test before writing anything else

The single biggest unknown is whether a broadcast extension can post a local
notification on your iOS version. Prove it first:

```swift
// In broadcastStarted, before anything else:
let c = UNMutableNotificationContent()
c.title = "broadcast alive"
UNUserNotificationCenter.current().add(
    UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil)
)
```

If that notification doesn't arrive, stop and switch to the fallback: write
verdicts to the App Group container and surface them in the host app instead.
Everything else in this design survives that change.

---

## Calibration

Coordinates in `AppraisalGeometry` were measured from real 1320x2868
screenshots, not guessed. They are normalised, so they should transfer across
devices — but verify on your own captures:

```bash
python3 tools/calibrate.py --species Mewtwo --cp 2387 --hp 136 shot.PNG
python3 tools/calibrate.py --emit-swift          # regenerate the Swift constants
```

**How the bars are read.** Each bar is drawn as three visual segments with
small gaps, but fill maps *linearly* across the whole track, so the rightmost
saturated pixel is all that's needed. Bar colour changes with tier — orange at
low tiers, pink at three stars — so detection keys on saturation, not hue.

**Confidence.** Each reading reports how close the fill sits to an exact step.
A bar caught mid-animation lands between steps and scores low; below 0.5 the
bars are discarded and the solver reports a range instead. A confident wrong
answer is worse than an honest uncertain one.

**The free cross-check.** If the measured bars produce *zero* solutions for the
observed CP and HP, the read was wrong. This costs nothing and catches almost
every misread, so it runs on every frame.

To grow the corpus: screenshot an appraisal, run `calibrate.py`, confirm the
numbers against what the game shows, append to `fixtures/reference.json`.

---

## Why capture happens on the appraisal screen

IVs don't exist in the client until you appraise. From CP and HP alone the
solver returned 10, 11 and 15 candidates on the three reference screenshots.
The appraisal bars are what collapse that to one.

So the loop is: catch → appraise → verdict. It cannot be fully passive.

## Two bugs the real screenshots caught

Both would have produced confidently wrong answers, which is the worst failure
mode for a tool like this.

**1. HP is "current / max".** A damaged Pokémon shows `81 / 136 HP`. The solver
needs 136. Reading the first number silently corrupts IVs for anything that has
been in a raid or a gym — and Mewtwo, the most valuable thing in the box, is
exactly the case that shows up damaged. `testCurrentHPIsRejected` pins it.

**2. Star tiers are 0-3, not 1-4.** The badge shows up to three filled stars.
Two reference Pokémon with IV totals of 27 and 28 both display a single star,
placing the first band at 23-29. The original code was off by one across the
whole range.

---

## Search syntax

The collection view accepts the game's own syntax, compiled to SQL:

```
4*&!#&!shiny        high IV, untagged, not shiny
shiny,shadow&4*     (shiny OR shadow) AND high IV
cp100-500&dragon    CP band AND dragon type
#build              tagged Build
age0                caught today
```

`&` is the loosest operator; `,` and `;` bind tighter inside a group. No
parentheses, same as the game.

---

## Known limitations

**Duplicate detection is partial.** The app only knows what you've appraised.
Verdicts say "best you've appraised so far" rather than claiming to know your
whole box. This is honest, and it improves as you use it.

**Identity is a guess.** Two Gabites with identical IVs and CP are genuinely
indistinguishable from a screenshot. `CollectionDatabase.resolveEntity` links
on fingerprint plus non-decreasing CP; ambiguous cases should surface a merge
prompt rather than silently guessing.

**Transfers are invisible.** The app can't see when you send something to the
professor. Mark it disposed in the detail view.

**Terms of service.** Niantic prohibits third-party software that interacts
with the game. Screenshot OCR of your own screen has been tolerated for years
and Poke Genie ships on the App Store doing exactly this, but it isn't formally
blessed. Personal and read-only is about as low-risk as this gets, not zero.

---

## Layout

```
tools/build_gamedata.py     game_master -> gamedata.sqlite
tools/solver_ref.py         reference solver, runs anywhere
tools/test_solver.py        property tests for the maths
tools/calibrate.py          measure screenshots, emit Swift constants
tools/test_fixtures.py      replay real screenshots end to end
tools/shortcut_server.py    HTTP service for the Shortcuts route
SHORTCUTS.md                the no-signing path, with the Shortcut recipe
fixtures/reference.json     ground truth
fixtures/shots/             the screenshots themselves

Sources/PokeVerdictCore/
  Model/Models.swift        Species, IVSet, Observation, Entity, Verdict
  Solver/IVSolver.swift     forward CP/HP + inverse solve
  Rules/RulesEngine.swift   verdict logic, driven by rules.json
  Search/SearchQuery.swift  in-game syntax -> SQL
  Data/GameDataStore.swift  read-only species and CPM tables
  Data/CollectionDatabase.swift  migrations, entity resolution, export
  Data/ObservationQueue.swift    crash-safe extension -> app handoff

Sources/BroadcastExtension/
  SampleHandler.swift       frame loop under the 50MB cap
  ScreenFingerprint.swift   AppraisalGeometry + card gate + bar measurement
  AppraisalParser.swift     Vision OCR on cropped regions

Sources/PokeVerdictApp/
  PokeVerdictApp.swift      wiring
  Ingestor.swift            drain + re-evaluate with box context
  Views/RootView.swift      capture, collection, rules
```
