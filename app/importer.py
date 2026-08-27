"""Writing the edited preview into the Cronometer diary.

This module receives the payload the browser built and writes exactly that. It
never re-parses the original text -- by the time we get here the draft has been
edited (portions moved between days, rows deleted, breakfasts toggled) and the
paste is no longer the truth.

Two properties matter more than anything else here:

  Idempotency. Pasting the same email twice must write once. Every diary entry
  we successfully write is recorded in SQLite; before writing we count what is
  already recorded for a (day, group, food) triple and write only the
  shortfall. Counting rather than upserting is deliberate -- two portions of
  the same soup on the same day is legitimate, so a unique key would silently
  drop the second.

  Partial failure is normal. One dish that fails to resolve, or one entry
  Cronometer rejects, must not abort the rest. Every entry gets its own status.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from app import db
from app.constants import BREAKFAST, DIARY_GROUP_BREAKFAST, DIARY_GROUP_LUNCH
from app.cronometer import CronometerAdapter, CronometerUnavailable
from app.parser import normalize_dish_name
from app.resolve import Dish, Macros, Resolution, ensure_food, resolve_all

logger = logging.getLogger(__name__)

GROUP_NAMES = {DIARY_GROUP_BREAKFAST: "breakfast", DIARY_GROUP_LUNCH: "lunch"}

# Only these resolution statuses may be logged. A `conflict` resolution *does*
# carry a Food -- the conflicting one -- so checking `res.food is not None` is
# not enough: that would log the stale-macro food we specifically refused to
# bind, which is the silent-poisoning failure this app exists to avoid.
WRITABLE_STATUSES = frozenset({"cached", "adopted", "created"})


@dataclass
class EntryResult:
    date: str
    group: str
    name: str
    status: str  # created | skipped | failed
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "date": self.date,
            "group": self.group,
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
        }


def breakfast_dishes() -> list[Dish]:
    """The fixed breakfast, as dishes for the same resolution ladder.

    They go through the identical path as a delivered dish: cache, then an
    existing food of mine, then create. Only the account's own foods are
    considered -- we never match against Cronometer's database, so the macros
    logged are exactly the ones in app/constants.py.
    """
    return [
        Dish(
            name=f.name,
            normalized_name=normalize_dish_name(f.name),
            macros=Macros(f.kcal, f.carbs_g, f.protein_g, f.fat_g),
            serving_name=f.serving_name,
            serving_grams=f.grams,
        )
        for f in BREAKFAST
    ]


def collect_dishes(payload: dict) -> list[Dish]:
    """Every unique dish the payload needs, lunch and breakfast alike."""
    seen: dict[str, Dish] = {}

    for day in payload.get("days", []):
        for row in day.get("rows", []):
            key = row.get("normalized_name") or normalize_dish_name(row["name"])
            if key not in seen:
                seen[key] = Dish(
                    name=row["name"],
                    normalized_name=key,
                    macros=Macros(
                        float(row["kcal"]),
                        float(row["carbs_g"]),
                        float(row["protein_g"]),
                        float(row["fat_g"]),
                    ),
                )
        if day.get("breakfast"):
            for d in breakfast_dishes():
                seen.setdefault(d.normalized_name, d)

    return list(seen.values())


def _wants(day: dict) -> list[tuple[int, str, str, int]]:
    """What a single day needs: (diary_group, dish_key, display_name, count).

    Lunch rows are one portion each, so identical dishes on the same day are
    counted rather than merged -- Friday's two soups stay two entries.
    """
    out: list[tuple[int, str, str, int]] = []

    names: dict[str, str] = {}
    counts: Counter[str] = Counter()
    for row in day.get("rows", []):
        key = row.get("normalized_name") or normalize_dish_name(row["name"])
        counts[key] += 1
        names.setdefault(key, row["name"])
    for key, n in counts.items():
        out.append((DIARY_GROUP_LUNCH, key, names[key], n))

    if day.get("breakfast"):
        for d in breakfast_dishes():
            out.append((DIARY_GROUP_BREAKFAST, d.normalized_name, d.name, 1))

    return out


def run_import(
    conn,
    adapter: CronometerAdapter,
    payload: dict,
    *,
    decisions: dict[str, str] | None = None,
) -> dict:
    """Write the edited payload. Returns a per-entry result array."""
    decisions = decisions or {}
    days = [d["date"] for d in payload.get("days", [])]

    # --- resolve every dish first, so a conflict is reported before any of
    #     that dish's entries are attempted.
    dishes = collect_dishes(payload)
    resolutions: dict[str, Resolution] = resolve_all(
        conn, adapter, dishes, decisions=decisions
    )

    # --- create the ones that need creating (this writes to Cronometer)
    for key, res in list(resolutions.items()):
        if res.status == "will_create":
            resolutions[key] = ensure_food(
                conn, adapter, res, decision=decisions.get(key)
            )

    run_id = db.start_run(conn, days)
    entries: list[EntryResult] = []

    for day in payload.get("days", []):
        day_str = day["date"]
        try:
            day_date = date.fromisoformat(day_str)
        except ValueError:
            entries.append(
                EntryResult(day_str, "-", "-", "failed", f"not a date: {day_str!r}")
            )
            continue

        for group, key, display, count in _wants(day):
            res = resolutions.get(key)
            group_name = GROUP_NAMES.get(group, str(group))

            # Not safely writable: report every portion, write none of them.
            if res is None or res.status not in WRITABLE_STATUSES or res.food is None:
                detail = (
                    res.message
                    if res is not None and res.message
                    else "could not be resolved to a Cronometer food"
                )
                if res is not None and res.status == "conflict":
                    detail = res.message + " Choose 'use existing' or 'new version'."
                for _ in range(count):
                    entries.append(
                        EntryResult(day_str, group_name, display, "failed", detail)
                    )
                continue

            food = res.food

            # Idempotency: how many of these are already on record for this
            # day and group? Write only the shortfall.
            already = db.count_recorded(
                conn, day=day_str, diary_group=group, food_id=food.food_id
            )
            skipped = min(count, already)
            for _ in range(skipped):
                entries.append(
                    EntryResult(
                        day_str,
                        group_name,
                        display,
                        "skipped",
                        "already imported for this day",
                    )
                )

            for _ in range(count - skipped):
                try:
                    entry_id = adapter.add_entry(
                        food=food, day=day_date, diary_group=group
                    )
                except CronometerUnavailable as exc:
                    entries.append(
                        EntryResult(day_str, group_name, display, "failed", str(exc))
                    )
                    continue

                db.record_entry(
                    conn,
                    day=day_str,
                    diary_group=group,
                    food_id=food.food_id,
                    normalized_name=key,
                    display_name=display,
                    servings=1.0,
                    entry_id=entry_id,
                    run_id=run_id,
                )
                entries.append(
                    EntryResult(day_str, group_name, display, "created", "")
                )

    created = sum(1 for e in entries if e.status == "created")
    skipped = sum(1 for e in entries if e.status == "skipped")
    failed = sum(1 for e in entries if e.status == "failed")
    db.finish_run(conn, run_id, created=created, skipped=skipped, failed=failed)

    logger.info(
        "import run %d: %d created, %d skipped, %d failed",
        run_id,
        created,
        skipped,
        failed,
    )

    return {
        "run_id": run_id,
        # Days we actually put something new into. A re-paste of an already
        # imported week reports 0 days written and everything skipped, which is
        # the honest reading of what happened.
        "days_written": len({e.date for e in entries if e.status == "created"}),
        "created": created,
        "skipped": skipped,
        "failed": failed,
        "entries": [e.as_dict() for e in entries],
        "resolutions": [r.as_dict() for r in resolutions.values()],
    }
