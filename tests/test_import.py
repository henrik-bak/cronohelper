"""The write path: what lands in the diary, on which date, in which group,
and what happens when the same paste is imported twice.
"""

from __future__ import annotations

import pytest

from app.constants import BREAKFAST, DIARY_GROUP_BREAKFAST, DIARY_GROUP_LUNCH
from app.importer import run_import
from app.parser import normalize_dish_name, parse_text
from tests.fixtures import FRIDAY, FRIDAY_DISH, SAMPLE_PASTE

SATURDAY = "2026-09-05"


def payload_from_rows(rows, *, breakfast=True):
    """Group parser rows into the day payload the browser would send."""
    by_date: dict[str, list] = {}
    for r in rows:
        by_date.setdefault(r.date, []).append(
            {
                "name": r.name,
                "normalized_name": r.normalized_name,
                "kcal": r.kcal,
                "carbs_g": r.carbs_g,
                "protein_g": r.protein_g,
                "fat_g": r.fat_g,
            }
        )
    return {
        "days": [
            {"date": d, "breakfast": breakfast, "rows": rs}
            for d, rs in sorted(by_date.items())
        ]
    }


def sample_payload(**kw):
    return payload_from_rows(parse_text(SAMPLE_PASTE).rows, **kw)


# --- what gets written -----------------------------------------------------


def test_lunch_and_breakfast_land_in_the_right_diary_groups(conn, adapter, fake):
    result = run_import(conn, adapter, sample_payload())

    assert result["failed"] == 0, result["entries"]

    monday = fake.servings_on("2026-08-31")
    lunches = [s for s in monday if s["diaryGroup"] == DIARY_GROUP_LUNCH]
    breakfasts = [s for s in monday if s["diaryGroup"] == DIARY_GROUP_BREAKFAST]

    assert len(lunches) == 2
    assert len(breakfasts) == len(BREAKFAST) == 2
    # Never diary group 0 -- "auto" would file meals by the wall clock.
    assert all(s["diaryGroup"] in (1, 2) for s in fake.servings)


def test_breakfast_is_the_two_fixed_foods(conn, adapter, fake):
    run_import(conn, adapter, sample_payload())
    names = {
        fake.name_of(s["foodId"])
        for s in fake.servings_on("2026-08-31", DIARY_GROUP_BREAKFAST)
    }
    for f in BREAKFAST:
        assert any(f.name in n for n in names), f"{f.name} missing from breakfast"


def test_breakfast_can_be_switched_off_for_a_day(conn, adapter, fake):
    payload = sample_payload()
    payload["days"][0]["breakfast"] = False

    run_import(conn, adapter, payload)

    assert fake.servings_on("2026-08-31", DIARY_GROUP_BREAKFAST) == []
    assert len(fake.servings_on("2026-09-01", DIARY_GROUP_BREAKFAST)) == 2


def test_two_portions_of_one_dish_write_two_entries(conn, adapter, fake):
    # Friday's `2 adag` line is two rows, and both must land -- a unique key on
    # (day, group, food) would silently drop the second.
    run_import(conn, adapter, sample_payload())

    friday_lunch = fake.servings_on(FRIDAY, DIARY_GROUP_LUNCH)
    assert len(friday_lunch) == 2
    assert friday_lunch[0]["foodId"] == friday_lunch[1]["foodId"]


def test_each_entry_is_exactly_one_serving(conn, adapter, fake):
    run_import(conn, adapter, sample_payload())
    for s in fake.servings:
        food = fake.foods[s["foodId"]]
        assert s["grams"] == food["measures"][0]["value"]
        assert s["measureId"] == food["defaultMeasureId"]


def test_the_date_written_is_the_date_meant(conn, adapter, fake):
    # No timezone arithmetic anywhere: the ISO date in the payload is the day
    # passed to add_serving.
    run_import(conn, adapter, sample_payload())
    written = {s["day"] for s in fake.servings}
    assert written == {
        "2026-08-31",
        "2026-09-01",
        "2026-09-02",
        "2026-09-03",
        "2026-09-04",
    }


# --- idempotency -----------------------------------------------------------


def test_importing_the_same_payload_twice_writes_once(conn, adapter, fake):
    first = run_import(conn, adapter, sample_payload())
    written_after_first = len(fake.servings)

    second = run_import(conn, adapter, sample_payload())

    assert first["created"] > 0
    assert first["skipped"] == 0
    assert second["created"] == 0, "a re-paste must write nothing"
    assert second["skipped"] == first["created"]
    assert second["days_written"] == 0
    assert len(fake.servings) == written_after_first, "no new servings at all"


def test_second_import_creates_no_new_foods(conn, adapter, fake):
    run_import(conn, adapter, sample_payload())
    created_foods = fake.calls["create_custom_food"]

    run_import(conn, adapter, sample_payload())

    assert fake.calls["create_custom_food"] == created_foods


def test_partial_re_import_writes_only_the_shortfall(conn, adapter, fake):
    # Import Friday with one portion, then again with both. The second run must
    # add exactly one entry, not two and not zero.
    rows = [r for r in parse_text(SAMPLE_PASTE).rows if r.date == FRIDAY]
    assert len(rows) == 2

    run_import(conn, adapter, payload_from_rows(rows[:1], breakfast=False))
    assert len(fake.servings_on(FRIDAY, DIARY_GROUP_LUNCH)) == 1

    result = run_import(conn, adapter, payload_from_rows(rows, breakfast=False))

    assert result["created"] == 1
    assert result["skipped"] == 1
    assert len(fake.servings_on(FRIDAY, DIARY_GROUP_LUNCH)) == 2


def test_breakfast_is_idempotent_too(conn, adapter, fake):
    payload = sample_payload()
    run_import(conn, adapter, payload)
    before = len(fake.servings_on("2026-08-31", DIARY_GROUP_BREAKFAST))

    run_import(conn, adapter, payload)

    assert len(fake.servings_on("2026-08-31", DIARY_GROUP_BREAKFAST)) == before == 2


# --- a moved portion -------------------------------------------------------


def test_moved_portion_lands_on_its_new_date_and_that_date_gets_breakfast(
    conn, adapter, fake
):
    # The Saturday case: one of Friday's two portions is dragged onto a date
    # that was never in the paste.
    rows = parse_text(SAMPLE_PASTE).rows
    friday_rows = [r for r in rows if r.name == FRIDAY_DISH]
    friday_rows[1].date = SATURDAY

    result = run_import(conn, adapter, payload_from_rows(rows))
    assert result["failed"] == 0, result["entries"]

    # The portion landed on Saturday...
    sat_lunch = fake.servings_on(SATURDAY, DIARY_GROUP_LUNCH)
    assert len(sat_lunch) == 1
    assert FRIDAY_DISH in fake.name_of(sat_lunch[0]["foodId"])

    # ...Friday kept exactly one...
    assert len(fake.servings_on(FRIDAY, DIARY_GROUP_LUNCH)) == 1

    # ...and Saturday got the fixed breakfast, because breakfast follows the
    # edited draft, not the pasted range.
    assert len(fake.servings_on(SATURDAY, DIARY_GROUP_BREAKFAST)) == len(BREAKFAST)


def test_a_day_with_only_breakfast_is_still_written(conn, adapter, fake):
    # Breakfast is independent of lunch: a day with no rows and breakfast on
    # is a real day to import.
    payload = {"days": [{"date": SATURDAY, "breakfast": True, "rows": []}]}

    result = run_import(conn, adapter, payload)

    assert result["failed"] == 0
    assert result["created"] == len(BREAKFAST)
    assert result["days_written"] == 1
    assert fake.servings_on(SATURDAY, DIARY_GROUP_LUNCH) == []


def test_moving_a_portion_reuses_the_same_food(conn, adapter, fake):
    rows = parse_text(SAMPLE_PASTE).rows
    [r for r in rows if r.name == FRIDAY_DISH][1].date = SATURDAY
    run_import(conn, adapter, payload_from_rows(rows))

    fri = fake.servings_on(FRIDAY, DIARY_GROUP_LUNCH)[0]
    sat = fake.servings_on(SATURDAY, DIARY_GROUP_LUNCH)[0]
    assert fri["foodId"] == sat["foodId"], "moving a portion must not clone the food"


# --- partial failure -------------------------------------------------------


def test_a_conflicting_dish_fails_without_aborting_the_rest(conn, adapter, fake):
    # One dish already exists with wrong macros; every other dish must still
    # be written.
    fake.add_food(FRIDAY_DISH, kcal=400.0, carbs_g=15.0, protein_g=26.6, fat_g=1.3)

    result = run_import(conn, adapter, sample_payload())

    failed = [e for e in result["entries"] if e["status"] == "failed"]
    assert len(failed) == 2, "both Friday portions of the conflicting dish"
    assert all(FRIDAY_DISH in e["name"] for e in failed)
    assert "differ" in failed[0]["detail"]

    assert result["created"] > 0
    assert fake.servings_on("2026-08-31", DIARY_GROUP_LUNCH), "Monday still written"
    assert fake.servings_on(FRIDAY, DIARY_GROUP_LUNCH) == [], "conflict wrote nothing"


def test_a_conflict_can_be_resolved_and_re_imported(conn, adapter, fake):
    fake.add_food(FRIDAY_DISH, kcal=400.0, carbs_g=15.0, protein_g=26.6, fat_g=1.3)
    payload = sample_payload()

    run_import(conn, adapter, payload)
    assert fake.servings_on(FRIDAY, DIARY_GROUP_LUNCH) == []

    # The user picks "new version" for the conflicting dish and re-imports.
    result = run_import(
        conn,
        adapter,
        payload,
        decisions={normalize_dish_name(FRIDAY_DISH): "create_new_version"},
    )

    assert result["failed"] == 0
    assert len(fake.servings_on(FRIDAY, DIARY_GROUP_LUNCH)) == 2
    # Everything already written the first time is skipped, not duplicated.
    assert result["skipped"] > 0


def test_result_counts_add_up(conn, adapter, fake):
    result = run_import(conn, adapter, sample_payload())
    total = result["created"] + result["skipped"] + result["failed"]
    assert total == len(result["entries"])


def test_history_records_the_run(conn, adapter, fake):
    from app import db

    run_import(conn, adapter, sample_payload())

    runs = db.recent_runs(conn)
    assert len(runs) == 1
    assert runs[0]["created"] > 0
    assert runs[0]["failed"] == 0
    assert "2026-08-31" in runs[0]["days"]
    assert set(db.days_with_entries(conn)) == {
        "2026-08-31",
        "2026-09-01",
        "2026-09-02",
        "2026-09-03",
        "2026-09-04",
    }
