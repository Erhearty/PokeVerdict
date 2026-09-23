#!/usr/bin/env python3
"""
Replays real screenshots through the full pipeline.

This is the test that catches a Niantic UI reskin. If the game moves the
appraisal bars, the measured IVs stop matching the recorded ground truth and
this fails loudly rather than the app silently producing wrong verdicts.

    python3 tools/test_fixtures.py

Add cases by screenshotting an appraisal, running tools/calibrate.py to get the
measurement, and appending to fixtures/reference.json once you've confirmed the
numbers against what the game shows you.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from calibrate import analyse, card_is_open, star_tier  # noqa: E402
from solver_ref import GameData, IVSet, solve  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "fixtures", "reference.json")
SHOTS = os.path.join(ROOT, "fixtures", "shots")

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}{' — ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


def _check_case(gd: GameData, case: dict) -> None:
    """Replay one recorded screenshot and check it against its ground truth."""
    path = os.path.join(SHOTS, case["file"])
    if not os.path.exists(path):
        check(case["file"], False, "screenshot missing")
        return

    name = f"{case['species']} ({case['file']})"
    result = analyse(path, gd, case["species"], case["cp"], case["maxHP"])

    if "error" in result:
        check(name, False, result["error"])
        return

    # 1. Bars measure to the recorded IVs.
    check(
        f"{name} bar measurement",
        result["ivs"] == case["expectedIVs"],
        f"got {result['ivs']} want {case['expectedIVs']}",
    )

    # 2. Measurement is crisply on a step, not caught mid-animation.
    check(
        f"{name} confidence",
        result["min_confidence"] >= 0.5,
        f"{result['min_confidence']:.3f}",
    )

    # 3. Star tier matches the badge visible in the screenshot.
    check(
        f"{name} star tier",
        result["stars"] == case["starTier"],
        f"got {result['stars']} want {case['starTier']}",
    )

    # 4. The strong cross-check: bars + CP + HP admit exactly one solution.
    solver = result["solver"]
    check(
        f"{name} solver consistency",
        solver["verdict"] == "consistent",
        f"{solver['verdict']} ({solver['candidates']} candidates)",
    )
    check(
        f"{name} level",
        solver["level"] == case["expectedLevel"],
        f"got {solver['level']} want {case['expectedLevel']}",
    )

    # 5. Appraisal genuinely earns its keep — blind must be ambiguous.
    check(
        f"{name} appraisal is necessary",
        result["blind_candidates"] > 1,
        f"CP+HP alone gives {result['blind_candidates']} candidates",
    )


def test_reference_fixtures(gd: GameData) -> None:
    """Every case in fixtures/reference.json measures and solves as recorded."""
    if not os.path.exists(FIXTURES):
        check("reference.json present", False)
        return

    data = json.load(open(FIXTURES))

    for case in data["cases"]:
        _check_case(gd, case)

    # 6. Reading the *first* HP number instead of the max must break things.
    #    This pins the bug that damaged Pokémon would otherwise cause.
    mewtwo = next(c for c in data["cases"] if c["species"] == "Mewtwo")
    wrong = solve(
        gd, "Mewtwo", mewtwo["cp"], 81,          # 81 is Mewtwo's *current* HP
        attack_bar=15, defense_bar=15, stamina_bar=15,
    )
    check(
        "current HP is rejected (max HP is required)",
        len(wrong) == 0,
        f"{len(wrong)} candidates from the wrong HP",
    )


def main() -> int:
    """Standalone entry point: run the fixture checks and print a summary."""
    if not os.path.exists(FIXTURES):
        print(f"no fixtures at {FIXTURES}")
        return 1

    data = json.load(open(FIXTURES))
    gd = GameData(os.path.join(ROOT, "gamedata.sqlite"))

    test_reference_fixtures(gd)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print(f"all {len(data['cases'])} fixtures passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
