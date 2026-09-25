"""Findings registered by users in the app, stored in SQLite.

Each finding belongs to the user who registered it. Findings by trusted users
(admins and users with the "training" permission) are used as training data
together with the GBIF findings (see train.load_findings), weighted by
config.OWN_FINDING_WEIGHT.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import polars as pl

from soppkart import config

DB_PATH = config.DATA_DIR / "user" / "findings.sqlite"


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS findings (
                id INTEGER PRIMARY KEY,
                species TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                accuracy_m REAL,
                found_at TEXT NOT NULL,
                note TEXT
            )""")
        # Findings from before logins existed have no owner (NULL) until claimed.
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(findings)")}
        if "user_id" not in columns:
            conn.execute("ALTER TABLE findings ADD COLUMN user_id INTEGER")
        yield conn
        conn.commit()
    finally:
        conn.close()


def add(
    user_id: int,
    species: str,
    lat: float,
    lon: float,
    accuracy_m: float | None,
    note: str | None,
) -> dict[str, Any]:
    found_at = datetime.now(UTC).isoformat(timespec="seconds")
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO findings (user_id, species, lat, lon, accuracy_m, found_at, note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, species, lat, lon, accuracy_m, found_at, note),
        )
        row_id = cur.lastrowid
    return {
        "id": row_id,
        "user_id": user_id,
        "species": species,
        "lat": lat,
        "lon": lon,
        "accuracy_m": accuracy_m,
        "found_at": found_at,
        "note": note,
    }


def list_for(species: str, user_id: int | None = None) -> list[dict[str, Any]]:
    """The species' findings by one user, or by everyone when user_id is None."""
    with connect() as conn:
        if user_id is None:
            rows = conn.execute(
                "SELECT * FROM findings WHERE species = ? ORDER BY found_at", (species,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM findings WHERE species = ? AND user_id = ? ORDER BY found_at",
                (species, user_id),
            ).fetchall()
    return [dict(r) for r in rows]


def get(finding_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
    return dict(row) if row else None


def delete(finding_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM findings WHERE id = ?", (finding_id,))
    return cur.rowcount > 0


def claim_unowned(user_id: int) -> int:
    """Give findings without an owner (from before logins) to a user. Returns how many."""
    with connect() as conn:
        cur = conn.execute("UPDATE findings SET user_id = ? WHERE user_id IS NULL", (user_id,))
    return cur.rowcount


def as_frame(species: str, user_ids: set[int]) -> pl.DataFrame:
    """These users' findings of the species, with the same columns as the GBIF findings."""
    rows = [r for r in list_for(species) if r["user_id"] in user_ids]
    return pl.DataFrame(
        {
            "lat": [r["lat"] for r in rows],
            "lon": [r["lon"] for r in rows],
            "year": [int(r["found_at"][:4]) for r in rows],
            "uncertainty_m": [r["accuracy_m"] for r in rows],
        },
        schema={
            "lat": pl.Float64,
            "lon": pl.Float64,
            "year": pl.Int64,
            "uncertainty_m": pl.Float64,
        },
    )
