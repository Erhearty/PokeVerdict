#!/usr/bin/env python3
"""
Unit tests for the collection web UI helpers in shortcut_server.py.

    python3 tools/test_web.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from collection_db import CollectionDB  # noqa: E402
from shortcut_server import (  # noqa: E402
    USER_TAGS, _HIDE_SM, _NARROW_MQ, _parse_user_tag, _render_collection,
)

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {label}{' — ' + detail if detail else ''}")
    if not ok:
        FAILURES.append(label)


def test_parse_user_tag() -> None:
    """Valid tags normalise to canonical case; auto/empty clear; junk is rejected."""
    cases = [
        ("Great", (True, "Great")),
        ("great", (True, "Great")),
        ("Desired Evo", (True, "Desired Evo")),
        ("desired evo", (True, "Desired Evo")),
        ("", (True, None)),
        (None, (True, None)),
        ("auto", (True, None)),
        ("AUTO", (True, None)),
        ("bogus", (False, None)),
        (42, (False, None)),
    ]
    for value, expected in cases:
        got = _parse_user_tag(value)
        check(f"_parse_user_tag({value!r})", got == expected, f"got {got}")


def _render_one(user_tag: str | None) -> str:
    """Render the collection page for one Gible whose computed tag is Keep."""
    with tempfile.TemporaryDirectory() as tmp:
        db = CollectionDB(os.path.join(tmp, "coll.sqlite"))
        eid, _ = db.record({
            "species": "Gible", "speciesTemplateID": "GIBLE",
            "ivs": [15, 14, 13], "total": 42, "cp": 900, "maxHP": 100,
            "verdict": "KEEP", "reasons": ["test"], "suggestedTag": "Keep",
        })
        db.set_user_tag(eid, user_tag)
        return _render_collection(db)


def test_render_user_tag() -> None:
    """A manual tag drives data-tag and the badge, keeps the computed tag, and the popup offers every tag."""
    page = _render_one("Great")
    check("render: data-tag is the manual tag", 'data-tag="great"' in page)
    check("render: data-usertag present", 'data-usertag="Great"' in page)
    check("render: data-autotag keeps computed tag", 'data-autotag="Keep"' in page)
    check("render: manual marker on badge", "Great ✎" in page)
    check("render: tooltip names the suggestion", "Manual tag (rules suggest: Keep)" in page)
    check("render: filter offers the effective tag", '<option value="great">' in page)
    check("render: ep-tag select exists", '<select id="ep-tag">' in page)
    check("render: ep-tag Auto option first", '<select id="ep-tag"><option value="">Auto</option>' in page)
    for t in USER_TAGS:
        check(f"render: ep-tag offers {t}", f'<option value="{t}">{t}</option>' in page)

    auto = _render_one(None)
    check("render: no override → data-tag is computed", 'data-tag="keep"' in auto)
    check("render: no override → no manual marker", "✎</span>" not in auto)

    odd = _render_one("Legacy Tag")
    check("render: unknown user_tag does not crash", 'data-tag="legacy tag"' in odd)


def test_mobile_layout() -> None:
    """Phone breakpoint CSS, sort select, hidden-column class and JS guards are present."""
    page = _render_one(None)
    check("mobile: media query exists", f"@media {_NARROW_MQ}" in page)
    check("mobile: sortsel exists", 'id="sortsel"' in page)
    check("mobile: sortsel offers IV% desc", 'value="pct:-1"' in page)
    check("mobile: col-hide-sm CSS rule", f".{_HIDE_SM}{{display:none}}" in page)
    for header in ("HP", "Fast", "Charged", "Seen"):
        check(f"mobile: {header} header hidden on phones",
              f'<th class="{_HIDE_SM}">{header}</th>' in page
              or f'class="{_HIDE_SM}">{header}</th>' in page)
    check("mobile: hidden cells tagged", page.count(_HIDE_SM) >= 16, str(page.count(_HIDE_SM)))
    check("mobile: matchMedia guard present", "window.matchMedia(NARROW_MQ)" in page
          and f"const NARROW_MQ = '{_NARROW_MQ}'" in page)
    check("mobile: a/d/s under IV%", '<span class="ivs-sm">15/14/13</span>' in page)


def main() -> int:
    test_parse_user_tag()
    print()
    test_render_user_tag()
    print()
    test_mobile_layout()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("all tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
