#!/usr/bin/env python3
"""
Property + regression tests for the IV solver.

    python3 tools/test_solver.py

The round-trip test is the important one: generate a random Pokémon, compute
its CP and HP forward, then assert the solver recovers the original IVs. Any
drift in the CPM table or the CP formula fails this immediately.
"""
from __future__ import annotations

import random
import sys

from solver_ref import GameData, IVSet, compute_cp, compute_hp, solve

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {label}{' — ' + detail if detail else ''}")
    if not ok:
        FAILURES.append(label)


def test_known_base_stats(gd: GameData) -> None:
    """Regression pin: flags silent stat changes when game_master is refreshed."""
    expected = {
        "Mewtwo": (300, 182, 214),
        "Gabite": (172, 125, 169),
        "Deino": (116, 93, 141),
        "Azumarill": (112, 152, 225),
        "Metagross": (257, 228, 190),
        "Beldum": (96, 132, 120),
        "Togekiss": (225, 217, 198),
        "Bulbasaur": (118, 111, 128),
    }
    for name, (atk, dfn, sta) in expected.items():
        sp = gd.species(name)
        got = (sp.base_attack, sp.base_defense, sp.base_stamina)
        check(f"base stats {name}", got == (atk, dfn, sta), f"got {got}")


def test_cpm_anchors(gd: GameData) -> None:
    """Well-known CPM values."""
    anchors = {2: 0.094, 20: 0.4225, 30: 0.51739395, 40: 0.5974,
               60: 0.7317, 80: 0.7903, 100: 0.8403}
    for level_x2, expected in anchors.items():
        got = gd.cpm[level_x2]
        check(
            f"cpm level {level_x2 / 2:g}",
            abs(got - expected) < 5e-5,
            f"got {got:.6f} want {expected}",
        )


def test_max_cp_anchors(gd: GameData) -> None:
    """Perfect-IV level 40 CP for Pokémon with widely published values."""
    perfect = IVSet(15, 15, 15, 80)  # level 40
    cases = {"Mewtwo": 4178, "Metagross": 3791, "Azumarill": 1588}
    for name, expected in cases.items():
        sp = gd.species(name)
        got = compute_cp(sp, perfect, gd.cpm[80])
        check(f"max CP@40 {name}", got == expected, f"got {got} want {expected}")


def test_round_trip(gd: GameData, trials: int = 400) -> None:
    """Generate -> compute CP/HP -> solve -> assert the original is recovered."""
    rng = random.Random(20260831)
    names = [
        r[0]
        for r in gd.db.execute(
            "SELECT display_name FROM species WHERE is_default_form = 1 "
            "ORDER BY dex LIMIT 500"
        )
    ]
    misses = 0
    unrecovered: list[str] = []

    for _ in range(trials):
        name = rng.choice(names)
        sp = gd.species(name)
        truth = IVSet(
            rng.randrange(16), rng.randrange(16), rng.randrange(16),
            rng.randrange(2, gd.max_level_x2 + 1),
        )
        m = gd.cpm[truth.level_x2]
        cp = compute_cp(sp, truth, m)
        hp = compute_hp(sp, truth.stamina, m)

        found = solve(gd, name, cp, hp)
        if truth not in found:
            misses += 1
            unrecovered.append(f"{name} {truth} cp={cp} hp={hp}")

    check(
        f"round-trip recovery ({trials} random Pokémon)",
        misses == 0,
        f"{misses} not recovered" + (f": {unrecovered[:3]}" if unrecovered else ""),
    )


def test_bars_collapse_search(gd: GameData, trials: int = 150) -> None:
    """With exact appraisal bars the solver must return exactly one answer."""
    rng = random.Random(7)
    names = [
        r[0]
        for r in gd.db.execute(
            "SELECT display_name FROM species WHERE is_default_form = 1 LIMIT 300"
        )
    ]
    ambiguous = 0
    for _ in range(trials):
        name = rng.choice(names)
        sp = gd.species(name)
        truth = IVSet(
            rng.randrange(16), rng.randrange(16), rng.randrange(16),
            rng.randrange(2, gd.max_level_x2 + 1),
        )
        m = gd.cpm[truth.level_x2]
        found = solve(
            gd, name,
            compute_cp(sp, truth, m),
            compute_hp(sp, truth.stamina, m),
            attack_bar=truth.attack,
            defense_bar=truth.defense,
            stamina_bar=truth.stamina,
        )
        if len(found) != 1:
            ambiguous += 1
    # A handful of species genuinely alias across half levels; allow a little.
    check(
        f"appraisal bars collapse the search ({trials} trials)",
        ambiguous <= trials * 0.10,
        f"{ambiguous}/{trials} still ambiguous",
    )


def test_lucky_floor(gd: GameData) -> None:
    """Lucky Pokémon can never solve below 12 in any stat."""
    sp = gd.species("Gabite")
    truth = IVSet(2, 3, 4, 40)
    m = gd.cpm[40]
    found = solve(
        gd, "Gabite",
        compute_cp(sp, truth, m),
        compute_hp(sp, truth.stamina, m),
        is_lucky=True,
    )
    check(
        "lucky floor excludes low IVs",
        all(r.attack >= 12 and r.defense >= 12 and r.stamina >= 12 for r in found),
        f"{len(found)} candidates",
    )


def test_star_tiers(gd: GameData) -> None:
    for total, want in [(45, 3), (37, 3), (36, 2), (30, 2), (29, 1), (23, 1), (22, 0), (0, 0)]:
        iv = IVSet(min(total, 15), max(0, min(total - 15, 15)), max(0, total - 30), 40)
        check(f"star tier for IV total {total}", iv.stars == want, f"got {iv.stars}")


def test_ambiguity_realistic(gd: GameData) -> None:
    """CP+HP alone should leave real ambiguity — this is why appraisal matters."""
    sp = gd.species("Gabite")
    truth = IVSet(10, 11, 12, 40)
    m = gd.cpm[40]
    found = solve(gd, "Gabite", compute_cp(sp, truth, m), compute_hp(sp, truth.stamina, m))
    check(
        "CP+HP alone leaves multiple candidates",
        len(found) > 1,
        f"{len(found)} candidates — appraisal bars are required for certainty",
    )


def test_pvp_rank(gd: GameData) -> None:
    """
    Azumarill PvP rank acceptance tests.

    0/15/15 must be rank 1 for Great League (1500): low attack IV allows a
    higher level, and the defense+stamina bulk gained beats the attack loss.
    15/15/15 must rank WORSE than 2000 in GL for the same species — raw IV
    total is the wrong metric for PvP and these tests pin that fact.
    """
    azu = gd.species("Azumarill")
    tid = azu.template_id

    rank_0_15_15, total = gd.pvp_rank(tid, 0, 15, 15, 1500)
    check(
        "PvP: Azumarill 0/15/15 GL rank 1",
        rank_0_15_15 == 1,
        f"got rank {rank_0_15_15} of {total}",
    )

    rank_15_15_15, _ = gd.pvp_rank(tid, 15, 15, 15, 1500)
    check(
        "PvP: Azumarill 15/15/15 GL rank > 2000 (raw IV is wrong metric for PvP)",
        rank_15_15_15 > 2000,
        f"got rank {rank_15_15_15}",
    )


def main() -> int:
    gd = GameData(sys.argv[1] if len(sys.argv) > 1 else "gamedata.sqlite")
    print(f"gamedata: max level {gd.max_level_x2 / 2:g}, {len(gd.cpm)} cpm entries\n")

    test_known_base_stats(gd)
    test_cpm_anchors(gd)
    test_max_cp_anchors(gd)
    test_star_tiers(gd)
    test_lucky_floor(gd)
    test_ambiguity_realistic(gd)
    test_bars_collapse_search(gd)
    test_round_trip(gd)
    test_pvp_rank(gd)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("all tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
