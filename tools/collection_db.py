#!/usr/bin/env python3
"""
Python port of the CollectionDatabase schema (Sources/PokeVerdictCore/Data/CollectionDatabase.swift).

Observations are facts; entities are the interpretation and can be revised.
Entity resolution: same identity_key + non-decreasing CP → same entity.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone


_DDL = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS entity (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_key   TEXT,
    nickname       TEXT,
    user_tag       TEXT,
    is_disposed    INTEGER NOT NULL DEFAULT 0,
    notes          TEXT,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entity_key ON entity(identity_key);
CREATE INDEX IF NOT EXISTS idx_entity_tag ON entity(user_tag);

CREATE TABLE IF NOT EXISTS observation (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at         TEXT NOT NULL,
    species_template_id TEXT NOT NULL,
    display_name        TEXT NOT NULL,
    cp                  INTEGER NOT NULL,
    hp                  INTEGER NOT NULL,
    stardust_cost       INTEGER,
    attack_iv           INTEGER,
    defense_iv          INTEGER,
    stamina_iv          INTEGER,
    iv_total            INTEGER,
    iv_certain          INTEGER NOT NULL DEFAULT 0,
    is_shiny            INTEGER NOT NULL DEFAULT 0,
    is_shadow           INTEGER NOT NULL DEFAULT 0,
    is_purified         INTEGER NOT NULL DEFAULT 0,
    is_lucky            INTEGER NOT NULL DEFAULT 0,
    is_costume          INTEGER NOT NULL DEFAULT 0,
    size_class          TEXT,
    verdict             TEXT,
    verdict_reason      TEXT,
    gamedata_version    TEXT,
    entity_id           INTEGER REFERENCES entity(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_captured  ON observation(captured_at);
CREATE INDEX IF NOT EXISTS idx_obs_species   ON observation(species_template_id);
CREATE INDEX IF NOT EXISTS idx_obs_entity    ON observation(entity_id);
CREATE INDEX IF NOT EXISTS idx_obs_iv_total  ON observation(iv_total);

CREATE TABLE IF NOT EXISTS capture_failure (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at  TEXT NOT NULL,
    raw_text     TEXT,
    reason       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS screenshot (
    observation_id INTEGER PRIMARY KEY REFERENCES observation(id) ON DELETE CASCADE,
    data           BLOB    NOT NULL,
    mime_type      TEXT    NOT NULL DEFAULT 'image/jpeg'
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CollectionDB:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)
        for _migration in [
            "ALTER TABLE observation ADD COLUMN verdict_tag TEXT",
            "ALTER TABLE observation ADD COLUMN is_dynamax INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE capture_failure ADD COLUMN screenshot BLOB",
            "ALTER TABLE capture_failure ADD COLUMN mime_type TEXT",
        ]:
            try:
                self._conn.execute(_migration)
                self._conn.commit()
            except Exception:
                pass

    def _resolve_entity(self, identity_key: str | None, cp: int, now: str) -> int:
        if identity_key:
            row = self._conn.execute("""
                SELECT e.id, MAX(o.cp) AS peak_cp
                FROM entity e
                JOIN observation o ON o.entity_id = e.id
                WHERE e.identity_key = ? AND e.is_disposed = 0
                GROUP BY e.id
            """, (identity_key,)).fetchone()
            if row and cp >= (row["peak_cp"] or 0):
                self._conn.execute(
                    "UPDATE entity SET last_seen_at = ? WHERE id = ?", (now, row["id"])
                )
                return row["id"]

        self._conn.execute(
            "INSERT INTO entity (identity_key, is_disposed, first_seen_at, last_seen_at)"
            " VALUES (?, 0, ?, ?)",
            (identity_key, now, now),
        )
        return self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def record(self, result: dict) -> tuple[int, int | None]:
        """Persist one appraisal result. Returns (entity_id, observation_id or None if duplicate)."""
        now = _now_iso()
        a, d, s = (result.get("ivs") or [None, None, None])[:3]
        total = result.get("total")
        template_id = result.get("speciesTemplateID") or result.get("species", "")
        display = result.get("species", "")
        cp = result.get("cp") or max(result.get("candidateCP") or [0], default=0)

        identity_key = None
        if a is not None and d is not None and s is not None:
            identity_key = f"{template_id}|{a}/{d}/{s}|False|False|False"

        with self._lock:
            with self._conn:
                entity_id = self._resolve_entity(identity_key, cp, now)
                # On a true duplicate (same IVs and CP) refresh in place: update
                # verdict/flags/timestamp and re-save the screenshot rather than
                # inserting a second row or silently discarding the re-appraisal.
                if a is not None and d is not None and s is not None:
                    dup = self._conn.execute(
                        "SELECT id FROM observation WHERE entity_id = ? "
                        "AND attack_iv = ? AND defense_iv = ? AND stamina_iv = ? AND cp = ?",
                        (entity_id, a, d, s, cp),
                    ).fetchone()
                    if dup:
                        dup_id = dup[0]
                        self._conn.execute("""
                            UPDATE observation SET
                                captured_at     = ?,
                                hp              = ?,
                                iv_certain      = ?,
                                is_shiny        = ?,
                                is_shadow       = ?,
                                is_purified     = ?,
                                is_lucky        = ?,
                                is_costume      = ?,
                                is_dynamax      = ?,
                                verdict         = ?,
                                verdict_reason  = ?,
                                verdict_tag     = ?
                            WHERE id = ?
                        """, (
                            now,
                            result.get("maxHP") or 0,
                            1 if result.get("iv_certain", True) else 0,
                            1 if result.get("isShiny")    is True else 0,
                            1 if result.get("isShadow")   is True else 0,
                            1 if result.get("isPurified") is True else 0,
                            1 if result.get("isLucky")    is True else 0,
                            1 if result.get("isCostume")  is True else 0,
                            1 if result.get("isDynamax")  is True else 0,
                            (result.get("verdict") or "").lower() or None,
                            "; ".join(result.get("reasons") or [result.get("reason")] or []) or None,
                            result.get("suggestedTag"),
                            dup_id,
                        ))
                        return entity_id, dup_id
                self._conn.execute("""
                    INSERT INTO observation (
                        captured_at, species_template_id, display_name, cp, hp,
                        attack_iv, defense_iv, stamina_iv, iv_total, iv_certain,
                        is_shiny, is_shadow, is_purified, is_lucky, is_costume, is_dynamax,
                        verdict, verdict_reason, verdict_tag, entity_id
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    now, template_id, display, cp, result.get("maxHP") or 0,
                    a, d, s, total, 1 if result.get("iv_certain", True) else 0,
                    1 if result.get("isShiny")    is True else 0,
                    1 if result.get("isShadow")   is True else 0,
                    1 if result.get("isPurified") is True else 0,
                    1 if result.get("isLucky")    is True else 0,
                    1 if result.get("isCostume")  is True else 0,
                    1 if result.get("isDynamax")  is True else 0,
                    (result.get("verdict") or "").lower() or None,
                    "; ".join(result.get("reasons") or [result.get("reason")] or []) or None,
                    result.get("suggestedTag"),
                    entity_id,
                ))
                obs_id = self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return entity_id, obs_id

    def save_screenshot(self, obs_id: int, data: bytes, mime_type: str = "image/jpeg") -> None:
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO screenshot (observation_id, data, mime_type)"
                    " VALUES (?,?,?)",
                    (obs_id, data, mime_type),
                )

    def get_screenshot(self, obs_id: int) -> tuple[bytes, str] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data, mime_type FROM screenshot WHERE observation_id = ?",
                (obs_id,),
            ).fetchone()
        return (bytes(row["data"]), row["mime_type"]) if row else None

    def record_failure(self, raw_text: str, reason: str,
                       screenshot: bytes | None = None,
                       mime_type: str = "image/jpeg") -> int:
        """Persist a failed appraisal to capture_failure. Returns the new row id."""
        now = _now_iso()
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO capture_failure (captured_at, raw_text, reason, screenshot, mime_type)"
                    " VALUES (?,?,?,?,?)",
                    (now, raw_text, reason, screenshot, mime_type),
                )
                return self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_observation(self, obs_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM observation WHERE id = ?", (obs_id,)
            ).fetchone()

    def latest_observation_for_entity(self, entity_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM observation WHERE entity_id = ?"
                " ORDER BY captured_at DESC, id DESC LIMIT 1",
                (entity_id,),
            ).fetchone()

    def update_observation(self, entity_id: int, updates: dict) -> int | None:
        """Update allowed fields on the most recent observation. Returns obs_id or None."""
        allowed = {"is_shiny", "is_shadow", "is_purified", "is_lucky", "is_dynamax", "is_costume", "cp"}
        cols = {k: v for k, v in updates.items() if k in allowed}
        if not cols:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM observation WHERE entity_id = ?"
                " ORDER BY captured_at DESC, id DESC LIMIT 1",
                (entity_id,),
            ).fetchone()
            if row is None:
                return None
            obs_id = row[0]
            set_clause = ", ".join(f"{k} = ?" for k in cols)
            with self._conn:
                self._conn.execute(
                    f"UPDATE observation SET {set_clause} WHERE id = ?",
                    (*cols.values(), obs_id),
                )
        return obs_id

    def update_verdict(self, obs_id: int, verdict: str, reasons: str, tag: str) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE observation SET verdict=?, verdict_reason=?, verdict_tag=? WHERE id=?",
                    (verdict, reasons, tag, obs_id),
                )

    def fetch_collection(self) -> list[sqlite3.Row]:
        """Most recent observation per live entity, sorted by CP descending."""
        with self._lock:
            return self._conn.execute("""
                SELECT
                    e.id          AS entity_id,
                    o.id          AS observation_id,
                    o.species_template_id,
                    o.display_name,
                    o.cp,
                    o.hp,
                    o.attack_iv,
                    o.defense_iv,
                    o.stamina_iv,
                    o.iv_total,
                    o.iv_certain,
                    o.is_shiny,
                    o.is_shadow,
                    o.is_lucky,
                    o.is_costume,
                    o.is_dynamax,
                    o.verdict,
                    o.verdict_reason,
                    o.verdict_tag,
                    o.captured_at,
                    e.user_tag,
                    CASE WHEN s.observation_id IS NOT NULL THEN 1 ELSE 0 END AS has_screenshot
                FROM entity e
                JOIN observation o ON o.id = (
                    SELECT id FROM observation
                    WHERE entity_id = e.id
                    ORDER BY captured_at DESC, id DESC LIMIT 1
                )
                LEFT JOIN screenshot s ON s.observation_id = o.id
                WHERE e.is_disposed = 0
                ORDER BY o.cp DESC
            """).fetchall()

    def count(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM entity WHERE is_disposed = 0"
            ).fetchone()[0]

    def box_context(self, species_template_id: str) -> dict:
        """
        Return {count, bestIVTotal} for live entities of this species.

        Honest: only covers what has been appraised so far, never the full box.
        bestIVTotal is None when nothing has been seen yet.
        """
        with self._lock:
            row = self._conn.execute("""
                SELECT COUNT(DISTINCT e.id) AS n, MAX(o.iv_total) AS best
                FROM observation o
                JOIN entity e ON e.id = o.entity_id
                WHERE o.species_template_id = ? AND e.is_disposed = 0
            """, (species_template_id,)).fetchone()
        return {
            "count": row["n"] if row else 0,
            "bestIVTotal": row["best"] if row else None,
        }

    def family_context(self, template_ids: list[str]) -> dict:
        """
        Best IV total and display name across an entire evolutionary family.

        Honest: covers only what has been appraised so far.
        bestSpecies is the display name of the best-IV observation in the family.
        """
        if not template_ids:
            return {"count": 0, "bestIVTotal": None, "bestSpecies": None}
        ph = ",".join("?" * len(template_ids))
        with self._lock:
            agg = self._conn.execute(f"""
                SELECT COUNT(DISTINCT e.id) AS n, MAX(o.iv_total) AS best
                FROM observation o
                JOIN entity e ON e.id = o.entity_id
                WHERE o.species_template_id IN ({ph}) AND e.is_disposed = 0
            """, template_ids).fetchone()
            best = agg["best"] if agg else None
            best_species = None
            if best is not None:
                name_row = self._conn.execute(f"""
                    SELECT o.display_name
                    FROM observation o
                    JOIN entity e ON e.id = o.entity_id
                    WHERE o.species_template_id IN ({ph}) AND o.iv_total = ?
                      AND e.is_disposed = 0
                    LIMIT 1
                """, template_ids + [best]).fetchone()
                best_species = name_row[0] if name_row else None
        return {
            "count": agg["n"] if agg else 0,
            "bestIVTotal": best,
            "bestSpecies": best_species,
        }

    def dispose(self, entity_id: int) -> bool:
        """Mark an entity as disposed (transferred/deleted). Returns True if it existed."""
        with self._lock:
            with self._conn:
                cur = self._conn.execute(
                    "UPDATE entity SET is_disposed = 1 WHERE id = ? AND is_disposed = 0",
                    (entity_id,),
                )
        return cur.rowcount > 0

    def set_user_tag(self, entity_id: int, tag: str | None) -> bool:
        """Set (or clear with None) the sticky manual tag on a live entity. Returns True if it existed."""
        with self._lock:
            with self._conn:
                cur = self._conn.execute(
                    "UPDATE entity SET user_tag = ? WHERE id = ? AND is_disposed = 0",
                    (tag, entity_id),
                )
        return cur.rowcount > 0
