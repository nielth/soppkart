"""Findings registered by the user in the app, stored in SQLite.

They are used as training data together with the GBIF findings (see
train.load_findings), weighted by config.OWN_FINDING_WEIGHT.
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
        yield conn
        conn.commit()
    finally:
        conn.close()


def add(
    species: str, lat: float, lon: float, accuracy_m: float | None, note: str | None
) -> dict[str, Any]:
    found_at = datetime.now(UTC).isoformat(timespec="seconds")
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO findings (species, lat, lon, accuracy_m, found_at, note) VALUES (?, ?, ?, ?, ?, ?)",
            (species, lat, lon, accuracy_m, found_at, note),
        )
        row_id = cur.lastrowid
    return {
        "id": row_id,
        "species": species,
        "lat": lat,
        "lon": lon,
        "accuracy_m": accuracy_m,
        "found_at": found_at,
        "note": note,
    }


def list_for(species: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM findings WHERE species = ? ORDER BY found_at", (species,)
        ).fetchall()
    return [dict(r) for r in rows]


def delete(finding_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM findings WHERE id = ?", (finding_id,))
    return cur.rowcount > 0


def as_frame(species: str) -> pl.DataFrame:
    """The species' own findings with the same columns as the GBIF findings."""
    rows = list_for(species)
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
