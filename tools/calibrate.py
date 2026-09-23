#!/usr/bin/env python3
"""
Calibration harness for the appraisal screen.

Reads screenshots, measures the three stat bars by pixel width, and
cross-checks the result against the IV solver. If measured bars produce zero
solutions for the observed CP and HP, the read was wrong — that is the single
most useful signal available and it costs nothing.

    python3 tools/calibrate.py shots/*.PNG
    python3 tools/calibrate.py --fixtures fixtures.json shots/*.PNG

Coordinates below were measured on a 1320x2868 iPhone screenshot. They are
normalised, so they should transfer across devices, but re-run this on your own
captures before trusting them.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict

try:
    from PIL import Image
except ImportError:
    sys.exit("needs Pillow:  pip install pillow")

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from solver_ref import GameData, solve, describe  # noqa: E402


# ---------------------------------------------------------------------------
# Geometry, normalised with a top-left origin.
# ---------------------------------------------------------------------------

@dataclass
class Geometry:
    # Stat bar horizontal range
    bar_start_x: float = 0.1182
    bar_end_x: float = 0.4644

    # Bar positions as offsets below the card top.
    # Derived from reference layout (card top 0.7015): absolute − 0.7015.
    bar_attack_offset: float = 0.0400
    bar_defense_offset: float = 0.0821
    bar_stamina_offset: float = 0.1240

    # Alternative bar offsets for the info-screen layout (card_top ≈ 0.808)
    # where the trainer stands on the right and the card is compressed.
    # Calibrated from pixel scan on 1320×2868 Shadow Mewtwo screenshot
    # (obs_id=468): bands at y=2403, 2468, 2525 → offsets 0.033, 0.058, 0.076.
    # Banner ("caught on") at offset ≈ 0.101 — keep stamina well clear.
    bar_attack_offset_alt: float = 0.0340
    bar_defense_offset_alt: float = 0.0580
    bar_stamina_offset_alt: float = 0.0760
    card_top_alt_threshold: float = 0.75  # use alt offsets when card_top > this

    # Known card-top positions to probe.  detect_card_top() tries each and
    # returns the first that gives consistent bar readings.  Add new values
    # here when Niantic changes the card height in a game update.
    # 0.740 covers the info-screen appraise layout where the trainer stands
    # on the right and the white card only occupies the left ~53% of width
    # (making the edge-scan 85%-white threshold unreachable despite a valid card).
    card_top_candidates: tuple = (0.7015, 0.7312, 0.710, 0.740, 0.808)

    # Text regions (screen-absolute, above the card)
    roi_cp:   tuple = (0.28, 0.050, 0.72, 0.092)
    roi_name: tuple = (0.25, 0.405, 0.75, 0.442)
    roi_hp:   tuple = (0.32, 0.458, 0.68, 0.482)
    roi_size: tuple = (0.08, 0.526, 0.92, 0.568)

    # Bar fill detection
    min_saturation: float = 0.25
    min_brightness: int = 170

    # Dynamax badge: vivid pink "×" icon left of the appraisal badge stars.
    # Calibrated on a 589×1280 info screen; badge sits above card_top_candidates.
    roi_dynamax: tuple = (0.30, 0.59, 0.44, 0.68)

    # Shadow glow: vivid blue-purple around the sprite.
    # Covers the sprite + surrounding area, above the appraisal card.
    roi_shadow: tuple = (0.10, 0.10, 0.90, 0.55)


GEO = Geometry()


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

def _saturation(r: int, g: int, b: int) -> float:
    mx, mn = max(r, g, b), min(r, g, b)
    return 0.0 if mx == 0 else (mx - mn) / mx


def _is_dynamax_pink(r: int, g: int, b: int) -> bool:
    mx = max(r, g, b)
    if mx < 80:
        return False
    sat = (mx - min(r, g, b)) / mx
    return r > g and r > b and sat >= 0.55 and (r - min(g, b)) >= 70


def detect_dynamax(im: "Image.Image", geo: Geometry = GEO) -> bool:
    """Return True when the Dynamax/Gigantamax badge (vivid pink) is visible."""
    W, H = im.size
    px = im.load()
    x0, y0, x1, y1 = geo.roi_dynamax
    ix0, iy0, ix1, iy1 = int(x0 * W), int(y0 * H), int(x1 * W), int(y1 * H)
    vivid = 0
    for iy in range(iy0, iy1, 3):
        for ix in range(ix0, ix1, 3):
            r, g, b = px[ix, iy][:3]
            if _is_dynamax_pink(r, g, b):
                vivid += 1
                if vivid >= 5:
                    return True
    return False


def _is_shadow_purple(r: int, g: int, b: int) -> bool:
    """
    True for the vivid blue-purple glow that surrounds Shadow Pokémon sprites.

    Calibrated against a 589×1280 Shadow Seel screenshot (11 445 matching pixels
    vs 0 on three non-shadow reference shots).  The glow sits between navy and
    magenta: blue dominant, red clearly above green, with high saturation.

    Exact thresholds derived from pixel sampling:
      sat ≥ 0.60   — vivid, not grey or pastel
      b > r        — blue-dominant (violet direction, not warm)
      r > g        — red elevated above green (purple, not cyan or navy)
      b − g ≥ 60   — strong blue-green gap (not teal)
      b − r ≤ 110  — not pure blue — the red component lifts it to purple/violet
    """
    mx = max(r, g, b)
    if mx < 60:
        return False
    sat = (mx - min(r, g, b)) / mx
    return sat >= 0.60 and b > r and r > g and b - g >= 60 and b - r <= 110


def detect_shadow(im: "Image.Image", geo: Geometry = GEO) -> bool:
    """Return True when the vivid purple shadow glow is present around the sprite."""
    W, H = im.size
    px = im.load()
    x0, y0, x1, y1 = geo.roi_shadow
    ix0, iy0, ix1, iy1 = int(x0 * W), int(y0 * H), int(x1 * W), int(y1 * H)
    vivid = 0
    for iy in range(iy0, iy1, 3):
        for ix in range(ix0, ix1, 3):
            r, g, b = px[ix, iy][:3]
            if _is_shadow_purple(r, g, b):
                vivid += 1
                if vivid >= 100:
                    return True
    return False


def detect_card_top(px, W: int, H: int, geo: Geometry = GEO) -> float | None:
    """
    Return the card-top Y by detecting the white top edge of the appraisal card,
    then validating that bar readings are consistent.

    Primary: scans horizontally for a mostly-white row in the expected card range
    (y = 0.64–0.78).  Any candidate that passes validation is returned immediately.

    Fallback: if edge detection finds nothing, tries geo.card_top_candidates —
    useful when a UI reskin removes the white band.
    """
    bar_offsets = (geo.bar_attack_offset, geo.bar_defense_offset, geo.bar_stamina_offset)

    def _validates(ct: float) -> bool:
        results = [measure_bar(px, W, H, ct + off, geo) for off in bar_offsets]
        valid   = sum(1 for iv, _, conf in results if iv is not None and conf > 0.3)
        nonzero = sum(1 for iv, _, _    in results if iv is not None and iv > 0)
        return valid >= 2 and nonzero >= 1

    # Edge scan: find first mostly-white horizontal band.
    x0, x1 = int(0.10 * W), int(0.90 * W)
    step = max(1, (x1 - x0) // 40)
    n_samples = len(range(x0, x1, step))
    for iy in range(int(0.64 * H), int(0.84 * H)):
        white = sum(1 for ix in range(x0, x1, step)
                    if all(c > 235 for c in px[ix, iy][:3]))
        if white / n_samples >= 0.85:
            ct = iy / H
            if _validates(ct):
                return ct

    # Fallback: hardcoded candidates for layouts without a clean white band.
    for ct in geo.card_top_candidates:
        if _validates(ct):
            return ct

    return None


def card_is_open(px, W: int, H: int, geo: Geometry = GEO) -> tuple[bool, float | None]:
    """
    Gate check for the appraisal card.

    Returns (is_open, card_top_y).  card_top_y is the normalised Y of the card
    top — use it to compute bar rows as card_top_y + geo.bar_*_offset.
    """
    ct = detect_card_top(px, W, H, geo)
    return (ct is not None, ct)


def measure_bar(px, W: int, H: int, row_y: float, geo: Geometry = GEO):
    """
    Return (iv, raw, confidence). The bar is drawn as three segments with small
    gaps, but fill maps linearly across the whole track, so the rightmost
    saturated pixel is all we need.

    Confidence is 1 minus the distance to the nearest integer, doubled and
    clamped: a fill caught mid-animation lands between steps and scores low.

    A ±2-pixel vertical scan around the nominal row handles sub-pixel alignment
    at high DPI (e.g. 1320×2868) where the calibrated fraction can land on a
    border pixel rather than the bar itself.
    """
    y_nom = int(row_y * H)
    x0, x1 = int(geo.bar_start_x * W), int(geo.bar_end_x * W)

    best = (None, None, 0.0)       # (iv, raw, conf) of the best row so far
    best_filled = -1

    for dy in range(-2, 3):
        y = y_nom + dy
        if y < 0 or y >= H:
            continue

        last_filled = None
        filled_count = 0
        for x in range(x0, x1 + 1):
            r, g, b = px[x, y][:3]
            if _saturation(r, g, b) > geo.min_saturation and max(r, g, b) > geo.min_brightness:
                last_filled = x
                filled_count += 1

        if last_filled is None:
            if best_filled < 0:
                best = (0, 0.0, 1.0)   # empty bar — keep as candidate
            continue

        span = last_filled - x0
        contiguity = filled_count / max(1, span + 1)
        if contiguity < 0.85:
            continue   # broken run (gap in fill) — skip this row

        raw = span / (x1 - x0) * 15
        iv = int(round(raw))
        conf = max(0.0, 1.0 - abs(raw - iv) * 2)
        candidate = (min(15, max(0, iv)), round(raw, 2), round(conf, 3))

        # Prefer the row with the most filled pixels (most solid bar core).
        if filled_count > best_filled:
            best_filled = filled_count
            best = candidate

    # If no row passed the contiguity check, signal a hard failure.
    if best[0] is None and best_filled <= 0:
        return None, None, 0.0
    return best


def star_tier(total: int) -> int:
    """In-game badge: 0 to 3 filled stars."""
    if total >= 37:
        return 3
    if total >= 30:
        return 2
    if total >= 23:
        return 1
    return 0


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def analyse(path: str, gd: GameData | None, species: str | None,
            cp: int | None, hp: int | None, geo: Geometry = GEO) -> dict:
    im = Image.open(path).convert("RGB")
    W, H = im.size
    px = im.load()

    result = {"file": path.rsplit("/", 1)[-1], "size": [W, H]}

    open_ok, card_top = card_is_open(px, W, H, geo)
    if not open_ok:
        result["error"] = "appraisal card not detected"
        return result

    result["card_top"] = round(card_top, 4)

    if card_top > geo.card_top_alt_threshold:
        bar_offsets = (geo.bar_attack_offset_alt, geo.bar_defense_offset_alt, geo.bar_stamina_offset_alt)
    else:
        bar_offsets = (geo.bar_attack_offset, geo.bar_defense_offset, geo.bar_stamina_offset)

    bars = {}
    for stat, offset in zip(("attack", "defense", "stamina"), bar_offsets):
        iv, raw, conf = measure_bar(px, W, H, card_top + offset, geo)
        bars[stat] = {"iv": iv, "raw": raw, "confidence": conf}

    result["bars"] = bars

    if any(b["iv"] is None for b in bars.values()):
        result["error"] = "a bar did not read cleanly"
        return result

    ivs = (bars["attack"]["iv"], bars["defense"]["iv"], bars["stamina"]["iv"])
    total = sum(ivs)
    result.update({
        "ivs": list(ivs),
        "total": total,
        "percent": round(total / 45 * 100, 1),
        "stars": star_tier(total),
        "min_confidence": min(b["confidence"] for b in bars.values()),
    })

    # The real validation: do these bars admit a solution for the observed
    # CP and HP? Zero solutions means the measurement is wrong.
    if gd and species and cp and hp:
        matches = solve(gd, species, cp, hp,
                        attack_bar=ivs[0], defense_bar=ivs[1], stamina_bar=ivs[2])
        result["solver"] = {
            "species": species, "cp": cp, "hp": hp,
            "candidates": len(matches),
            "level": matches[0].level if len(matches) == 1 else None,
            "verdict": ("consistent" if len(matches) == 1
                        else "AMBIGUOUS" if matches else "INCONSISTENT"),
        }
        result["blind_candidates"] = len(solve(gd, species, cp, hp))

    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="*")
    ap.add_argument("--gamedata", default="gamedata.sqlite")
    ap.add_argument("--species", help="cross-check against this species")
    ap.add_argument("--cp", type=int)
    ap.add_argument("--hp", type=int, help="MAX hp, the number after the slash")
    ap.add_argument("--fixtures", help="write results as JSON test fixtures")
    args = ap.parse_args()

    try:
        gd = GameData(args.gamedata)
    except Exception:
        gd = None

    results = []
    for path in args.images:
        r = analyse(path, gd, args.species, args.cp, args.hp)
        results.append(r)

        print(f"\n{r['file']}  {r['size'][0]}x{r['size'][1]}")
        if "error" in r:
            print(f"   ! {r['error']}")
            continue
        b = r["bars"]
        print(f"   bars   {b['attack']['iv']}/{b['defense']['iv']}/{b['stamina']['iv']}"
              f"   raw {b['attack']['raw']}/{b['defense']['raw']}/{b['stamina']['raw']}")
        print(f"   total  {r['total']}/45 = {r['percent']}%  ->  {r['stars']} star(s)")
        print(f"   conf   {r['min_confidence']:.3f}")
        if "solver" in r:
            s = r["solver"]
            level = f", level {s['level']:g}" if s["level"] else ""
            print(f"   solver {s['verdict']} ({s['candidates']} candidate(s){level});"
                  f" blind would give {r['blind_candidates']}")

    if args.fixtures:
        with open(args.fixtures, "w") as fh:
            json.dump({"geometry": asdict(GEO), "results": results}, fh, indent=2)
        print(f"\nwrote {args.fixtures}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
