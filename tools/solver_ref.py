#!/usr/bin/env python3
"""
Reference implementation of the IV solver.

This is the algorithm the Swift app ships; keeping a runnable copy means the
maths can be property-tested on any machine without an Xcode toolchain.

    CP = floor( ATK * sqrt(DEF) * sqrt(STA) * CPM^2 / 10 ), minimum 10
    HP = floor( STA * CPM ),                                minimum 10

where ATK = base_attack + atk_iv (and so on).
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from functools import lru_cache

# ---------------------------------------------------------------------------
# Regional form name normalisation
# ---------------------------------------------------------------------------

_REGIONAL_PREFIXES = {
    "alolan": "Alola", "galarian": "Galarian", "hisuian": "Hisui",
    "paldean": "Paldea", "kantonian": "Kanto", "johtonian": "Johto",
}


def _regional_normalize(text: str) -> list[str]:
    """'Galarian Stunfisk' → ['Stunfisk (Galarian)'].  Returns [] if not a regional form."""
    parts = text.strip().split(" ", 1)
    if len(parts) == 2:
        suffix = _REGIONAL_PREFIXES.get(parts[0].lower())
        if suffix:
            return [f"{parts[1]} ({suffix})"]
    return []


# ---------------------------------------------------------------------------
# OCR glyph correction
# ---------------------------------------------------------------------------

# Characters that tesseract commonly confuses with others in Pokémon names.
_GLYPH_TABLE = str.maketrans({
    '!': 'l', '|': 'l', '1': 'l',
    '0': 'o',
    '5': 's', '$': 's',
    '’': "'",   # RIGHT SINGLE QUOTATION MARK  →  straight apostrophe
    '´': "'",   # ACUTE ACCENT                 →  straight apostrophe
})


def _apply_glyphs(text: str) -> str:
    return text.translate(_GLYPH_TABLE)


def _levenshtein(a: str, b: str) -> int:
    """Standard DP Levenshtein distance."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    if la > lb:
        a, b, la, lb = b, a, lb, la
    prev = list(range(lb + 1))
    for i in range(la):
        curr = [i + 1] + [0] * lb
        for j in range(lb):
            curr[j + 1] = min(
                prev[j] + (0 if a[i] == b[j] else 1),
                curr[j] + 1,
                prev[j + 1] + 1,
            )
        prev = curr
    return prev[lb]


@dataclass(frozen=True)
class Species:
    template_id: str
    display_name: str
    base_attack: int
    base_defense: int
    base_stamina: int
    rarity: str | None = None  # 'legendary', 'mythic', 'ultra_beast', or None
    is_default_form: int = 1   # 1 = default/base form, 0 = regional/alternate form


@dataclass(frozen=True)
class IVSet:
    attack: int
    defense: int
    stamina: int
    level_x2: int

    @property
    def level(self) -> float:
        return self.level_x2 / 2

    @property
    def total(self) -> int:
        return self.attack + self.defense + self.stamina

    @property
    def percent(self) -> float:
        return self.total / 45 * 100

    @property
    def stars(self) -> int:
        """In-game appraisal badge: 0-3 filled stars."""
        t = self.total
        if t >= 37:
            return 3
        if t >= 30:
            return 2
        if t >= 23:
            return 1
        return 0

    def __str__(self) -> str:
        return (
            f"{self.attack}/{self.defense}/{self.stamina} "
            f"({self.percent:.0f}%) L{self.level:g}"
        )


class GameData:
    def __init__(self, path: str = "gamedata.sqlite", check_same_thread: bool = True):
        # check_same_thread=False lets a server hand this connection between
        # request threads. Safe only when callers serialise access; the
        # Shortcuts server does so with a lock.
        self.db = sqlite3.connect(path, check_same_thread=check_same_thread)
        self.db.row_factory = sqlite3.Row
        self.cpm: dict[int, float] = {
            r["level_x2"]: r["multiplier"]
            for r in self.db.execute("SELECT level_x2, multiplier FROM cpm")
        }
        self.max_level_x2 = max(self.cpm)

    @lru_cache(maxsize=None)
    def species(self, name_or_id: str) -> Species:
        row = self.db.execute(
            """SELECT template_id, display_name, base_attack, base_defense, base_stamina,
                      rarity, is_default_form
               FROM species
               WHERE display_name = ? COLLATE NOCASE
                  OR pokemon_id   = ? COLLATE NOCASE
                  OR template_id  = ?
               ORDER BY is_default_form DESC LIMIT 1""",
            (name_or_id, name_or_id, name_or_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown species: {name_or_id!r}")
        return Species(*row)

    def pvp_rank(
        self, template_id: str, atk_iv: int, def_iv: int, sta_iv: int, cap: int
    ) -> tuple[int, int]:
        """
        Return (rank, total_spreads) for the given IVs at the CP cap.

        Rank is 1-indexed; 1 = best. Stat product =
            (base_atk + atk_iv) * CPM  ×  (base_def + def_iv) * CPM
            × floor((base_sta + sta_iv) * CPM)
        at the highest level where CP ≤ cap.  Spreads that can't fit under
        the cap at even level 1 score 0 and rank last.
        """
        table = self._pvp_table(template_id, cap)
        target = table.get((atk_iv, def_iv, sta_iv), 0.0)
        rank = sum(1 for s in table.values() if s > target) + 1
        return rank, len(table)

    def _pvp_table(
        self, template_id: str, cap: int
    ) -> dict[tuple[int, int, int], float]:
        """Cached: stat-product for every (atk, def, sta) combo at the cap."""
        key = (template_id, cap)
        if not hasattr(self, "_pvp_cache"):
            self._pvp_cache: dict[tuple, dict] = {}
        if key not in self._pvp_cache:
            self._pvp_cache[key] = self._compute_pvp_table(template_id, cap)
        return self._pvp_cache[key]

    def _compute_pvp_table(
        self, template_id: str, cap: int
    ) -> dict[tuple[int, int, int], float]:
        sp = self.species(template_id)
        # Scan from highest level downward to find the cap for each spread.
        sorted_lx2 = sorted(self.cpm.keys(), reverse=True)
        table: dict[tuple[int, int, int], float] = {}
        for a in range(16):
            for d in range(16):
                for s in range(16):
                    score = 0.0
                    for lx2 in sorted_lx2:
                        m = self.cpm[lx2]
                        if compute_cp(sp, IVSet(a, d, s, lx2), m) <= cap:
                            eff_a = (sp.base_attack + a) * m
                            eff_d = (sp.base_defense + d) * m
                            eff_s = math.floor((sp.base_stamina + s) * m)
                            score = eff_a * eff_d * eff_s
                            break
                    table[(a, d, s)] = score
        return table

    def family_id_for(self, template_id: str) -> str | None:
        """The family_id of the given species template, cached."""
        if not hasattr(self, "_family_cache"):
            self._family_cache: dict[str, str | None] = {}
        if template_id not in self._family_cache:
            row = self.db.execute(
                "SELECT family_id FROM species WHERE template_id = ?", (template_id,)
            ).fetchone()
            self._family_cache[template_id] = row[0] if row else None
        return self._family_cache[template_id]

    def family_members(self, family_id: str) -> list[str]:
        """All template_ids belonging to the given evolutionary family."""
        rows = self.db.execute(
            "SELECT template_id FROM species WHERE family_id = ?", (family_id,)
        ).fetchall()
        return [r[0] for r in rows]

    def evo_forward_cluster(self, template_id: str) -> frozenset[str]:
        """
        All template_ids reachable from template_id by following evolution links,
        including the starting template itself.

        Used to constrain family matching for non-default forms: Galarian Stunfisk
        shares a family_id with Kantonian Stunfisk, but their evolution trees are
        disjoint.  Walking evolutions forward keeps the match form-specific.
        """
        if not hasattr(self, "_evo_cluster_cache"):
            self._evo_cluster_cache: dict[str, frozenset[str]] = {}
        if template_id in self._evo_cluster_cache:
            return self._evo_cluster_cache[template_id]

        visited: set[str] = {template_id}
        queue = [template_id]
        while queue:
            current = queue.pop()
            evo_rows = self.db.execute(
                "SELECT to_pokemon_id, to_form FROM evolution WHERE from_template = ?",
                (current,),
            ).fetchall()
            for er in evo_rows:
                to_pid, to_form = er["to_pokemon_id"], er["to_form"]
                sp_rows = []
                if to_form:
                    sp_rows = self.db.execute(
                        "SELECT template_id FROM species WHERE pokemon_id = ? AND form = ?",
                        (to_pid, to_form),
                    ).fetchall()
                # Fallback: cosmetic _NORMAL forms are collapsed to form=NULL by
                # build_gamedata.py, so to_form='CURSOLA_NORMAL' finds nothing.
                if not sp_rows:
                    sp_rows = self.db.execute(
                        "SELECT template_id FROM species WHERE pokemon_id = ? AND form IS NULL",
                        (to_pid,),
                    ).fetchall()
                for sr in sp_rows:
                    tid = sr["template_id"]
                    if tid not in visited:
                        visited.add(tid)
                        queue.append(tid)

        result = frozenset(visited)
        self._evo_cluster_cache[template_id] = result
        return result

    def best_moves(self, template_id: str) -> dict:
        """
        Return best fast and charged moves for raid and PvP contexts.

        Raid fast ranking: DPS = pve_power / (pve_duration_ms / 1000)
        Raid charged ranking: DPE = pve_power / abs(pve_energy)
        PvP fast ranking: EPT primary, DPT secondary (turns = pvp_duration_ms / 500)
        PvP charged ranking: DPE = pvp_power / abs(pvp_energy)

        Returns {} if the move tables are absent (old DB) or the species has no moves.
        """
        try:
            rows = self.db.execute(
                """SELECT m.move_id, m.display_name, m.move_type,
                          m.pve_power, m.pve_energy, m.pve_duration_ms,
                          m.pvp_power, m.pvp_energy, m.pvp_duration_ms
                   FROM species_move sm JOIN move m ON m.move_id = sm.move_id
                   WHERE sm.template_id = ?""",
                (template_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            return {}

        if not rows:
            return {}

        fast    = [r for r in rows if r["move_type"] == "fast"]
        charged = [r for r in rows if r["move_type"] == "charged"]

        def _pve_dps(m) -> float:
            return (m["pve_power"] or 0) / max(1, (m["pve_duration_ms"] or 1) / 1000)

        def _pvp_turns(m) -> int:
            return max(1, round((m["pvp_duration_ms"] or 500) / 500))

        def _pvp_ept(m) -> float:
            return (m["pvp_energy"] or 0) / _pvp_turns(m)

        def _pvp_dpt(m) -> float:
            return (m["pvp_power"] or 0) / _pvp_turns(m)

        def _pve_dpe(m) -> float:
            return (m["pve_power"] or 0) / max(1, abs(m["pve_energy"] or 1))

        def _pvp_dpe(m) -> float:
            return (m["pvp_power"] or 0) / max(1, abs(m["pvp_energy"] or 1))

        best_fast_raid = max(fast, key=_pve_dps, default=None)
        best_fast_pvp  = max(fast, key=lambda m: (_pvp_ept(m), _pvp_dpt(m)), default=None)
        charged_raid   = sorted(charged, key=_pve_dpe, reverse=True)
        charged_pvp    = sorted(charged, key=_pvp_dpe, reverse=True)

        return {
            "raid": {
                "fast":    best_fast_raid["display_name"] if best_fast_raid else None,
                "charged": [m["display_name"] for m in charged_raid[:2]],
            },
            "pvp": {
                "fast":    best_fast_pvp["display_name"] if best_fast_pvp else None,
                "charged": [m["display_name"] for m in charged_pvp[:2]],
            },
        }

    def best_species_match(self, text: str) -> Species:
        """
        OCR-tolerant lookup for the appraisal pipeline.

        Resolution order:
          1. Exact match (display name, pokemon_id, or template_id).
          2. Exact match after applying the glyph substitution table
             (! | 1 → l, 0 → o, 5 → s, $ → s, curly/accent apostrophes → ').
          3. Levenshtein distance against default-form display names.
             Budget: 1 for names ≤ 5 chars, 2 for longer names.
             Tie → raise rather than guess.

        Does not alter the behaviour of `species()` — callers that depend on
        exact semantics are unaffected.
        """
        # 1. Exact match.
        try:
            return self.species(text)
        except KeyError:
            pass

        # 2. Exact match after glyph substitution.
        fixed = _apply_glyphs(text)
        if fixed != text:
            try:
                return self.species(fixed)
            except KeyError:
                pass

        # 2b. Regional prefix variant: "Galarian Stunfisk" → "Stunfisk (Galarian)".
        for variant in _regional_normalize(text) + _regional_normalize(fixed):
            try:
                return self.species(variant)
            except KeyError:
                pass

        # 3. Edit distance. Use the glyph-fixed text to reduce noise.
        budget = 1 if len(text) <= 5 else 2
        query = fixed.lower()

        best_dist = budget + 1
        candidates: list[str] = []

        for row in self.db.execute(
            "SELECT display_name FROM species WHERE is_default_form = 1"
        ):
            name = row[0]
            dist = _levenshtein(query, name.lower())
            if dist > budget:
                continue
            if dist < best_dist:
                best_dist = dist
                candidates = [name]
            elif dist == best_dist:
                candidates.append(name)

        if not candidates:
            raise KeyError(f"unknown species from OCR: {text!r}")

        if len(candidates) > 1:
            tied = " vs ".join(repr(n) for n in candidates)
            raise KeyError(f"ambiguous OCR match for {text!r}: {tied}")

        return self.species(candidates[0])


def compute_cp(sp: Species, iv: IVSet, cpm: float) -> int:
    atk = sp.base_attack + iv.attack
    dfn = sp.base_defense + iv.defense
    sta = sp.base_stamina + iv.stamina
    cp = math.floor(atk * math.sqrt(dfn) * math.sqrt(sta) * cpm * cpm / 10)
    return max(10, cp)


def compute_hp(sp: Species, stamina_iv: int, cpm: float) -> int:
    return max(10, math.floor((sp.base_stamina + stamina_iv) * cpm))


def solve(
    gd: GameData,
    species_name: str,
    cp: int,
    hp: int,
    *,
    stars: int | None = None,
    attack_bar: int | None = None,
    defense_bar: int | None = None,
    stamina_bar: int | None = None,
    is_lucky: bool = False,
    min_level_x2: int = 2,
    max_level_x2: int | None = None,
) -> list[IVSet]:
    """
    Return every (atk, def, sta, level) consistent with the observation.

    `attack_bar` and friends are exact IV values read off the appraisal bars by
    measuring pixel width. When supplied they collapse the search instantly.
    `stars` is the coarse appraisal tier and is a much weaker constraint.
    """
    sp = gd.species(species_name)
    hi = max_level_x2 or gd.max_level_x2

    atk_range = [attack_bar] if attack_bar is not None else range(16)
    def_range = [defense_bar] if defense_bar is not None else range(16)
    sta_range = [stamina_bar] if stamina_bar is not None else range(16)

    # Lucky Pokémon are floored at 12 in every stat.
    if is_lucky:
        atk_range = [v for v in atk_range if v >= 12]
        def_range = [v for v in def_range if v >= 12]
        sta_range = [v for v in sta_range if v >= 12]

    results: list[IVSet] = []
    for level_x2 in range(min_level_x2, hi + 1):
        m = gd.cpm[level_x2]
        for sta_iv in sta_range:
            # HP depends only on stamina and level, so filter here and skip
            # 256 pointless CP evaluations whenever it fails.
            if compute_hp(sp, sta_iv, m) != hp:
                continue
            for atk_iv in atk_range:
                for def_iv in def_range:
                    cand = IVSet(atk_iv, def_iv, sta_iv, level_x2)
                    if stars is not None and cand.stars != stars:
                        continue
                    if compute_cp(sp, cand, m) == cp:
                        results.append(cand)
    return results


def describe(results: list[IVSet]) -> str:
    if not results:
        return "no solution — check the parsed CP/HP"
    if len(results) == 1:
        return f"exact: {results[0]}"
    lo = min(r.total for r in results)
    hi = max(r.total for r in results)
    return (
        f"{len(results)} candidates, IV total {lo}-{hi} "
        f"({lo / 45 * 100:.0f}-{hi / 45 * 100:.0f}%)"
    )


if __name__ == "__main__":
    import sys

    gd = GameData(sys.argv[1] if len(sys.argv) > 1 else "gamedata.sqlite")

    # Worked example: build a Pokémon, compute what the game would display,
    # then show what the solver can recover from that display alone.
    for name, truth in [
        ("Mewtwo", IVSet(14, 13, 15, 60)),
        ("Gabite", IVSet(10, 11, 12, 50)),
    ]:
        sp = gd.species(name)
        m = gd.cpm[truth.level_x2]
        cp = compute_cp(sp, truth, m)
        hp = compute_hp(sp, truth.stamina, m)

        print(f"{name}: actual {truth}  ->  screen shows CP {cp}, HP {hp}")
        print(f"    from CP+HP only : {describe(solve(gd, name, cp, hp))}")
        exact = solve(
            gd, name, cp, hp,
            attack_bar=truth.attack, defense_bar=truth.defense,
            stamina_bar=truth.stamina,
        )
        print(f"    with appraisal  : {describe(exact)}")
