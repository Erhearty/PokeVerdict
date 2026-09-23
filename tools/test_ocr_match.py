#!/usr/bin/env python3
"""
Tests for OCR-tolerant species matching.

Run after test_solver.py and test_fixtures.py — those cover the solver maths;
this one covers the fuzzy lookup the appraisal pipeline uses to turn OCR text
into a Species object.

    python3 tools/test_ocr_match.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from solver_ref import GameData  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "gamedata.sqlite")

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {label}{' — ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


def resolves(gd: GameData, text: str, expected: str) -> None:
    try:
        sp = gd.best_species_match(text)
        check(
            f"{text!r} resolves to {expected}",
            sp.display_name == expected,
            f"got {sp.display_name!r}",
        )
    except KeyError as exc:
        check(f"{text!r} resolves to {expected}", False, f"raised: {exc}")


def rejects(gd: GameData, text: str, reason: str = "") -> None:
    try:
        sp = gd.best_species_match(text)
        check(f"{text!r} is rejected{' (' + reason + ')' if reason else ''}",
              False, f"wrongly matched to {sp.display_name!r}")
    except KeyError:
        check(f"{text!r} is rejected{' (' + reason + ')' if reason else ''}", True)


def test_ocr_matching(gd: GameData) -> None:
    """OCR-garbled names resolve via glyph/edit-distance; ties and junk are rejected."""
    # ── glyph substitution path ─────────────────────────────────────────────
    # ! | 1  →  l
    resolves(gd, "Aerodacty!", "Aerodactyl")
    resolves(gd, "Aerodacty1", "Aerodactyl")
    resolves(gd, "Aerodacty|", "Aerodactyl")

    # 0  →  o
    resolves(gd, "Mewtw0", "Mewtwo")

    # ── edit-distance path ──────────────────────────────────────────────────
    # "Komaia": 'i' misread as 'l', no glyph covers this → edit distance 1
    resolves(gd, "Komaia", "Komala")

    # ── rejections ──────────────────────────────────────────────────────────
    # "Muw": edit distance 1 from both Mew and Muk → tie → must raise, not guess
    rejects(gd, "Muw", "tie between Mew and Muk")

    # "Zzzz": nothing within budget
    rejects(gd, "Zzzz", "no match")

    # ── exact lookups still work unchanged ──────────────────────────────────
    # These go through best_species_match but must resolve on the first try.
    for name in ("Mewtwo", "Heatmor", "Komala", "Aerodactyl"):
        resolves(gd, name, name)


def main() -> int:
    """Standalone entry point: run the OCR matching checks and print a summary."""
    gd = GameData(DB)
    test_ocr_matching(gd)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
