#!/usr/bin/env python3
"""
Appraisal service for the iOS Shortcuts route.

Accepts a Pokémon GO appraisal screenshot as a raw POST body, measures the
stat bars, OCRs the species and max HP, solves the IVs, applies the rules, and
returns a verdict. Optionally forwards it to ntfy.sh as a push notification.

    python3 tools/shortcut_server.py --port 8765
    curl --data-binary @shot.PNG http://localhost:8765/appraise

Needs no Apple developer account, no code signing, and no App Store. The iOS
side is a Shortcut bound to Back Tap; see README for the recipe.

Dependencies: pillow, pytesseract, and the tesseract-ocr binary.

CP is deliberately *derived* rather than trusted from OCR. Given the species,
the stamina IV from the bar, and max HP, the level is pinned directly — and CP
follows from the level. CP text sits on an arbitrary photographic background
and is the least reliable thing on the screen, so when OCR does manage to read
it, it is used as a cross-check rather than an input.
"""
from __future__ import annotations

import argparse
import html
import io
import json
import os
import sys
import threading
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from calibrate import GEO, card_is_open, detect_dynamax, detect_shadow, measure_bar, star_tier  # noqa: E402
from collection_db import CollectionDB  # noqa: E402
from solver_ref import GameData, IVSet, compute_cp, compute_hp  # noqa: E402

try:
    from PIL import Image
    import numpy as np
except ImportError:
    sys.exit("needs pillow and numpy:  pip install pillow numpy")

try:
    import pytesseract
    pytesseract.get_tesseract_version()  # raises if binary is missing
except Exception:
    pytesseract = None


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------

TEXT_ROI = {
    "name": (0.20, 0.380, 0.80, 0.442),
    "hp":   (0.20, 0.448, 0.80, 0.492),
}
CP_ROI = (0.26, 0.045, 0.74, 0.100)


def _ocr(image: "Image.Image", config: str) -> str:
    if pytesseract is None:
        return ""
    return pytesseract.image_to_string(image, config=config).strip()


def read_text(im: "Image.Image", roi, scale: int = 2, config: str = "--psm 7") -> str:
    W, H = im.size
    x0, y0, x1, y1 = roi
    crop = im.crop((int(x0 * W), int(y0 * H), int(x1 * W), int(y1 * H)))
    crop = crop.resize((crop.width * scale, crop.height * scale), Image.LANCZOS)
    return _ocr(crop.convert("L"), config)


def read_cp(im: "Image.Image") -> int | None:
    """
    CP is white text over whatever the catch background happened to be.
    Thresholding to near-white and inverting gives tesseract a fighting chance,
    but it still fails on bright backgrounds — which is fine, because CP is
    optional here.
    """
    W, H = im.size
    x0, y0, x1, y1 = CP_ROI
    crop = im.crop((int(x0 * W), int(y0 * H), int(x1 * W), int(y1 * H))).convert("RGB")
    arr = np.asarray(crop).astype(int)
    mask = arr.min(axis=2) > 205
    if mask.mean() > 0.45:          # background is itself near-white; hopeless
        return None
    binary = Image.fromarray(np.where(mask, 0, 255).astype("uint8"))
    binary = binary.resize((binary.width * 3, binary.height * 3), Image.LANCZOS)

    for psm in (8, 13, 7):
        text = _ocr(binary, f"--psm {psm} -c tessedit_char_whitelist=CP0123456789")
        digits = "".join(c for c in text if c.isdigit())
        if digits and 10 <= int(digits) <= 6000:
            return int(digits)
    return None


def parse_max_hp(text: str) -> int | None:
    """
    Displayed as "current / max", e.g. "81 / 136 HP" on a damaged Pokémon.
    The solver needs max, so take the largest number present.
    """
    numbers = [int(t) for t in "".join(
        c if c.isdigit() else " " for c in text
    ).split() if t.isdigit()]
    return max(numbers) if numbers else None


# ---------------------------------------------------------------------------
# Solving
# ---------------------------------------------------------------------------

def levels_matching_hp(gd: GameData, species, stamina_iv: int, hp: int) -> list[int]:
    return [lx2 for lx2, m in sorted(gd.cpm.items())
            if compute_hp(species, stamina_iv, m) == hp]


def appraise(gd, rules_fn, image_bytes: bytes, collection=None) -> dict:
    im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    W, H = im.size
    px = im.load()

    card_open, card_top = card_is_open(px, W, H, GEO)
    if not card_open:
        # Log what the edge scan actually found so we can diagnose layout changes.
        x0, x1 = int(0.10 * W), int(0.90 * W)
        step = max(1, (x1 - x0) // 40)
        n_samples = len(range(x0, x1, step))
        best_white = max(
            (sum(1 for ix in range(x0, x1, step) if all(c > 235 for c in px[ix, iy][:3])) / n_samples, iy)
            for iy in range(int(0.60 * H), int(0.82 * H))
        )
        sys.stderr.write(
            f"  [card_not_open] {W}x{H}  best white row: y={best_white[1]} "
            f"({best_white[1]/H:.4f}) white={best_white[0]:.2%}  "
            f"candidates tried={GEO.card_top_candidates}\n"
        )
        return {"ok": False, "failureKind": "card_not_open",
                "error": "appraisal card not open in this screenshot"}

    if card_top > GEO.card_top_alt_threshold:
        bar_offsets = (GEO.bar_attack_offset_alt, GEO.bar_defense_offset_alt, GEO.bar_stamina_offset_alt)
    else:
        bar_offsets = (GEO.bar_attack_offset, GEO.bar_defense_offset, GEO.bar_stamina_offset)

    bars = {}
    for stat, offset in zip(("attack", "defense", "stamina"), bar_offsets):
        iv, raw, conf = measure_bar(px, W, H, card_top + offset, GEO)
        if iv is None:
            return {"ok": False, "failureKind": "bar_read",
                    "error": f"{stat} bar did not read cleanly"}
        bars[stat] = {"iv": iv, "raw": raw, "confidence": conf}

    confidence = min(b["confidence"] for b in bars.values())
    iv_certain = confidence >= 0.5

    name_text = read_text(im, TEXT_ROI["name"])
    hp_text = read_text(im, TEXT_ROI["hp"])
    max_hp = parse_max_hp(hp_text)

    try:
        species = gd.best_species_match(name_text)
    except KeyError as exc:
        return {"ok": False, "failureKind": "ocr_species",
                "error": str(exc), "ocr": {"name": name_text}}

    a, d, s = bars["attack"]["iv"], bars["defense"]["iv"], bars["stamina"]["iv"]
    total = a + d + s

    result = {
        "ok": True,
        "iv_certain": iv_certain,
        "species": species.display_name,
        "speciesTemplateID": species.template_id,
        "ivs": [a, d, s],
        "total": total,
        "percent": round(total / 45 * 100, 1),
        "stars": star_tier(total),
        "confidence": confidence,
        "maxHP": max_hp,
        "ocr": {"name": name_text, "hp": hp_text},
        # Flags — all unknown until pixel detection is implemented for each.
        # Rules treat None as "unknown", never as False.
        "isShiny":    None,  # TODO: sparkle glyph next to CP text
        "isShadow":   detect_shadow(im, GEO) or None,
        "isPurified": None,  # TODO: blue swirl near sprite
        "isLucky":    None,  # TODO: yellow-orange sparkle background
        "isCostume":  None,  # TODO: hat/accessory on sprite (or template_id suffix)
        "sizeClass":  None,  # TODO: XXL/XXS badge — ROI exists in calibrate.GEO.roi_size
        "isDynamax":  detect_dynamax(im, GEO) or None,
    }

    # Derive level and CP from HP rather than trusting CP OCR.
    if max_hp:
        levels = levels_matching_hp(gd, species, s, max_hp)
        derived = [(lx2 / 2, compute_cp(species, IVSet(a, d, s, lx2), gd.cpm[lx2]))
                   for lx2 in levels]
        result["candidateLevels"] = [lv for lv, _ in derived]
        result["candidateCP"] = [cp for _, cp in derived]

        ocr_cp = read_cp(im)
        result["ocr"]["cp"] = ocr_cp
        if ocr_cp is not None:
            narrowed = [(lv, cp) for lv, cp in derived if cp == ocr_cp]
            if narrowed:
                result["level"], result["cp"] = narrowed[0]
                result["cpSource"] = "derived, confirmed by OCR"
            else:
                result["cpSource"] = "OCR disagreed with derivation — ignoring OCR"
        if "cp" not in result and len(derived) == 1:
            result["level"], result["cp"] = derived[0]
            result["cpSource"] = "derived (HP pinned the level uniquely)"

    # PvP stat-product ranks for Great League and Ultra League.
    # Computed from bars (exact IVs), so only available when bars are certain.
    pvp: dict = {}
    for league_name, cap in (("greatLeague", 1500), ("ultraLeague", 2500)):
        rank, total_spreads = gd.pvp_rank(species.template_id, a, d, s, cap)
        pvp[league_name] = {
            "rank": rank,
            "total": total_spreads,
            "percentile": round((1 - (rank - 1) / total_spreads) * 100, 1),
        }
    result["pvp"] = pvp

    # Box context: same-species count and best IV total seen so far.
    # Family context: best IV total across the whole evolutionary line.
    if collection is not None:
        result["boxContext"] = collection.box_context(species.template_id)
        fam_id = gd.family_id_for(species.template_id)
        if fam_id:
            result["familyContext"] = collection.family_context(
                gd.family_members(fam_id)
            )

    rules_out = rules_fn(species, result)
    if not iv_certain:
        rules_out.setdefault("notes", []).insert(
            0, f"IV bars uncertain (confidence {confidence:.2f}) — IVs stored as best estimate"
        )
    result.update(rules_out)

    moves = gd.best_moves(species.template_id)
    if moves:
        result["bestMoves"] = moves
        # Append a move shortlist as a note for actionable verdicts.
        if result.get("verdict") in ("KEEP", "BUILD", "BUDDY"):
            tag = result.get("suggestedTag") or ""
            use_pvp = tag in ("Great", "Ultra")
            mv = moves.get("pvp" if use_pvp else "raid", {})
            fast = mv.get("fast")
            charged = mv.get("charged") or []
            if fast or charged:
                move_str = f"Best moves: {fast}" if fast else "Best moves:"
                if charged:
                    move_str += " + " + " / ".join(charged)
                result.setdefault("notes", []).append(move_str)

    return result


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

def load_rules(path: str | None) -> dict:
    default = {
        "keepSpecies": ["Mewtwo", "Zygarde", "Charizard", "Porygon"],
        "buildSpecies": [
            "Gabite", "Gible", "Beldum", "Metang", "Mankey", "Marill",
            "Meditite", "Ralts", "Togepi", "Togetic", "Absol", "Mareep",
            "Makuhita", "Sableye", "Froakie", "Deino", "Charmander",
        ],
        "buddyQueue": ["Deino", "Beldum", "Gabite"],
        "notableIV": 37,
        "transferIVCeiling": 23,
    }
    if path and os.path.exists(path):
        default.update(json.load(open(path)))
    return default


_VERDICT_STRENGTH = {
    "KEEP": 6, "BUILD": 5, "BUDDY": 4, "TRADE": 3, "TRANSFER": 2, "UNDECIDED": 1,
}

# Canonical Pokémon GO tag names → priority for suggestedTag selection.
# Single source of truth for the valid tag list (also used by the web UI).
_TAG_PRIORITY = {
    "Shiny": 8, "Max": 7,
    "Great": 6, "Ultra": 6, "Raid": 5,
    "Desired Evo": 3, "Keep": 2,
    "Trade": 1, "Transfer": 0,
}
USER_TAGS: tuple[str, ...] = tuple(_TAG_PRIORITY)
_USER_TAG_AUTO = "auto"


def _parse_user_tag(value) -> tuple[bool, str | None]:
    """
    Validate a manual tag from a PATCH body.

    None / "" / "auto" clear the override → (True, None); a case-insensitive
    match of USER_TAGS → (True, canonical name); anything else → (False, None).
    """
    if value is None:
        return True, None
    if not isinstance(value, str):
        return False, None
    key = value.strip().lower()
    if key in ("", _USER_TAG_AUTO):
        return True, None
    for tag in USER_TAGS:
        if tag.lower() == key:
            return True, tag
    return False, None


def make_rules_fn(rules: dict, gd: "GameData | None" = None):
    """
    Build the rules-evaluation function.

    Returns a callable evaluate(species, result) -> dict with keys:
      verdict      — strongest fired outcome
      suggestedTag — in-game tag to apply (Keep/Build/Buddy/Trade), or None
      reasons      — every reason that fired, ordered by verdict strength
    """
    def _family_match(key: str, species) -> bool:
        """True when any listed name shares a family_id with the species."""
        if gd is None:
            return any(n.lower() == species.display_name.lower() for n in rules[key])
        sp_family = gd.family_id_for(species.template_id)
        for listed_name in rules[key]:
            try:
                listed_sp = gd.best_species_match(listed_name)
            except KeyError:
                continue
            if listed_sp.is_default_form:
                if gd.family_id_for(listed_sp.template_id) == sp_family:
                    return True
            else:
                # Non-default form (regional variant): walk the evolution tree
                # forward from this specific form rather than matching the whole
                # family, so "Galarian Stunfisk" cannot leak onto Kantonian Stunfisk.
                if species.template_id in gd.evo_forward_cluster(listed_sp.template_id):
                    return True
        return False

    def _buddy_pos(species) -> int | None:
        """0-based position in buddyQueue by family, or None if not listed."""
        for i, listed_name in enumerate(rules.get("buddyQueue", [])):
            if gd is not None:
                try:
                    listed_sp = gd.best_species_match(listed_name)
                    if gd.family_id_for(listed_sp.template_id) == gd.family_id_for(species.template_id):
                        return i
                except KeyError:
                    pass
            elif listed_name.lower() == species.display_name.lower():
                return i
        return None

    def _family_match_any(names: list, species) -> bool:
        """True when any name in the list shares a family with the species."""
        if gd is None:
            return any(n.lower() == species.display_name.lower() for n in names)
        sp_family = gd.family_id_for(species.template_id)
        for listed_name in names:
            try:
                listed_sp = gd.best_species_match(listed_name)
            except KeyError:
                continue
            if listed_sp.is_default_form:
                if gd.family_id_for(listed_sp.template_id) == sp_family:
                    return True
            else:
                if species.template_id in gd.evo_forward_cluster(listed_sp.template_id):
                    return True
        return False

    # ── startup validation ──────────────────────────────────────────────────
    if gd is not None:
        _all_rule_names = set(
            rules.get("keepSpecies", [])
            + rules.get("buildSpecies", [])
            + rules.get("buddyQueue", [])
            + [sp for pr in rules.get("purposeRules", []) for sp in pr.get("species", [])]
            + rules.get("gigantamaxSpecies", [])
            + rules.get("dynamaxSpecies", [])
        )
        for _n in sorted(_all_rule_names):
            try:
                gd.best_species_match(_n)
            except KeyError:
                sys.stderr.write(
                    f"  [rules] WARNING: {_n!r} cannot be resolved — rule will never fire\n"
                )

    def evaluate(species, result: dict) -> dict:
        name = species.display_name
        total = result.get("total", 0)
        pct = result.get("percent", 0.0)

        # fired: list of (verdict, suggested_tag, reason_text)
        fired: list[tuple[str, str | None, str]] = []
        # notes: informational observations that don't affect the verdict
        notes: list[str] = []

        # ── legendary / mythical / ultra beast guard ─────────────────────────
        # These can never be re-caught without a repeat event or Special
        # Research.  The IV floor for a trade can be set later; never transfer.
        _rarity = getattr(species, "rarity", None)
        if _rarity in ("legendary", "mythic", "ultra_beast"):
            _rarity_label = {"legendary": "Legendary", "mythic": "Mythical",
                             "ultra_beast": "Ultra Beast"}.get(_rarity, _rarity.title())
            fired.append(("KEEP", "Keep",
                           f"{name} — {_rarity_label}; never transfer"))

        # ── shiny rule ──────────────────────────────────────────────────────
        # Only fires on True. None = unknown → do not infer absence.
        if result.get("isShiny") is True:
            fired.append(("KEEP", "Shiny", "shiny, irreplaceable"))

        # ── keep list ───────────────────────────────────────────────────────
        if _family_match("keepSpecies", species):
            fired.append(("KEEP", "Keep", f"{name} is on your Keep list — not re-catchable"))

        # ── buddy queue ─────────────────────────────────────────────────────
        buddy_pos = _buddy_pos(species)
        if buddy_pos == 0:
            fired.append(("BUDDY", "Desired Evo", f"Top of your buddy queue"))
        elif buddy_pos is not None:
            fired.append(("BUILD", "Desired Evo", f"Buddy queue position {buddy_pos + 1}"))

        # ── build list ──────────────────────────────────────────────────────
        if _family_match("buildSpecies", species):
            box = result.get("boxContext") or {}
            best_seen = box.get("bestIVTotal")
            if best_seen is not None and best_seen > total:
                # We have a better specimen already → trade this for Lucky
                fired.append(("TRADE", "Trade",
                               f"{name} is a build target, but you have a better one "
                               f"({best_seen}/45 seen vs {total}/45 this one)"))
            else:
                fired.append(("BUILD", "Desired Evo",
                               f"{name} evolves into something worth investing in"))

        # ── purpose rules ────────────────────────────────────────────────────
        # Purposes split into categories; each fires independently so that a
        # dual raid+GL Pokémon is not auto-traded away just because a better
        # raid specimen exists — it may still be the optimal GL/UL build.
        # Raid and ML use total-IV comparison (high IVs help).
        # GL/UL never auto-trade: IV total is the wrong metric under a CP cap.
        for pr in rules.get("purposeRules", []):
            if not _family_match_any(pr.get("species", []), species):
                continue
            purposes = pr.get("purposes", [])
            verd = pr.get("verdict", "BUILD")

            raid_ps = [p for p in purposes if p.startswith("Raid:")]
            gl_ps   = [p for p in purposes if "Great League"  in p]
            ul_ps   = [p for p in purposes if "Ultra League"  in p]
            ml_ps   = [p for p in purposes if "Master League" in p]

            box = result.get("boxContext") or {}
            best_seen = box.get("bestIVTotal")

            for cat_label, cat_ps, use_trade in [
                ("Raid",  raid_ps, True),
                ("Great", gl_ps,   False),
                ("Ultra", ul_ps,   False),
                ("Raid",  ml_ps,   True),
            ]:
                if not cat_ps:
                    continue
                cat_str = " · ".join(cat_ps)
                if verd == "BUILD":
                    if use_trade and best_seen is not None and best_seen > total:
                        fired.append(("TRADE", "Trade",
                                       f"{name} ({cat_str}) — "
                                       f"you have a better one ({best_seen}/45 vs {total}/45)"))
                    else:
                        fired.append((verd, cat_label, f"{name} — {cat_str}"))
                else:
                    fired.append((verd, cat_label, f"{name} — {cat_str}"))

        # ── Gigantamax / Dynamax ──────────────────────────────────────────────
        if result.get("isDynamax") is True:
            gmax_list = rules.get("gigantamaxSpecies", [])
            dmax_list = rules.get("dynamaxSpecies", [])
            if _family_match_any(gmax_list, species):
                fired.append(("KEEP", "Max",
                               f"Gigantamax {name} — G-Max move beats regular Dynamax; "
                               f"only available from G-Max raids"))
            elif _family_match_any(dmax_list, species):
                fired.append(("BUILD", "Max",
                               f"Dynamax {name} — strong Max Moves for Max Battles; "
                               f"worth keeping for Dynamax raid teams"))
            else:
                # Pokémon from a Dynamax raid but not on either list: weak BUILD
                # signal — any species can participate in Max Battles with its Max
                # Move, so it's worth keeping for type-coverage utility.
                fired.append(("BUILD", "Max",
                               f"Dynamax {name} — Max Battle participant; "
                               f"keep for type-coverage in Max Battles"))

        # ── shadow rule ─────────────────────────────────────────────────────
        # Placed here so _meta_fired sees all prior rules: keep list, build
        # list, purpose rules, and Dynamax. Both the note and the KEEP
        # promotion are gated on _meta_fired — shadow on a junk species (e.g.
        # Bidoof) gets nothing; shadow on a meta species gets KEEP + note.
        # Only fires on True; None = not yet detected → do not infer absence.
        if result.get("isShadow") is True:
            _meta_fired = any(v in ("BUILD", "KEEP") for v, _, _ in fired)
            if _meta_fired:
                notes.append(
                    "Shadow: +20% Attack — do NOT purify; shadow bonus permanently "
                    "beats any IV improvement from purification"
                )
                fired.append(("KEEP", "Keep",
                               f"Shadow {name} — +20% Attack makes this a premium raid attacker"))

        # ── attack IV advisory ──────────────────────────────────────────────
        # Attack IV is not square-root dampened in the DPS formula, so for the
        # same IV total a higher Attack beats higher Def/Sta in raid output.
        # For GL/UL the opposite holds: low Attack lets the Pokémon level up
        # further under the CP cap, gaining bulk.  The pvp_rank check already
        # handles GL/UL correctly; this advisory only fires for raid/ML species
        # to flag a sub-15 Attack so the user knows to compare with it.
        # Mixed-purpose species (e.g. Swampert = raid + GL) are skipped to
        # avoid contradicting advice.
        _ivs = result.get("ivs") or []
        _atk = _ivs[0] if len(_ivs) >= 1 else None
        if _atk is not None:
            _has_raid_ml = any(
                "Raid:" in r or "Master League PvP" in r
                for _, _, r in fired
            )
            _has_gl_ul = any(
                "Great League PvP" in r or "Ultra League PvP" in r
                for _, _, r in fired
            )
            if _has_raid_ml and not _has_gl_ul and _atk < 15:
                notes.append(f"Attack IV {_atk}/15 — Attack matters most for raids/ML "
                              f"(not square-root dampened); compare against 15-Attack specimens")
            elif _has_gl_ul and not _has_raid_ml and _atk > 8:
                notes.append(f"Attack IV {_atk}/15 — GL/UL prefers low Attack for "
                              f"bulk under the CP cap; check PvP rank")

        # ── PvP rank ────────────────────────────────────────────────────────
        # Only fires for species already on the GL/UL purpose list — every
        # species has a rank 1; rank alone is not a reason to hold a Chansey.
        pvp = result.get("pvp") or {}
        _gl_purpose = any("Great League" in r for _, _, r in fired)
        _ul_purpose = any("Ultra League" in r for _, _, r in fired)
        for league, label, has_purpose in (
            ("greatLeague", "Great", _gl_purpose),
            ("ultraLeague", "Ultra", _ul_purpose),
        ):
            if not has_purpose:
                continue
            lg = pvp.get(league) or {}
            rank = lg.get("rank")
            total_spreads = lg.get("total", 4096)
            if rank is not None and rank <= 25:
                fired.append(("KEEP", label,
                               f"{label} rank {rank} of {total_spreads}"
                               " — raw IV total is misleading for PvP"))

        # ── notable IVs ──────────────────────────────────────────────────────
        # notableIV in rules.json (default 45 = hundo only).  A perfect
        # 15/15/15 is always worth keeping: tradeable for Lucky and
        # future-proof against meta shifts.  Setting notableIV lower lets the
        # user auto-KEEP near-perfect specimens even for non-meta species.
        _notable_threshold = rules.get("notableIV", 45)
        if total >= _notable_threshold:
            if total == 45:
                fired.append(("KEEP", "Keep", "hundo — always keep regardless of species"))
            else:
                fired.append(("KEEP", "Keep",
                               f"{total}/45 ({pct:.0f}%) — above notableIV threshold"))

        # ── family cross-species comparison ──────────────────────────────────
        # Fires when a *different* species in the same evolutionary line is
        # already in the collection, so you know which one to keep.
        fam = result.get("familyContext") or {}
        fam_best = fam.get("bestIVTotal")
        fam_best_name = fam.get("bestSpecies")
        if fam_best is not None and fam_best_name and fam_best_name != name:
            if fam_best > total:
                fired.append(("TRADE", "Trade",
                               f"Keep your {fam_best_name} ({fam_best}/45) — "
                               f"it outclasses this {name} ({total}/45)"))
            elif fam_best < total:
                fired.append(("BUILD", "Desired Evo",
                               f"This {name} ({total}/45) outclasses your "
                               f"{fam_best_name} ({fam_best}/45) — consider releasing the old one"))

        # ── transfer floor ───────────────────────────────────────────────────
        if total < rules.get("transferIVCeiling", 23):
            fired.append(("TRANSFER", "Transfer", f"only {pct}% — below transfer threshold"))

        # ── fallback ─────────────────────────────────────────────────────────
        # Nothing in the meta list and not a hundo → no reason to hold.
        # UNDECIDED is reserved for cases where a rule did fire but was
        # inconclusive (e.g., PvP rank borderline); silence here means transfer.
        if not fired:
            fired.append(("TRANSFER", "Transfer", "not on any meta list — transfer unless hundo"))

        # Pick strongest verdict
        fired.sort(key=lambda t: -_VERDICT_STRENGTH.get(t[0], 0))
        verdict = fired[0][0]
        reasons = [t[2] for t in fired]

        # suggestedTag: picks the most specific tag across all fired rules.
        tag_priority = _TAG_PRIORITY
        suggested_tag: str | None = None
        best_tag_score = -1
        for _, tag, _ in fired:
            if tag is not None and tag_priority.get(tag, -1) > best_tag_score:
                best_tag_score = tag_priority[tag]
                suggested_tag = tag

        return {"verdict": verdict, "suggestedTag": suggested_tag,
                "reasons": reasons, "notes": notes}

    return evaluate


def refresh_stale_verdicts(gd: "GameData", rules_fn, collection: "CollectionDB") -> int:
    """
    Re-run the rules engine against every live collection entry and persist
    any verdicts that have changed due to box/family context shifts.

    Called after each successful record() so that entries whose TRADE/BUILD
    status changed (because a better specimen just arrived) are updated
    immediately rather than staying stale until the user re-appaises them.

    Returns the number of rows updated.
    """
    with collection._lock:
        rows = collection._conn.execute("""
            SELECT o.id, o.species_template_id, o.display_name,
                   o.attack_iv, o.defense_iv, o.stamina_iv, o.iv_total,
                   o.is_shiny, o.is_shadow, o.is_purified,
                   o.is_lucky, o.is_costume, o.is_dynamax,
                   o.verdict, o.verdict_reason, o.verdict_tag
            FROM observation o
            JOIN entity e ON e.id = o.entity_id
            WHERE e.is_disposed = 0
            AND o.id = (
                SELECT id FROM observation WHERE entity_id = e.id
                ORDER BY captured_at DESC, id DESC LIMIT 1
            )
        """).fetchall()

    updates: list[tuple] = []
    for row in rows:
        try:
            species = gd.species(row["species_template_id"])
        except KeyError:
            try:
                species = gd.best_species_match(row["display_name"])
            except KeyError:
                continue

        a, d, s = row["attack_iv"], row["defense_iv"], row["stamina_iv"]
        total = row["iv_total"]
        pvp: dict = {}
        for league, cap in (("greatLeague", 1500), ("ultraLeague", 2500)):
            rank, total_spreads = gd.pvp_rank(species.template_id, a, d, s, cap)
            pvp[league] = {"rank": rank, "total": total_spreads,
                           "percentile": round((1 - (rank - 1) / total_spreads) * 100, 1)}

        fam_id = gd.family_id_for(species.template_id)
        result = {
            "total": total, "percent": round(total / 45 * 100, 1), "ivs": [a, d, s],
            "isShiny":    bool(row["is_shiny"]) or None,
            "isShadow":   bool(row["is_shadow"]) or None,
            "isPurified": bool(row["is_purified"]) or None,
            "isLucky":    bool(row["is_lucky"]) or None,
            "isCostume":  bool(row["is_costume"]) or None,
            "sizeClass":  None,
            "isDynamax":  bool(row["is_dynamax"]) or None,
            "pvp": pvp,
            "boxContext": collection.box_context(row["species_template_id"]),
            "familyContext": collection.family_context(
                gd.family_members(fam_id) if fam_id else []
            ),
        }
        r = rules_fn(species, result)
        new_verdict = r["verdict"].lower()
        new_tag     = r.get("suggestedTag") or ""
        new_reasons = "; ".join(r["reasons"])
        old_verdict = (row["verdict"] or "").lower()
        old_tag     = row["verdict_tag"] or ""
        old_reasons = row["verdict_reason"] or ""
        updates.append((row["id"], new_verdict, new_reasons, new_tag,
                        row["display_name"], old_verdict, old_tag))

    changed = 0
    if updates:
        with collection._lock:
            with collection._conn:
                for obs_id, verdict, reasons, tag, name, old_v, old_t in updates:
                    collection._conn.execute(
                        "UPDATE observation SET verdict=?, verdict_reason=?, verdict_tag=? WHERE id=?",
                        (verdict, reasons, tag, obs_id),
                    )
                    if verdict != old_v or tag != (old_t or ""):
                        old_str = f"{old_v}({old_t})" if old_t else old_v
                        new_str = f"{verdict}({tag})"  if tag    else verdict
                        sys.stderr.write(f"  re-verdict {name}: {old_str} → {new_str}\n")
                        changed += 1

    return changed


def species_buddy_km(species) -> str:  # noqa: kept for compatibility
    return ""


# ---------------------------------------------------------------------------
# Formatting and push
# ---------------------------------------------------------------------------

def format_line(r: dict) -> str:
    if not r.get("ok"):
        return f"! {r.get('error', 'unknown error')}"
    parts = [r["species"]]
    if r.get("cp"):
        parts.append(f"CP {r['cp']}")
    elif r.get("candidateCP"):
        parts.append("CP " + "/".join(str(c) for c in r["candidateCP"][:3]))
    parts.append("{}/{}/{} = {}%".format(*r["ivs"], r["percent"]))
    if r.get("level"):
        parts.append(f"L{r['level']:g}")
    tag = r.get("suggestedTag")
    verdict_part = f"{r['verdict']}"
    if tag:
        verdict_part += f" (tag: {tag})"
    moves = r.get("bestMoves", {})
    ctx = tag.lower() if tag else ""
    if "league" in ctx and moves.get("pvp"):
        mv = moves["pvp"]
        parts.append(f"{mv['fast']} / {', '.join(mv['charged'])}")
    elif moves.get("raid"):
        mv = moves["raid"]
        parts.append(f"{mv['fast']} / {', '.join(mv['charged'])}")
    return f"{verdict_part} · " + " · ".join(parts)


def push_ntfy(topic_url: str, r: dict) -> str:
    body = format_line(r).encode()
    request = urllib.request.Request(
        topic_url, data=body, method="POST",
        headers={
            "Title": r.get("species", "PokeVerdict"),
            "Tags": {"KEEP": "star", "BUILD": "hammer", "BUDDY": "footprints",
                     "TRANSFER": "wastebasket"}.get(r.get("verdict", ""), "grey_question"),
            "Priority": "default",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return f"ntfy {resp.status}"
    except Exception as exc:
        return f"ntfy failed: {exc}"


# ---------------------------------------------------------------------------
# Collection web UI
# ---------------------------------------------------------------------------

_VERDICT_COLOR = {
    "keep":     "#D4AF37",
    "build":    "#2E86AB",
    "buddy":    "#5B8C5A",
    "trade":    "#8E6C9B",
    "transfer": "#9A9A9A",
    "undecided":"#666666",
}

_TAG_COLOR = {
    "shiny":       "#D4AF37",
    "max":         "#7B2D8B",
    "raid":        "#E55B25",
    "great":       "#2E7D32",
    "ultra":       "#00695C",
    "desired evo": "#2E86AB",
    "keep":        "#D4AF37",
    "trade":       "#8E6C9B",
    "transfer":    "#9A9A9A",
}
_DEFAULT_BADGE_COLOR = "#888"

# Phone breakpoint shared by the CSS media query and the JS matchMedia guards.
_NARROW_MQ = "(max-width:640px)"
# Class on th/td cells hidden below the phone breakpoint.
_HIDE_SM = "col-hide-sm"
# (data key, label) pairs offered by the phone sort <select>.
_SORT_KEYS = (("name", "Species"), ("cp", "CP"), ("pct", "IV%"),
              ("stars", "Stars"), ("purpose", "Purpose"))

_HTML_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PokeVerdict</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font:15px/1.5 system-ui,sans-serif;background:#f2f2f2;color:#1a1a1a}
header{background:#cc0000;color:#fff;padding:.6rem 1.25rem;display:flex;align-items:center;gap:.75rem}
header h1{font-size:1.15rem;font-weight:700;letter-spacing:.02em}
header .sub{opacity:.75;font-size:.85rem}
.bar{background:#fff;border-bottom:1px solid #ddd;padding:.5rem 1.25rem;display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}
.bar input{padding:.3rem .6rem;border:1px solid #ccc;border-radius:5px;font-size:.9rem;flex:1;min-width:12rem;max-width:22rem}
.bar select{padding:.3rem .5rem;border:1px solid #ccc;border-radius:5px;font-size:.85rem;background:#fff}
.wrap{padding:1rem 1.25rem}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1);table-layout:fixed}
thead{position:sticky;top:0;z-index:1}
th{position:relative;background:#f7f7f7;color:#555;font-size:.75rem;text-transform:uppercase;letter-spacing:.05em;padding:.45rem 1.2rem .45rem .7rem;text-align:left;border-bottom:2px solid #e8e8e8;cursor:pointer;user-select:none;overflow:hidden;min-width:3rem}
th:hover{background:#eee}
th.asc::after{content:" ↑";opacity:.6}
th.desc::after{content:" ↓";opacity:.6}
.col-resize{position:absolute;right:0;top:0;width:5px;height:100%;cursor:col-resize;z-index:2}
.col-resize:hover,.col-resize.dragging{background:rgba(0,0,0,.18)}
td{padding:.42rem .7rem;border-bottom:1px solid #f0f0f0;font-size:.88rem;vertical-align:top;overflow-wrap:break-word;word-break:break-word;overflow:hidden}
td.nw,td.name,td.ivs{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
tr:last-child td{border-bottom:none}
tr.hidden{display:none}
tr:hover td{background:#f9fff5}
.name{font-weight:600}
.ivs{font-family:monospace;color:#555;font-size:.82rem}
.pct{font-weight:600}
.stars{color:#f59e0b;font-size:.9rem}
.badge{display:inline-block;padding:2px 8px;border-radius:9999px;font-size:.72rem;font-weight:700;color:#fff;text-transform:uppercase;letter-spacing:.04em}
.dim{color:#aaa;font-size:.82rem}
.flag{font-size:.85rem}
.mv{color:#444;font-size:.82rem}
.purpose{font-size:.8rem;color:#555}
.empty{padding:3rem;text-align:center;color:#aaa;font-size:.95rem}
.del-btn{background:none;border:1px solid #e0e0e0;border-radius:4px;color:#bbb;cursor:pointer;font-size:.85rem;line-height:1;padding:2px 7px}
.del-btn:hover{background:#ffeaea;border-color:#e88;color:#c00}
.edit-btn{background:none;border:1px solid #e0e0e0;border-radius:4px;color:#bbb;cursor:pointer;font-size:.8rem;line-height:1;padding:2px 6px;margin-right:3px}
.edit-btn:hover{background:#e8f4ff;border-color:#88c;color:#339}
#edit-popup{position:fixed;background:#fff;border:1px solid #ccc;border-radius:8px;padding:1rem 1.1rem;box-shadow:0 4px 16px rgba(0,0,0,.18);z-index:100;min-width:190px;display:none}
#edit-popup b{display:block;margin-bottom:.6rem;font-size:.9rem;color:#333}
#edit-popup label{display:flex;align-items:center;gap:.5rem;margin:.3rem 0;font-size:.88rem;cursor:pointer}
#edit-popup input[type=number]{width:5rem;padding:.2rem .4rem;border:1px solid #ccc;border-radius:4px;font-size:.88rem}
#edit-popup .ep-row{margin-top:.75rem;display:flex;gap:.5rem}
#edit-popup .ep-save{background:#2E86AB;color:#fff;border:none;border-radius:5px;padding:.35rem .8rem;cursor:pointer;font-size:.85rem}
#edit-popup .ep-save:hover{background:#1a6a8a}
#edit-popup .ep-cancel{background:none;border:1px solid #ccc;border-radius:5px;padding:.35rem .8rem;cursor:pointer;font-size:.85rem}
.shot-cell{padding:.2rem .3rem;vertical-align:middle}
.thumb{display:block;height:72px;width:36px;object-fit:cover;object-position:top center;border-radius:3px;background:#eee}
.no-shot{color:#ddd;font-size:.75rem;padding:.2rem}
.ivs-sm,.sort-sm{display:none}
@media """ + _NARROW_MQ + """{
body{font-size:16px}
header{padding:.6rem .75rem}
.bar{padding:.5rem}
.wrap{padding:.5rem}
table{table-layout:auto}
.""" + _HIDE_SM + """{display:none}
th{padding:.45rem .35rem;min-width:0}
td{font-size:.95rem;padding:.45rem .35rem}
td.name,td.verdict{white-space:normal}
.badge{font-size:.8rem}
.ivs-sm{display:block;font:400 .8rem monospace;color:#999;white-space:nowrap}
.bar input{width:100%;min-width:0;max-width:none;font-size:16px}
.bar select{font-size:16px;min-height:44px}
.sort-sm{display:inline-block}
.col-resize{display:none}
td.actions{white-space:nowrap}
.edit-btn,.del-btn{min-width:44px;min-height:44px;font-size:1.1rem;margin:0}
.edit-btn{margin-right:8px}
#edit-popup{left:0;right:0;bottom:0;top:auto;width:100%;min-width:0;border-radius:12px 12px 0 0;padding:1rem 1rem 1.5rem}
#edit-popup label{min-height:44px;font-size:1rem}
#edit-popup input[type=checkbox]{width:24px;height:24px}
#edit-popup input[type=number],#edit-popup select{min-height:44px;font-size:16px;flex:1}
#edit-popup .ep-save,#edit-popup .ep-cancel{min-height:44px;flex:1;font-size:1rem}
}
</style>
</head>"""

_HTML_SCRIPT = """<script>
const tbody = document.getElementById('tbody');
const countEl = document.getElementById('count');
let sortKey = 'cp', sortDir = -1;
const NARROW_MQ = '""" + _NARROW_MQ + """';
function isNarrow(){ return window.matchMedia(NARROW_MQ).matches; }

function updateCount(){
  let n = 0;
  for(const r of tbody.rows) if(!r.classList.contains('hidden')) n++;
  countEl.textContent = n + ' Pokémon';
}

function applyFilter(){
  const q = document.getElementById('q').value.toLowerCase();
  const vf = document.getElementById('vf').value;
  for(const r of tbody.rows){
    const ok = (!q || r.dataset.search.includes(q)) &&
               (!vf || r.dataset.tag === vf);
    r.classList.toggle('hidden', !ok);
  }
  updateCount();
}

function sortBy(key, dir){
  if(dir !== undefined){ sortKey = key; sortDir = dir; }
  else if(sortKey === key) sortDir = -sortDir;
  else { sortKey = key; sortDir = -1; }
  document.querySelectorAll('th[data-key]').forEach(th => {
    th.classList.remove('asc','desc');
    if(th.dataset.key === key) th.classList.add(sortDir > 0 ? 'asc' : 'desc');
  });
  const ss = document.getElementById('sortsel');
  if(ss) ss.value = key + ':' + sortDir;
  const rows = [...tbody.rows];
  rows.sort((a,b) => {
    let av = a.dataset[key] ?? '', bv = b.dataset[key] ?? '';
    const n = parseFloat(av), m = parseFloat(bv);
    if(!isNaN(n) && !isNaN(m)) return (n - m) * sortDir;
    return av < bv ? -sortDir : av > bv ? sortDir : 0;
  });
  rows.forEach(r => tbody.appendChild(r));
}

document.getElementById('q').addEventListener('input', applyFilter);
document.getElementById('vf').addEventListener('change', applyFilter);
document.getElementById('sortsel').addEventListener('change', e => {
  const [k, d] = e.target.value.split(':');
  sortBy(k, parseInt(d, 10));
});

function delPokemon(e, id, name) {
  if(!confirm('Transfer ' + name + '?')) return;
  fetch('/pokemon/' + id, {method:'DELETE'})
    .then(r=>r.json())
    .then(d=>{ if(d.ok){ e.target.closest('tr').remove(); updateCount(); }});
}

updateCount();

// Measure fixed column widths under auto layout, then lock them.
(function(){
  if(isNarrow()) return;
  const tbl = document.querySelector('table');
  const ths = [...document.querySelectorAll('th')];
  tbl.style.tableLayout = 'auto';
  const locked = ths.map(th => th.hasAttribute('data-noresize') ? th.offsetWidth : null);
  tbl.style.tableLayout = 'fixed';
  ths.forEach((th, i) => { if(locked[i] !== null) th.style.width = locked[i] + 'px'; });
})();

// Column resizing — only for columns without data-noresize.
document.querySelectorAll('th:not([data-noresize])').forEach(th => {
  const handle = document.createElement('div');
  handle.className = 'col-resize';
  th.appendChild(handle);
  let startX, startW;
  handle.addEventListener('mousedown', e => {
    if(isNarrow()) return;
    e.stopPropagation();
    startX = e.pageX;
    startW = th.offsetWidth;
    handle.classList.add('dragging');
    const onMove = e => { th.style.width = Math.max(48, startW + e.pageX - startX) + 'px'; };
    const onUp   = () => { handle.classList.remove('dragging'); document.removeEventListener('mousemove', onMove); document.removeEventListener('mouseup', onUp); };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
});

// Edit popup
let _editEid = null;

function openEdit(e, eid) {
  e.stopPropagation();
  _editEid = eid;
  const row = e.target.closest('tr');
  const ds = row.dataset;
  document.getElementById('ep-name').textContent = ds.name;
  document.getElementById('ep-cp').value = (ds.cp && ds.cp !== '0') ? ds.cp : '';
  document.getElementById('ep-shiny').checked   = ds.shiny   === '1';
  document.getElementById('ep-shadow').checked  = ds.shadow  === '1';
  document.getElementById('ep-costume').checked = ds.costume === '1';
  document.getElementById('ep-dynamax').checked = ds.dynamax === '1';
  const sel = document.getElementById('ep-tag');
  sel.options[0].textContent = ds.autotag ? 'Auto (' + ds.autotag + ')' : 'Auto';
  sel.value = ds.usertag || '';
  if (sel.selectedIndex < 0) sel.value = '';
  const popup = document.getElementById('edit-popup');
  popup.style.display = 'block';
  if (isNarrow()) {
    popup.style.left = '';
    popup.style.top  = '';
    return;
  }
  const px = Math.min(e.clientX, window.innerWidth  - 220);
  const py = Math.min(e.clientY, window.innerHeight - 180);
  popup.style.left = px + 'px';
  popup.style.top  = py + 'px';
}

function closeEdit() {
  document.getElementById('edit-popup').style.display = 'none';
  _editEid = null;
}

function saveEdit() {
  if (!_editEid) return;
  const payload = {
    cp:         parseInt(document.getElementById('ep-cp').value) || undefined,
    is_shiny:   document.getElementById('ep-shiny').checked   ? 1 : 0,
    is_shadow:  document.getElementById('ep-shadow').checked  ? 1 : 0,
    is_costume: document.getElementById('ep-costume').checked ? 1 : 0,
    is_dynamax: document.getElementById('ep-dynamax').checked ? 1 : 0,
    user_tag:   document.getElementById('ep-tag').value || null,
  };
  fetch('/pokemon/' + _editEid, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  }).then(r => r.json()).then(d => {
    if (d.ok) location.reload();
    else alert('Save failed: ' + (d.error || 'unknown'));
    closeEdit();
  }).catch(() => { alert('Save failed'); closeEdit(); });
}

document.addEventListener('click', e => {
  const popup = document.getElementById('edit-popup');
  if (popup && popup.style.display !== 'none' && !popup.contains(e.target)) closeEdit();
});
</script>"""


def _stars_html(total: int | None) -> str:
    if total is None:
        return '<span class="dim">?</span>'
    if total >= 37:
        return '<span class="stars">★★★</span>'
    if total >= 30:
        return '<span class="stars">★★</span><span class="dim">★</span>'
    if total >= 23:
        return '<span class="stars">★</span><span class="dim">★★</span>'
    return '<span class="dim">★★★</span>'


def _badge(verdict: str | None, tag: str | None = None) -> str:
    if not verdict:
        return ""
    label = tag or verdict.upper()
    color = _TAG_COLOR.get(label.lower(), _VERDICT_COLOR.get(verdict.lower(), _DEFAULT_BADGE_COLOR))
    return f'<span class="badge" style="background:{color}">{label}</span>'


def _eff_tag(row) -> str:
    """Effective tag for a collection row: the manual override, else the computed suggestion."""
    return row["user_tag"] or row["verdict_tag"] or ""


def _row_badge(verdict: str | None, user_tag: str | None, auto_tag: str | None) -> str:
    """Verdict badge; a manual override is marked with ✎ and names the rules' suggestion."""
    if not user_tag:
        return _badge(verdict, auto_tag)
    color = _TAG_COLOR.get(user_tag.lower(), _DEFAULT_BADGE_COLOR)
    title = html.escape(f"Manual tag (rules suggest: {auto_tag or verdict or 'none'})")
    return (f'<span class="badge" style="background:{color}" title="{title}">'
            f'{html.escape(user_tag)} ✎</span>')


def _relative_time(iso: str) -> str:
    try:
        from datetime import timezone
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        diff = (datetime.now(timezone.utc) - dt).total_seconds()
        if diff < 60:
            return "just now"
        if diff < 3600:
            return f"{int(diff / 60)}m ago"
        if diff < 86400:
            return f"{int(diff / 3600)}h ago"
        return f"{int(diff / 86400)}d ago"
    except Exception:
        return iso[:10]


def _purpose_from_reasons(verdict_reason: str | None) -> str:
    """Extract a short purpose label from the stored reasons string."""
    if not verdict_reason:
        return ""
    r = verdict_reason
    if "G-Max" in r:
        return "G-Max"
    if "shiny" in r.lower():
        return "Shiny"
    if "Shadow" in r:
        return "Shadow"
    # Check PvP leagues before Raid so multi-purpose entries get the right label.
    if "Great League" in r and "Ultra League" in r:
        return "GL / UL"
    if "Great League" in r:
        return "Great League"
    if "Ultra League" in r:
        return "Ultra League"
    if "Master League" in r and "Raid" not in r:
        return "Master League"
    if "Raid:" in r:
        # Extract type(s): "Raid: Dragon · Raid: Ground" → "Dragon / Ground"
        types = [p.replace("Raid:", "").strip() for p in r.split("·") if "Raid:" in p]
        # de-duplicate while preserving order
        seen: set[str] = set()
        unique = [t for t in types if not (t in seen or seen.add(t))]  # type: ignore[func-returns-value]
        return "Raid: " + " / ".join(unique[:3]) if unique else "Raid"
    if "Master League" in r:
        return "Master League"
    if "hundo" in r.lower():
        return "Hundo"
    return ""


def _render_collection(coll: "CollectionDB", gd: "GameData | None" = None) -> str:
    from datetime import datetime
    rows = coll.fetch_collection()

    ncols = 13  # Shot Species CP HP IVs IV% Stars Fast Charged Purpose Verdict Seen ×

    if not rows:
        body = f'<tr><td colspan="{ncols}" class="empty">No Pokémon yet — appraise something!</td></tr>'
    else:
        parts = []
        for r in rows:
            ivs = r["attack_iv"], r["defense_iv"], r["stamina_iv"]
            total = r["iv_total"]
            pct = f"{total / 45 * 100:.0f}" if total is not None else ""
            iv_text = (f"{ivs[0]}/{ivs[1]}/{ivs[2]}"
                       if all(v is not None for v in ivs) else "—")
            name = r["display_name"] or ""
            verdict = r["verdict"] or ""
            eff_tag = _eff_tag(r)
            flags = ("✨ " if r["is_shiny"] else "") + ("🌑 " if r["is_shadow"] else "")

            # Best moves — use PvP context when the purpose is a PvP league.
            purpose = _purpose_from_reasons(r["verdict_reason"])
            moves: dict = {}
            if gd:
                try:
                    moves = gd.best_moves(r["species_template_id"])
                except Exception:
                    pass
            use_pvp = eff_tag in ("Great", "Ultra") or any(kw in purpose for kw in ("League", "GL", "UL", "ML"))
            mv = moves.get("pvp" if use_pvp else "raid", {})
            fast_text    = mv.get("fast") or "—"
            charged_list = mv.get("charged") or []
            charged_text = " / ".join(charged_list) if charged_list else "—"

            tag_lower = html.escape(eff_tag.lower(), quote=True)
            search_str = html.escape(
                f"{name.lower()} {verdict.lower()} {eff_tag.lower()} {purpose.lower()}", quote=True)
            stars_val = (0 if total is None else
                         3 if total >= 37 else 2 if total >= 30 else 1 if total >= 23 else 0)
            eid = r["entity_id"]
            hp_val = r["hp"] if r["hp"] else "—"
            cp_raw = r["cp"] or 0
            cp_display = str(cp_raw) if cp_raw else '<span class="dim">?</span>'
            safe_name = name.replace("'", "\\'")
            obs_id = r["observation_id"]
            if r["has_screenshot"]:
                shot_cell = (f'<td class="shot-cell {_HIDE_SM}">'
                             f'<a href="/screenshot/{obs_id}" target="_blank">'
                             f'<img src="/screenshot/{obs_id}" class="thumb" loading="lazy">'
                             f'</a></td>')
            else:
                shot_cell = f'<td class="shot-cell no-shot {_HIDE_SM}">—</td>'
            parts.append(
                f'<tr data-name="{name}" data-cp="{cp_raw}" data-pct="{pct}"'
                f' data-stars="{stars_val}" data-verdict="{verdict.lower()}" data-tag="{tag_lower}"'
                f' data-shiny="{1 if r["is_shiny"] else 0}"'
                f' data-shadow="{1 if r["is_shadow"] else 0}"'
                f' data-costume="{1 if r["is_costume"] else 0}"'
                f' data-dynamax="{1 if r["is_dynamax"] else 0}"'
                f' data-eid="{eid}"'
                f' data-usertag="{html.escape(r["user_tag"] or "")}"'
                f' data-autotag="{html.escape(r["verdict_tag"] or "")}"'
                f' data-purpose="{html.escape(purpose.lower())}"'
                f' data-search="{search_str}">'
                + shot_cell
                + f'<td class="name">{flags}{name}</td>'
                f'<td class="nw">{cp_display}</td>'
                f'<td class="dim nw {_HIDE_SM}">{hp_val}</td>'
                f'<td class="ivs {_HIDE_SM}">{iv_text}</td>'
                f'<td class="pct">{pct + "%" if pct else "—"}'
                f'<span class="ivs-sm">{iv_text}</span></td>'
                f'<td class="nw {_HIDE_SM}">{_stars_html(total)}</td>'
                f'<td class="mv {_HIDE_SM}">{fast_text}</td>'
                f'<td class="mv {_HIDE_SM}">{charged_text}</td>'
                f'<td class="purpose {_HIDE_SM}">{purpose}</td>'
                f'<td class="nw verdict">{_row_badge(verdict, r["user_tag"], r["verdict_tag"])}</td>'
                f'<td class="dim {_HIDE_SM}">{_relative_time(r["captured_at"])}</td>'
                f'<td class="actions"><button class="edit-btn" onclick="openEdit(event,{eid})">&#9998;</button>'
                f'<button class="del-btn" onclick="delPokemon(event,{eid},\'{safe_name}\')">&times;</button></td>'
                f'</tr>\n'
            )
        body = "".join(parts)

    count = len(rows)
    tags = sorted({_eff_tag(r).lower() for r in rows} - {""})
    tag_opts = '<option value="">All tags</option>' + "".join(
        f'<option value="{html.escape(t)}">{html.escape(t.title())}</option>' for t in tags
    )
    sort_opts = "".join(
        f'<option value="{k}:{d}"{" selected" if (k, d) == ("cp", -1) else ""}>{label} {arrow}</option>'
        for k, label in _SORT_KEYS for d, arrow in ((1, "↑"), (-1, "↓"))
    )
    ep_tag_opts = '<option value="">Auto</option>' + "".join(
        f'<option value="{t}">{t}</option>' for t in USER_TAGS
    )

    return (
        _HTML_HEAD
        + f"""
<body>
<header>
  <h1>PokeVerdict</h1>
  <span class="sub" id="count">{count} Pokémon</span>
</header>
<div class="bar">
  <input id="q" type="search" placeholder="Search species or verdict…" autofocus>
  <select id="vf">{tag_opts}</select>
  <select id="sortsel" class="sort-sm" aria-label="Sort by">{sort_opts}</select>
</div>
<div class="wrap">
<table>
<thead><tr>
  <th data-noresize class="{_HIDE_SM}"></th>
  <th data-noresize data-key="name" onclick="sortBy('name')">Species</th>
  <th data-noresize data-key="cp" onclick="sortBy('cp')" class="desc">CP</th>
  <th data-noresize class="{_HIDE_SM}">HP</th>
  <th data-noresize class="{_HIDE_SM}" style="min-width:5.5rem">IVs</th>
  <th data-noresize data-key="pct" onclick="sortBy('pct')">IV%</th>
  <th data-noresize class="{_HIDE_SM}" data-key="stars" onclick="sortBy('stars')">Stars</th>
  <th class="{_HIDE_SM}">Fast</th>
  <th class="{_HIDE_SM}">Charged</th>
  <th class="{_HIDE_SM}" data-key="purpose" onclick="sortBy('purpose')">Purpose</th>
  <th data-noresize>Verdict</th>
  <th class="{_HIDE_SM}">Seen</th>
  <th data-noresize></th>
</tr></thead>
<tbody id="tbody">
{body}
</tbody>
</table>
</div>
<div id="edit-popup">
  <b id="ep-name"></b>
  <label>CP <input id="ep-cp" type="number" min="10" max="10000"></label>
  <label><input id="ep-shiny" type="checkbox"> Shiny ✨</label>
  <label><input id="ep-shadow" type="checkbox"> Shadow 🌑</label>
  <label><input id="ep-costume" type="checkbox"> Costume 🎩</label>
  <label><input id="ep-dynamax" type="checkbox"> Dynamax ☁</label>
  <label for="ep-tag">Tag <select id="ep-tag">{ep_tag_opts}</select></label>
  <div class="ep-row">
    <button class="ep-save" onclick="saveEdit()">Save</button>
    <button class="ep-cancel" onclick="closeEdit()">Cancel</button>
  </div>
</div>
"""
        + _HTML_SCRIPT
        + "\n</body></html>"
    )


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    gamedata: GameData
    rules_fn = None
    ntfy_url: str | None = None
    collection: "CollectionDB | None" = None
    lock = threading.Lock()

    def log_message(self, fmt, *args):
        sys.stderr.write("  [%s] %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, payload, content_type="application/json"):
        body = (json.dumps(payload, indent=2) if content_type == "application/json"
                else str(payload)).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            if self.collection is None:
                self._send(200, "No collection DB — restart with --collection PATH", "text/plain")
            else:
                self._send(200, _render_collection(self.collection, self.gamedata), "text/html")
        elif self.path.startswith("/screenshot/"):
            if self.collection is None:
                self._send(503, {"ok": False, "error": "no collection"})
                return
            try:
                obs_id = int(self.path[len("/screenshot/"):].split("?")[0])
            except ValueError:
                self._send(404, {"ok": False, "error": "not found"})
                return
            shot = self.collection.get_screenshot(obs_id)
            if shot is None:
                self._send(404, {"ok": False, "error": "no screenshot"})
                return
            shot_data, mime = shot
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(shot_data)))
            self.send_header("Cache-Control", "max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(shot_data)
        elif self.path.startswith("/health"):
            self._send(200, {"ok": True, "species": len(self.gamedata.cpm),
                             "ocr": pytesseract is not None,
                             "collection": self.collection is not None})
        else:
            self._send(404, {"ok": False, "error": "POST an image to /appraise"})

    def do_DELETE(self):
        # DELETE /pokemon/<entity_id>  →  mark entity as disposed
        parts = self.path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "pokemon":
            try:
                eid = int(parts[1])
            except ValueError:
                self._send(400, {"ok": False, "error": "invalid id"})
                return
            if self.collection is None:
                self._send(503, {"ok": False, "error": "no collection"})
                return
            ok = self.collection.dispose(eid)
            self._send(200, {"ok": ok})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/appraise"):
            self._send(404, {"ok": False, "error": "unknown endpoint"})
            return

        length = int(self.headers.get("Content-Length", 0))
        if not length:
            self._send(400, {"ok": False, "error": "empty body"})
            return

        data = self.rfile.read(length)
        try:
            with self.lock:
                result = appraise(self.gamedata, self.rules_fn, data, self.collection)
        except Exception as exc:
            traceback.print_exc()
            self._send(500, {"ok": False, "error": str(exc)})
            return

        mime = "image/png" if data[:4] == b'\x89PNG' else "image/jpeg"
        if result.get("ok") and self.collection is not None:
            try:
                _eid, obs_id = self.collection.record(result)
                if obs_id is not None:
                    self.collection.save_screenshot(obs_id, data, mime)
                refresh_stale_verdicts(self.gamedata, self.rules_fn, self.collection)
            except Exception as exc:
                sys.stderr.write(f"  collection write failed: {exc}\n")
        elif result.get("failureKind") and self.collection is not None:
            try:
                self.collection.record_failure(
                    raw_text=result.get("ocr", {}).get("name", ""),
                    reason=result.get("error", "unknown"),
                    screenshot=data,
                    mime_type=mime,
                )
                sys.stderr.write(f"  capture_failure saved: {result.get('error')}\n")
            except Exception as exc:
                sys.stderr.write(f"  capture_failure write failed: {exc}\n")

        if self.ntfy_url and result.get("ok"):
            result["push"] = push_ntfy(self.ntfy_url, result)

        # Shortcuts is happier with plain text; ask for it with ?format=text
        if "format=text" in self.path:
            self._send(200, format_line(result), "text/plain")
        else:
            self._send(200, result)

    def do_PATCH(self):
        parts = self.path.strip("/").split("/")
        if not (len(parts) == 2 and parts[0] == "pokemon"):
            self._send(404, {"ok": False, "error": "not found"})
            return
        try:
            eid = int(parts[1])
        except ValueError:
            self._send(400, {"ok": False, "error": "invalid id"})
            return
        if self.collection is None:
            self._send(503, {"ok": False, "error": "no collection"})
            return
        updates = self._read_patch_body()
        if updates is None:
            return
        has_tag = "user_tag" in updates
        tag = None
        if has_tag:
            ok, tag = _parse_user_tag(updates.pop("user_tag"))
            if not ok:
                self._send(400, {"ok": False, "error": "invalid user_tag"})
                return
        extra = {"user_tag": tag} if has_tag else {}

        with self.lock:
            if has_tag and not self.collection.set_user_tag(eid, tag):
                self._send(404, {"ok": False, "error": "entity not found"})
                return
            if has_tag and not updates:
                self._send(200, {"ok": True, **extra})
                return
            obs_id = self.collection.update_observation(eid, updates)
            if obs_id is None and has_tag:
                # Tag saved; remaining keys were not editable fields.
                self._send(200, {"ok": True, **extra})
                return
            if obs_id is None:
                self._send(404, {"ok": False, "error": "entity not found"})
                return
            fields = self._reevaluate(obs_id)
        self._send(200, {"ok": True, **(fields or {}), **extra})

    def _read_patch_body(self) -> dict | None:
        """Read and parse the PATCH JSON object body; sends a 400 and returns None on error."""
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            self._send(400, {"ok": False, "error": "empty body"})
            return None
        try:
            updates = json.loads(self.rfile.read(length))
        except Exception:
            self._send(400, {"ok": False, "error": "bad JSON"})
            return None
        if not isinstance(updates, dict):
            self._send(400, {"ok": False, "error": "body must be a JSON object"})
            return None
        return updates

    def _reevaluate(self, obs_id: int) -> dict | None:
        """
        Re-run the rules on an edited observation and persist the new verdict.

        Caller holds self.lock. Returns the response fields, or None when the
        species cannot be resolved (verdict left as is).
        """
        row = self.collection.get_observation(obs_id)
        try:
            species = self.gamedata.species(row["species_template_id"])
        except KeyError:
            try:
                species = self.gamedata.best_species_match(row["display_name"])
            except KeyError:
                return None
        r = self.rules_fn(species, self._eval_input(row, species))
        new_verdict = r["verdict"].lower()
        new_tag     = r.get("suggestedTag") or ""
        new_reasons = "; ".join(r["reasons"])
        self.collection.update_verdict(obs_id, new_verdict, new_reasons, new_tag)
        return {
            "verdict": new_verdict,
            "suggestedTag": new_tag,
            "cp": row["cp"],
            "is_shiny": bool(row["is_shiny"]),
            "is_shadow": bool(row["is_shadow"]),
        }

    def _eval_input(self, row, species) -> dict:
        """Build the rules-engine input dict for a stored observation row."""
        a, d, s = row["attack_iv"], row["defense_iv"], row["stamina_iv"]
        total = row["iv_total"]
        pvp: dict = {}
        for league, cap in (("greatLeague", 1500), ("ultraLeague", 2500)):
            rank, total_spreads = self.gamedata.pvp_rank(species.template_id, a, d, s, cap)
            pvp[league] = {"rank": rank, "total": total_spreads}
        fam_id = self.gamedata.family_id_for(species.template_id)
        return {
            "total": total, "percent": round(total / 45 * 100, 1), "ivs": [a, d, s],
            "isShiny":    bool(row["is_shiny"]) or None,
            "isShadow":   bool(row["is_shadow"]) or None,
            "isPurified": bool(row["is_purified"]) or None,
            "isLucky":    bool(row["is_lucky"]) or None,
            "isCostume":  bool(row["is_costume"]) or None,
            "isDynamax":  bool(row["is_dynamax"]) or None,
            "pvp": pvp,
            "boxContext": self.collection.box_context(species.template_id),
            "familyContext": self.collection.family_context(
                self.gamedata.family_members(fam_id) if fam_id else []
            ),
        }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--gamedata", default=None)
    ap.add_argument("--rules", default=None)
    ap.add_argument("--ntfy", help="e.g. https://ntfy.sh/your-private-topic")
    ap.add_argument("--collection", "--db", default=None, metavar="PATH",
                    dest="collection",
                    help="SQLite collection file (created on first appraisal; --db is an alias)")
    ap.add_argument("--selftest", nargs="*", help="run against images and exit")
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    gd = GameData(args.gamedata or os.path.join(root, "gamedata.sqlite"),
                  check_same_thread=False)
    rules_fn = make_rules_fn(load_rules(args.rules), gd)

    if args.selftest is not None:
        for path in args.selftest:
            with open(path, "rb") as fh:
                r = appraise(gd, rules_fn, fh.read())
            print(f"\n{os.path.basename(path)}")
            print("   " + format_line(r))
            if r.get("ok"):
                for reason in r.get("reasons", [r.get("reason", "")]):
                    print(f"   reason      {reason}")
                print(f"   cp source   {r.get('cpSource', 'not determined')}")
                print(f"   ocr         name={r['ocr']['name']!r} "
                      f"hp={r['ocr']['hp']!r} cp={r['ocr'].get('cp')}")
                print(f"   confidence  {r['confidence']:.3f}")
        return 0

    Handler.gamedata = gd
    # staticmethod, or Python binds it as an instance method and passes self.
    Handler.rules_fn = staticmethod(rules_fn)
    Handler.ntfy_url = args.ntfy

    if args.collection:
        Handler.collection = CollectionDB(args.collection)
        n_refreshed = refresh_stale_verdicts(gd, rules_fn, Handler.collection)
        print(f"collection    {args.collection}  "
              f"({Handler.collection.count()} Pokémon"
              + (f", {n_refreshed} verdicts refreshed" if n_refreshed else "")
              + ")",
              file=sys.stderr)

    if not pytesseract:
        print("WARNING: tesseract-ocr not found — species name OCR disabled.", file=sys.stderr)
        print("  Install it with:  sudo dnf install tesseract   (Fedora/RHEL)", file=sys.stderr)
        print("  or:               sudo apt install tesseract-ocr   (Debian/Ubuntu)", file=sys.stderr)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"listening on  http://{args.host}:{args.port}/appraise  "
          f"(ocr={'yes' if pytesseract else 'NO — install tesseract-ocr'})", file=sys.stderr)
    if args.collection:
        print(f"collection UI http://{args.host}:{args.port}/", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
