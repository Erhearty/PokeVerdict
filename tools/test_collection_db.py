#!/usr/bin/env python3
"""
Unit tests for CollectionDB.set_user_tag (sticky manual tag override).

    python3 tools/test_collection_db.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from collection_db import CollectionDB  # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {label}{' — ' + detail if detail else ''}")
    if not ok:
        FAILURES.append(label)


def _result(species: str, ivs: list[int], cp: int) -> dict:
    """Minimal appraisal result dict accepted by CollectionDB.record."""
    return {
        "species": species,
        "speciesTemplateID": species.upper(),
        "ivs": ivs,
        "total": sum(ivs),
        "cp": cp,
        "maxHP": 100,
        "verdict": "KEEP",
        "reasons": ["test"],
        "suggestedTag": "Keep",
    }


def _user_tag(db: CollectionDB, eid: int):
    """user_tag of the live entity eid in fetch_collection, or a sentinel if absent."""
    for row in db.fetch_collection():
        if row["entity_id"] == eid:
            return row["user_tag"]
    return "<missing>"


def test_set_user_tag(db: CollectionDB) -> None:
    """Setting, clearing, unknown and disposed ids."""
    eid, _ = db.record(_result("Gible", [15, 14, 13], 900))
    check("set: returns True", db.set_user_tag(eid, "Great") is True)
    check("set: fetch_collection has user_tag", _user_tag(db, eid) == "Great", str(_user_tag(db, eid)))
    check("clear: returns True", db.set_user_tag(eid, None) is True)
    check("clear: user_tag is NULL", _user_tag(db, eid) is None, str(_user_tag(db, eid)))
    check("unknown id → False", db.set_user_tag(999999, "Keep") is False)

    gone, _ = db.record(_result("Bidoof", [1, 2, 3], 200))
    db.dispose(gone)
    check("disposed id → False", db.set_user_tag(gone, "Keep") is False)


def test_update_verdict_keeps_user_tag(db: CollectionDB) -> None:
    """Re-evaluating the latest observation must not touch the manual tag."""
    eid, _ = db.record(_result("Ralts", [10, 11, 12], 500))
    db.set_user_tag(eid, "Raid")
    obs = db.latest_observation_for_entity(eid)
    db.update_verdict(obs["id"], "transfer", "junk", "Transfer")
    check("update_verdict leaves user_tag", _user_tag(db, eid) == "Raid", str(_user_tag(db, eid)))


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db = CollectionDB(os.path.join(tmp, "coll.sqlite"))
        test_set_user_tag(db)
        print()
        test_update_verdict_keeps_user_tag(db)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("all tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
