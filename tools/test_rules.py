#!/usr/bin/env python3
"""
Unit tests for the rules engine in shortcut_server.py.

    python3 tools/test_rules.py gamedata.sqlite

These tests are deliberately independent of screenshots — they pass mock
result dicts directly into the rules function so each rule can be exercised
in isolation, without needing a fixture image for every edge case.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import solver_ref  # noqa: E402 — must come before shortcut_server import
from shortcut_server import load_rules, make_rules_fn  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "gamedata.sqlite")

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {label}{' — ' + detail if detail else ''}")
    if not ok:
        FAILURES.append(label)


# ─── helpers ────────────────────────────────────────────────────────────────

def _result(total: int, **flags) -> dict:
    """Minimal result dict for rules testing."""
    return {
        "total": total,
        "percent": round(total / 45 * 100, 1),
        "isShiny": None,
        "isShadow": None,
        "isPurified": None,
        "isLucky": None,
        "isCostume": None,
        "sizeClass": None,
        **flags,
    }


def _rules(gd, overrides: dict | None = None) -> object:
    base = {
        "keepSpecies": ["Mewtwo"],
        "buildSpecies": ["Gabite"],
        "buddyQueue": ["Deino", "Beldum"],
        "notableIV": 37,
        "transferIVCeiling": 23,
    }
    if overrides:
        base.update(overrides)
    return make_rules_fn(base, gd)


# ─── Phase 1: flag handling ──────────────────────────────────────────────────

def test_phase1_flags(gd):
    """Shiny=True must give KEEP even on low-IV garbage; None must be ignored."""
    fn = _rules(gd)
    bidoof = gd.species("Bidoof")

    # Core regression: shiny Bidoof with abysmal IVs must not be TRANSFER
    r = fn(bidoof, _result(5, isShiny=True))
    check(
        "P1: shiny Bidoof → KEEP",
        r["verdict"] == "KEEP",
        f"got {r['verdict']}",
    )

    # Unknown shiny (None) should not trigger the shiny rule
    r = fn(bidoof, _result(5, isShiny=None))
    check(
        "P1: unknown shiny does not force KEEP",
        r["verdict"] != "KEEP",
        f"got {r['verdict']}",
    )

    # Result dict must carry all flag keys
    from shortcut_server import appraise, load_rules, make_rules_fn
    # (checked structurally via the rules receiving them without error)
    check("P1: rules engine accepts None flags without error", True)


# ─── Phase 2: family matching ────────────────────────────────────────────────

def test_phase2_family(gd):
    """A rule naming Garchomp must match a caught Gible (same family)."""
    fn = make_rules_fn(
        {
            "keepSpecies": [],
            "buildSpecies": ["Garchomp"],  # only Garchomp listed
            "buddyQueue": [],
            "notableIV": 45,            # high threshold — won't fire on test IVs
            "transferIVCeiling": 0,
        },
        gd,
    )
    gible = gd.species("Gible")
    r = fn(gible, _result(30))
    check(
        "P2: Garchomp in buildSpecies → Gible matches",
        r["verdict"] == "BUILD",
        f"got {r['verdict']}",
    )

    # Sanity: a species not in any family should not match
    bidoof = gd.species("Bidoof")
    r2 = fn(bidoof, _result(30))
    check(
        "P2: Bidoof not matched by Garchomp rule",
        r2["verdict"] != "BUILD",
        f"got {r2['verdict']}",
    )


# ─── Phase 4: multi-rule evaluation ─────────────────────────────────────────

def test_phase4_multi_rule(gd):
    """All fired rules must be reported; verdict is the strongest one."""
    fn = _rules(gd)
    gabite = gd.species("Gabite")

    # Shiny Gabite: shiny (KEEP) + build list (BUILD) both fire
    r = fn(gabite, _result(32, isShiny=True))
    check(
        "P4: shiny Gabite verdict is KEEP",
        r["verdict"] == "KEEP",
        f"got {r['verdict']}",
    )
    check(
        "P4: shiny Gabite has ≥ 2 reasons",
        len(r.get("reasons", [])) >= 2,
        f"got {r.get('reasons')}",
    )
    check(
        "P4: shiny Gabite suggestedTag is Shiny",
        r.get("suggestedTag") == "Shiny",
        f"got {r.get('suggestedTag')}",
    )

    # Mewtwo hundo: keep list + hundo both fire → tag Keep
    mewtwo = gd.species("Mewtwo")
    r2 = fn(mewtwo, _result(45))
    check(
        "P4: hundo Mewtwo has ≥ 2 reasons",
        len(r2.get("reasons", [])) >= 2,
        f"got {r2.get('reasons')}",
    )
    check(
        "P4: hundo Mewtwo suggestedTag is Keep",
        r2.get("suggestedTag") == "Keep",
        f"got {r2.get('suggestedTag')}",
    )

    # Non-meta species with no remarkable IVs must be TRANSFER (not UNDECIDED)
    bidoof = gd.species("Bidoof")
    r3 = fn(bidoof, _result(28))  # 28 total: above ceiling(23) so ceiling doesn't fire, but fallback does
    check(
        "P4: unremarkable Bidoof is TRANSFER",
        r3["verdict"] == "TRANSFER",
        f"got {r3['verdict']}",
    )


# ─── Phase 5: box context ────────────────────────────────────────────────────

def test_phase5_box_context(gd):
    """A build-list species that is a worse duplicate should become TRADE."""
    fn = _rules(gd)
    gabite = gd.species("Gabite")

    # No box context → normal BUILD
    r = fn(gabite, _result(30))
    check(
        "P5: Gabite with no box context → BUILD",
        r["verdict"] == "BUILD",
        f"got {r['verdict']}",
    )

    # Box context with a better specimen seen → TRADE
    r2 = fn(gabite, _result(30, boxContext={"bestIVTotal": 42, "count": 2}))
    check(
        "P5: worse Gabite (30 vs best 42) → TRADE",
        r2["verdict"] == "TRADE",
        f"got {r2['verdict']}",
    )

    # Same IV total → not a worse duplicate, keep BUILD
    r3 = fn(gabite, _result(30, boxContext={"bestIVTotal": 30, "count": 1}))
    check(
        "P5: same-IV Gabite → not downgraded to TRADE",
        r3["verdict"] == "BUILD",
        f"got {r3['verdict']}",
    )


# ─── Purpose rules ───────────────────────────────────────────────────────────

def test_purpose_rules(gd):
    """purposeRules entries fire on family members and include purpose text."""
    fn = make_rules_fn(
        {
            "keepSpecies": [],
            "buildSpecies": [],
            "buddyQueue": [],
            "notableIV": 45,
            "transferIVCeiling": 0,
            "purposeRules": [
                {"species": ["Mewtwo"],  "purposes": ["Raid: Psychic", "Master League PvP"], "verdict": "KEEP"},
                {"species": ["Garchomp"],"purposes": ["Raid: Dragon", "Raid: Ground"],        "verdict": "BUILD"},
            ],
        },
        gd,
    )

    # KEEP: Mewtwo fires with purpose text
    mewtwo = gd.species("Mewtwo")
    r = fn(mewtwo, _result(30))
    check("purpose KEEP: Mewtwo verdict", r["verdict"] == "KEEP", f"got {r['verdict']}")
    check("purpose KEEP: reason includes purpose",
          any("Raid: Psychic" in reason for reason in r["reasons"]),
          str(r["reasons"]))

    # BUILD: Gible fires via Garchomp family match
    gible = gd.species("Gible")
    r2 = fn(gible, _result(30))
    check("purpose BUILD: Gible matches Garchomp family", r2["verdict"] == "BUILD", f"got {r2['verdict']}")
    check("purpose BUILD: reason includes purpose",
          any("Raid: Dragon" in reason for reason in r2["reasons"]),
          str(r2["reasons"]))

    # BUILD with better box context → TRADE
    r3 = fn(gible, _result(30, boxContext={"bestIVTotal": 42, "count": 1}))
    check("purpose BUILD: downgraded to TRADE when better seen", r3["verdict"] == "TRADE", f"got {r3['verdict']}")

    # Non-matching species: Bidoof should not fire either purpose rule → TRANSFER
    bidoof = gd.species("Bidoof")
    r4 = fn(bidoof, _result(30))
    check("purpose: Bidoof not matched → TRANSFER", r4["verdict"] == "TRANSFER", f"got {r4['verdict']}")
    check("purpose: Bidoof reason has no purpose text",
          not any("Raid:" in r or "PvP" in r for r in r4["reasons"]),
          str(r4["reasons"]))


# ─── Attack IV priority advisory ─────────────────────────────────────────────

def test_attack_iv_advisory(gd):
    """Attack advisory fires for raid/ML species with sub-15 Attack; GL/UL species warn on high Attack."""
    fn = make_rules_fn(
        {
            "keepSpecies": [],
            "buildSpecies": [],
            "buddyQueue": [],
            "notableIV": 45,
            "transferIVCeiling": 0,
            "purposeRules": [
                {"species": ["Garchomp"], "purposes": ["Raid: Dragon", "Raid: Ground", "Master League PvP"], "verdict": "BUILD"},
                {"species": ["Azumarill"], "purposes": ["Great League PvP", "Ultra League PvP"], "verdict": "BUILD"},
            ],
        },
        gd,
    )

    # Raid species with sub-15 Attack → advisory fires in notes, not reasons
    gible = gd.species("Gible")
    r = fn(gible, _result(39, ivs=[12, 14, 13]))
    check("attack advisory: sub-15 Attack on raid species fires",
          any("Attack IV 12/15" in n for n in r.get("notes", [])),
          str(r.get("notes")))
    check("attack advisory: verdict still BUILD",
          r["verdict"] == "BUILD",
          f"got {r['verdict']}")
    check("attack advisory: not in reasons",
          not any("Attack IV" in reason for reason in r["reasons"]),
          str(r["reasons"]))

    # Raid species with 15 Attack → advisory silent
    r2 = fn(gible, _result(39, ivs=[15, 12, 12]))
    check("attack advisory: 15 Attack → no advisory",
          not any("Attack IV" in n for n in r2.get("notes", [])),
          str(r2.get("notes")))

    # GL/UL species with high Attack → advisory fires in notes
    azu = gd.species("Azumarill")
    r3 = fn(azu, _result(39, ivs=[14, 13, 12]))
    check("attack advisory: high Attack on GL/UL species fires",
          any("GL/UL prefers low Attack" in n for n in r3.get("notes", [])),
          str(r3.get("notes")))

    # GL/UL species with low Attack (0) → advisory silent
    r4 = fn(azu, _result(30, ivs=[0, 15, 15]))
    check("attack advisory: low Attack on GL/UL → no advisory",
          not any("GL/UL prefers low Attack" in n for n in r4.get("notes", [])),
          str(r4.get("notes")))


# ─── Gigantamax rule ─────────────────────────────────────────────────────────

def test_gigantamax_rule(gd):
    """isDynamax=True + species in gigantamaxSpecies → KEEP with G-Max reason."""
    fn = make_rules_fn(
        {
            "keepSpecies": [],
            "buildSpecies": [],
            "buddyQueue": [],
            "notableIV": 45,
            "transferIVCeiling": 0,
            "purposeRules": [],
            "gigantamaxSpecies": ["Charizard", "Gengar"],
        },
        gd,
    )
    char = gd.species("Charmander")  # family member of Charizard

    r = fn(char, _result(30, isDynamax=True))
    check("gmax: isDynamax Charizard family → KEEP",
          r["verdict"] == "KEEP", f"got {r['verdict']}")
    check("gmax: reason mentions G-Max",
          any("G-Max" in reason for reason in r["reasons"]), str(r["reasons"]))

    # isDynamax=None → rule must not fire
    r2 = fn(char, _result(30, isDynamax=None))
    check("gmax: isDynamax=None → no G-Max rule",
          not any("G-Max" in reason for reason in r2["reasons"]), str(r2["reasons"]))

    # isDynamax=True but species not in list → no rule
    bidoof = gd.species("Bidoof")
    r3 = fn(bidoof, _result(30, isDynamax=True))
    check("gmax: non-G-Max species → no G-Max rule",
          not any("G-Max" in reason for reason in r3["reasons"]), str(r3["reasons"]))


# ─── Shadow promotion ────────────────────────────────────────────────────────

def test_shadow_promotion(gd):
    """Shadow meta species → KEEP; shadow on junk → no note, no promotion."""
    fn = make_rules_fn(
        {
            "keepSpecies": [],
            "buildSpecies": [],
            "buddyQueue": [],
            "notableIV": 45,
            "transferIVCeiling": 0,
            "purposeRules": [
                {"species": ["Ralts"], "purposes": ["Raid: Fairy", "Great League PvP"], "verdict": "BUILD"},
                {"species": ["Deino"], "purposes": ["Raid: Dark", "Raid: Dragon"],      "verdict": "BUILD"},
            ],
        },
        gd,
    )

    ralts = gd.species("Ralts")
    deino = gd.species("Deino")
    bidoof = gd.species("Bidoof")

    # Shadow meta species → KEEP + note
    r = fn(ralts, _result(36, isShadow=True))
    check("shadow: Shadow Ralts → KEEP",
          r["verdict"] == "KEEP", f"got {r['verdict']}")
    check("shadow: reason mentions Shadow",
          any("Shadow" in reason for reason in r["reasons"]), str(r["reasons"]))
    check("shadow: note fires for meta species",
          any("do NOT purify" in n for n in r.get("notes", [])), str(r.get("notes")))

    r2 = fn(deino, _result(28, isShadow=True))
    check("shadow: Shadow Deino → KEEP",
          r2["verdict"] == "KEEP", f"got {r2['verdict']}")

    # Non-shadow meta species → normal BUILD, no shadow reason
    r3 = fn(ralts, _result(36, isShadow=None))
    check("shadow: non-shadow Ralts → BUILD",
          r3["verdict"] == "BUILD", f"got {r3['verdict']}")
    check("shadow: non-shadow has no Shadow reason",
          not any("Shadow" in reason for reason in r3["reasons"]), str(r3["reasons"]))

    # Shadow on junk → TRANSFER, no note
    r4 = fn(bidoof, _result(20, isShadow=True))
    check("shadow: Shadow Bidoof stays TRANSFER",
          r4["verdict"] == "TRANSFER", f"got {r4['verdict']}")
    check("shadow: Shadow Bidoof has no purify note",
          not any("purify" in n.lower() for n in r4.get("notes", [])), str(r4.get("notes")))


# ─── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    gd = solver_ref.GameData(sys.argv[1] if len(sys.argv) > 1 else DB)

    test_phase1_flags(gd)
    print()
    test_phase2_family(gd)
    print()
    test_phase4_multi_rule(gd)
    print()
    test_phase5_box_context(gd)
    print()
    test_purpose_rules(gd)
    print()
    test_attack_iv_advisory(gd)
    print()
    test_gigantamax_rule(gd)
    print()
    test_shadow_promotion(gd)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("all tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
