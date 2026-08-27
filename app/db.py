"""SQLite state: the food-id cache, the written-entry ledger, and import runs.

Everything here lives on the mounted volume so it survives `docker compose
down`. The Cronometer session token is also parked in this directory (see
DATA_DIR / session.json) -- Cronometer rate-limits login aggressively, so the
token has to outlive the container.

The ledger is what makes imports idempotent. Every diary entry we successfully
write is recorded as its own row; on a later import we count what is already
recorded for a (day, group, food) triple and only write the shortfall. Counting
rather than upserting is deliberate: two portions of the same soup on the same
day is a legitimate state, so a UNIQUE key on the triple would silently drop
the second one.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DB_PATH = DATA_DIR / "cronohelper.sqlite3"
SESSION_PATH = DATA_DIR / "session.json"

_SCHEMA = """
PRAGMA journal_mode=WAL;

-- normalized dish name -> the Cronometer food we bound it to.
CREATE TABLE IF NOT EXISTS food_cache (
    normalized_name    TEXT PRIMARY KEY,
    cronometer_food_id INTEGER NOT NULL,
    measure_id         INTEGER,
    -- Gram weight of measure_id, and the translation id. Both are needed to
    -- log a serving, and caching them keeps a cache hit to zero API calls.
    grams_per_serving  REAL NOT NULL DEFAULT 100,
    translation_id     INTEGER NOT NULL DEFAULT 0,
    display_name       TEXT NOT NULL,
    -- The macros we verified at bind time. Kept so a later week can detect
    -- that the bound food drifted, instead of trusting the id forever.
    kcal               REAL NOT NULL,
    carbs_g            REAL NOT NULL,
    protein_g          REAL NOT NULL,
    fat_g              REAL NOT NULL,
    created_by_app     INTEGER NOT NULL DEFAULT 0,
    bound_at           TEXT NOT NULL
);

-- One row per diary entry this app has successfully written.
CREATE TABLE IF NOT EXISTS diary_entries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    day                 TEXT NOT NULL,
    diary_group         INTEGER NOT NULL,
    cronometer_food_id  INTEGER NOT NULL,
    normalized_name     TEXT NOT NULL,
    display_name        TEXT NOT NULL,
    servings            REAL NOT NULL,
    cronometer_entry_id TEXT,
    run_id              INTEGER,
    written_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entries_day ON diary_entries(day);
CREATE INDEX IF NOT EXISTS idx_entries_triple
    ON diary_entries(day, diary_group, cronometer_food_id);

CREATE TABLE IF NOT EXISTS import_runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    days       TEXT NOT NULL,
    created    INTEGER NOT NULL DEFAULT 0,
    skipped    INTEGER NOT NULL DEFAULT 0,
    failed     INTEGER NOT NULL DEFAULT 0
);
"""


def init(db_path: Path | None = None) -> None:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(_SCHEMA)


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --- food cache ------------------------------------------------------------


def get_cached_food(conn: sqlite3.Connection, normalized_name: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM food_cache WHERE normalized_name = ?", (normalized_name,)
    ).fetchone()
    return dict(row) if row else None


def bind_food(
    conn: sqlite3.Connection,
    *,
    normalized_name: str,
    food_id: int,
    measure_id: int | None,
    grams_per_serving: float,
    translation_id: int,
    display_name: str,
    kcal: float,
    carbs_g: float,
    protein_g: float,
    fat_g: float,
    created_by_app: bool,
) -> None:
    """Bind a dish name to a Cronometer food id.

    Only ever called once the food's macros have been checked against the
    pasted values -- binding an unverified id is the failure mode that poisons
    every future week.
    """
    conn.execute(
        """
        INSERT INTO food_cache (normalized_name, cronometer_food_id, measure_id,
                                grams_per_serving, translation_id,
                                display_name, kcal, carbs_g, protein_g, fat_g,
                                created_by_app, bound_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(normalized_name) DO UPDATE SET
            cronometer_food_id = excluded.cronometer_food_id,
            measure_id         = excluded.measure_id,
            grams_per_serving  = excluded.grams_per_serving,
            translation_id     = excluded.translation_id,
            display_name       = excluded.display_name,
            kcal               = excluded.kcal,
            carbs_g            = excluded.carbs_g,
            protein_g          = excluded.protein_g,
            fat_g              = excluded.fat_g,
            created_by_app     = excluded.created_by_app,
            bound_at           = excluded.bound_at
        """,
        (
            normalized_name,
            food_id,
            measure_id,
            grams_per_serving,
            translation_id,
            display_name,
            kcal,
            carbs_g,
            protein_g,
            fat_g,
            1 if created_by_app else 0,
            _now(),
        ),
    )


# --- the written-entry ledger ---------------------------------------------


def count_recorded(
    conn: sqlite3.Connection, *, day: str, diary_group: int, food_id: int
) -> int:
    """How many entries of this food are already recorded for this day+group."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM diary_entries
        WHERE day = ? AND diary_group = ? AND cronometer_food_id = ?
        """,
        (day, diary_group, food_id),
    ).fetchone()
    return int(row["n"])


def record_entry(
    conn: sqlite3.Connection,
    *,
    day: str,
    diary_group: int,
    food_id: int,
    normalized_name: str,
    display_name: str,
    servings: float,
    entry_id: str | None,
    run_id: int | None,
) -> None:
    conn.execute(
        """
        INSERT INTO diary_entries (day, diary_group, cronometer_food_id,
                                   normalized_name, display_name, servings,
                                   cronometer_entry_id, run_id, written_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            day,
            diary_group,
            food_id,
            normalized_name,
            display_name,
            servings,
            entry_id,
            run_id,
            _now(),
        ),
    )


def days_with_entries(conn: sqlite3.Connection) -> list[str]:
    """Every date this app has already written to. The preview uses this to
    make already-imported days visually distinct *before* anything is clicked."""
    return [
        r["day"]
        for r in conn.execute(
            "SELECT DISTINCT day FROM diary_entries ORDER BY day"
        ).fetchall()
    ]


# --- import runs -----------------------------------------------------------


def start_run(conn: sqlite3.Connection, days: list[str]) -> int:
    cur = conn.execute(
        "INSERT INTO import_runs (started_at, days) VALUES (?, ?)",
        (_now(), ",".join(days)),
    )
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection, run_id: int, *, created: int, skipped: int, failed: int
) -> None:
    conn.execute(
        "UPDATE import_runs SET created = ?, skipped = ?, failed = ? WHERE id = ?",
        (created, skipped, failed, run_id),
    )


def recent_runs(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM import_runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["days"] = [x for x in d["days"].split(",") if x]
        out.append(d)
    return out
