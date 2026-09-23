#!/usr/bin/env python3
"""
Build gamedata.sqlite from the PokeMiners game_master dump.

    python3 tools/build_gamedata.py --fetch          # download + build
    python3 tools/build_gamedata.py --input gm.json  # build from local copy

Produces a compact read-only SQLite the iOS app bundles. Regenerate whenever
Niantic ships a balance patch or a new generation.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.request

GM_URL = "https://raw.githubusercontent.com/PokeMiners/game_masters/master/latest/latest.json"

# Forms that are purely cosmetic re-skins of the base species. They share base
# stats with the default form, so collapsing them keeps the table small without
# losing solver accuracy.
COSMETIC_FORM_RE = re.compile(
    r"_(NORMAL|FALL_\d+|COPY_\d+|VS_\d+|\d{4}|WINTER_\d+|SUMMER_\d+|SPRING_\d+|"
    r"ADVENTURE_HAT_\d+|FLYING_\d+|COSTUME_\d+|HALLOWEEN_\d+|JAN_\d+)$"
)

SCHEMA = """
PRAGMA journal_mode = DELETE;

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per (species, form) that has distinct base stats.
CREATE TABLE species (
    template_id      TEXT PRIMARY KEY,
    dex              INTEGER NOT NULL,
    pokemon_id       TEXT    NOT NULL,
    form             TEXT,
    display_name     TEXT    NOT NULL,
    base_attack      INTEGER NOT NULL,
    base_defense     INTEGER NOT NULL,
    base_stamina     INTEGER NOT NULL,
    type1            TEXT,
    type2            TEXT,
    family_id        TEXT,
    candy_to_evolve  INTEGER,
    km_buddy_distance REAL,
    is_default_form  INTEGER NOT NULL DEFAULT 0,
    rarity           TEXT    -- 'legendary', 'mythic', 'ultra_beast', or NULL for normal
);

CREATE INDEX idx_species_dex        ON species(dex);
CREATE INDEX idx_species_pokemon_id ON species(pokemon_id);
CREATE INDEX idx_species_name       ON species(display_name);

-- Combat Power Multiplier, indexed by half-level * 2 so levels stay integers.
-- level_x2 = 2  -> level 1.0
-- level_x2 = 3  -> level 1.5
CREATE TABLE cpm (
    level_x2   INTEGER PRIMARY KEY,
    multiplier REAL NOT NULL
);

-- Stardust cost to power up, keyed the same way. Used to bound the level search.
CREATE TABLE powerup_cost (
    level_x2  INTEGER PRIMARY KEY,
    stardust  INTEGER NOT NULL,
    candy     INTEGER NOT NULL,
    xl_candy  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE evolution (
    from_template TEXT NOT NULL,
    to_pokemon_id TEXT NOT NULL,
    to_form       TEXT,
    candy_cost    INTEGER,
    item          TEXT,
    PRIMARY KEY (from_template, to_pokemon_id, to_form)
);

-- All moves that exist in the game, with PvE and PvP stats.
CREATE TABLE move (
    move_id         TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    move_type       TEXT NOT NULL,   -- 'fast' or 'charged'
    poke_type       TEXT,
    pve_power       INTEGER,
    pve_energy      INTEGER,         -- energyDelta: >0 for fast (generates), <0 for charged (costs)
    pve_duration_ms INTEGER,
    pvp_power       INTEGER,
    pvp_energy      INTEGER,
    pvp_duration_ms INTEGER
);
CREATE INDEX idx_move_type ON move(move_type);

-- Which moves each species can learn.
CREATE TABLE species_move (
    template_id TEXT NOT NULL,
    move_id     TEXT NOT NULL,
    move_slot   TEXT NOT NULL,   -- 'fast' or 'charged'
    PRIMARY KEY (template_id, move_id)
);
CREATE INDEX idx_species_move ON species_move(template_id);
"""


def fetch(url: str) -> list:
    print(f"fetching {url}", file=sys.stderr)
    with urllib.request.urlopen(url, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def pretty_name(pokemon_id: str, form: str | None) -> str:
    base = pokemon_id.replace("_", " ").title()
    # Nidoran gender, Farfetch'd, Mr. Mime and friends need a little help.
    fixes = {
        "Nidoran Female": "Nidoran\u2640",
        "Nidoran Male": "Nidoran\u2642",
        "Farfetchd": "Farfetch'd",
        "Mr Mime": "Mr. Mime",
        "Mime Jr": "Mime Jr.",
        "Ho Oh": "Ho-Oh",
        "Porygon Z": "Porygon-Z",
        "Type Null": "Type: Null",
        "Jangmo O": "Jangmo-o",
        "Hakamo O": "Hakamo-o",
        "Kommo O": "Kommo-o",
    }
    base = fixes.get(base, base)
    if not form:
        return base
    suffix = form.replace(f"{pokemon_id}_", "", 1).replace("_", " ").title()
    if suffix in ("Normal", ""):
        return base
    return f"{base} ({suffix})"


def move_display_name(move_id: str) -> str:
    """SHADOW_CLAW_FAST → 'Shadow Claw',  SHADOW_BALL → 'Shadow Ball'."""
    name = move_id
    if name.endswith("_FAST"):
        name = name[:-5]
    return name.replace("_", " ").title()


def half_level_cpm(whole: list[float]) -> dict[int, float]:
    """
    game_master only publishes whole-level multipliers. Half levels sit at the
    quadratic midpoint between neighbours:  sqrt((a^2 + b^2) / 2)
    """
    out: dict[int, float] = {}
    for i, m in enumerate(whole):
        level = i + 1
        out[level * 2] = m
        if i + 1 < len(whole):
            nxt = whole[i + 1]
            out[level * 2 + 1] = ((m * m + nxt * nxt) / 2) ** 0.5
    return out


# The published CPM table runs to level 55, but those top entries are NOT
# player-attainable. Pokémon max out at level 50 by powering up. Levels 50.5
# and 51 are reachable only as a Best Buddy boost on a level 49.5/50 Pokémon,
# and the level 55 multiplier exists solely for team leaders in Master League
# Battle Training.
#
# Searching past 51 lets the solver return levels no real Pokémon can be at,
# which produces phantom candidates.
PLAYER_ATTAINABLE_MAX_LEVEL_X2 = 102   # level 51.0, i.e. Best Buddy on a L50


def max_meaningful_level_x2(cpm: dict[int, float]) -> int:
    return min(PLAYER_ATTAINABLE_MAX_LEVEL_X2, max(cpm))


# Stardust / candy to advance one half level. Flat within each band.
# (max_level_inclusive, stardust, candy, xl_candy)
#
# CAUTION: these are approximate. Niantic does not publish the cost tables and
# the community figures disagree on the level 40-50 stardust total (sources
# cite both 250,000 and 296,000). The XL total of 296 candy for 40 -> 50 is
# well established; the per-step split below is a reasonable reconstruction,
# not a verified one.
#
# This table is used only to narrow the level search when the power-up cost is
# legible on screen. It is an optimisation, never a source of truth, and the
# solver is correct without it. Do not surface these numbers to the user as
# fact.
POWERUP_BANDS = [
    (10.0, 200, 1, 0), (12.5, 400, 1, 0), (15.0, 600, 1, 0),
    (17.5, 800, 1, 0), (20.0, 1000, 1, 0), (22.5, 1300, 2, 0),
    (25.0, 1600, 2, 0), (27.5, 1900, 2, 0), (30.0, 2200, 2, 0),
    (32.5, 2500, 3, 0), (35.0, 3000, 3, 0), (37.5, 3500, 3, 0),
    (40.0, 4000, 4, 0), (42.0, 10000, 0, 10), (44.0, 11000, 0, 12),
    (46.0, 12000, 0, 15), (48.0, 13000, 0, 17), (51.0, 15000, 0, 20),
]


def powerup_cost_for(level: float) -> tuple[int, int, int]:
    for cap, dust, candy, xl in POWERUP_BANDS:
        if level <= cap:
            return dust, candy, xl
    return POWERUP_BANDS[-1][1:]


def build(gm: list, out_path: str) -> None:
    if os.path.exists(out_path):
        os.remove(out_path)
    db = sqlite3.connect(out_path)
    db.executescript(SCHEMA)

    # ---- CPM ------------------------------------------------------------
    whole = None
    for entry in gm:
        if entry.get("templateId") == "PLAYER_LEVEL_SETTINGS":
            whole = entry["data"]["playerLevel"]["cpMultiplier"]
            break
    if whole is None:
        raise SystemExit("PLAYER_LEVEL_SETTINGS not found in game_master")

    cpm = half_level_cpm(whole)
    cap_x2 = max_meaningful_level_x2(cpm)
    cpm = {k: v for k, v in cpm.items() if k <= cap_x2}

    db.executemany(
        "INSERT INTO cpm (level_x2, multiplier) VALUES (?, ?)",
        sorted(cpm.items()),
    )
    db.executemany(
        "INSERT INTO powerup_cost (level_x2, stardust, candy, xl_candy) VALUES (?,?,?,?)",
        [(lx2, *powerup_cost_for(lx2 / 2)) for lx2 in sorted(cpm)],
    )

    # ---- species --------------------------------------------------------
    seen_stats: dict[tuple, str] = {}
    rows, evo_rows = [], []

    for entry in gm:
        data = entry.get("data") or {}
        ps = data.get("pokemonSettings")
        if not ps or "stats" not in ps:
            continue
        stats = ps["stats"]
        if not all(k in stats for k in ("baseAttack", "baseDefense", "baseStamina")):
            continue

        template = entry["templateId"]
        m = re.match(r"^V(\d+)_POKEMON_", template)
        if not m:
            continue
        dex = int(m.group(1))
        pid = ps["pokemonId"]
        form = ps.get("form")

        # Collapse cosmetic forms onto the default entry.
        if form and COSMETIC_FORM_RE.search(form):
            continue

        # Include form so that regional variants with identical base stats
        # (e.g. Galarian Stunfisk) are never collapsed onto the default form.
        key = (pid, form or "", stats["baseAttack"], stats["baseDefense"], stats["baseStamina"])
        is_default = 1 if form is None else 0
        if key in seen_stats and not is_default:
            continue
        seen_stats[key] = template

        rarity_raw = (ps.get("pokemonClass") or "").replace("POKEMON_CLASS_", "").lower()
        rarity = rarity_raw if rarity_raw in ("legendary", "mythic", "ultra_beast") else None
        rows.append((
            template, dex, pid, form, pretty_name(pid, form),
            stats["baseAttack"], stats["baseDefense"], stats["baseStamina"],
            ps.get("type", "").replace("POKEMON_TYPE_", "") or None,
            ps.get("type2", "").replace("POKEMON_TYPE_", "") or None,
            ps.get("familyId"), ps.get("candyToEvolve"),
            ps.get("kmBuddyDistance"), is_default,
            rarity,
        ))

        for branch in ps.get("evolutionBranch", []) or []:
            if "evolution" not in branch:
                continue
            evo_rows.append((
                template, branch["evolution"], branch.get("form"),
                branch.get("candyCost"), branch.get("evolutionItemRequirement"),
            ))

    db.executemany(
        "INSERT OR REPLACE INTO species VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    db.executemany(
        "INSERT OR REPLACE INTO evolution VALUES (?,?,?,?,?)", evo_rows
    )

    # ---- moves ----------------------------------------------------------
    pve_moves: dict[str, dict] = {}
    for entry in gm:
        tid = entry.get("templateId", "")
        if not re.match(r"^V\d+_MOVE_", tid):
            continue
        ms = (entry.get("data") or {}).get("moveSettings")
        if not ms:
            continue
        mid = ms.get("movementId", "")
        if not isinstance(mid, str) or not mid:
            continue
        pve_moves[mid] = {
            "poke_type": (ms.get("pokemonType") or "").replace("POKEMON_TYPE_", "") or None,
            "pve_power": int(ms.get("power") or 0),
            "pve_energy": int(ms.get("energyDelta") or 0),
            "pve_duration_ms": int(ms.get("durationMs") or 0),
        }

    pvp_moves: dict[str, dict] = {}
    for entry in gm:
        tid = entry.get("templateId", "")
        if not re.match(r"^COMBAT_V\d+_MOVE_", tid):
            continue
        cm = (entry.get("data") or {}).get("combatMove")
        if not cm:
            continue
        mid = cm.get("uniqueId", "")
        if not isinstance(mid, str) or not mid:
            continue
        pvp_moves[mid] = {
            "pvp_power": int(cm.get("power") or 0),
            "pvp_energy": int(cm.get("energyDelta") or 0),
            "pvp_duration_ms": int(cm.get("durationMs") or 500),
        }

    all_move_ids = set(pve_moves) | set(pvp_moves)
    move_rows = []
    for mid in all_move_ids:
        pve = pve_moves.get(mid, {})
        pvp = pvp_moves.get(mid, {})
        move_rows.append((
            mid,
            move_display_name(mid),
            "fast" if mid.endswith("_FAST") else "charged",
            pve.get("poke_type"),
            pve.get("pve_power"), pve.get("pve_energy"), pve.get("pve_duration_ms"),
            pvp.get("pvp_power"), pvp.get("pvp_energy"), pvp.get("pvp_duration_ms"),
        ))
    db.executemany("INSERT OR REPLACE INTO move VALUES (?,?,?,?,?,?,?,?,?,?)", move_rows)

    inserted_templates = {r[0] for r in rows}
    sm_rows = []
    for entry in gm:
        data = entry.get("data") or {}
        ps = data.get("pokemonSettings")
        if not ps:
            continue
        template = entry["templateId"]
        if template not in inserted_templates:
            continue
        for mid in (ps.get("quickMoves") or []):
            if mid in all_move_ids:
                sm_rows.append((template, mid, "fast"))
        for mid in (ps.get("cinematicMoves") or []):
            if mid in all_move_ids:
                sm_rows.append((template, mid, "charged"))
    db.executemany("INSERT OR IGNORE INTO species_move VALUES (?,?,?)", sm_rows)

    db.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        [
            ("schema_version", "2"),
            ("max_level_x2", str(cap_x2)),
            ("max_level", str(cap_x2 / 2)),
            ("species_count", str(len(rows))),
            ("source", GM_URL),
        ],
    )
    db.commit()
    db.execute("VACUUM")
    db.close()

    size = os.path.getsize(out_path) / 1024
    print(
        f"wrote {out_path}: {len(rows)} species, {len(cpm)} cpm levels "
        f"(max level {cap_x2 / 2}), {len(evo_rows)} evolutions, "
        f"{len(move_rows)} moves, {len(sm_rows)} species-move links, {size:.0f} KB",
        file=sys.stderr,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--input", default="gm.json")
    ap.add_argument("--output", default="gamedata.sqlite")
    args = ap.parse_args()

    gm = fetch(GM_URL) if args.fetch else json.load(open(args.input))
    build(gm, args.output)


if __name__ == "__main__":
    main()
