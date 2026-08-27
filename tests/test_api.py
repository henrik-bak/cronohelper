"""The HTTP surface, wired end to end against the fake Cronometer client."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db, main
from app.constants import BREAKFAST
from app.cronometer import CronometerAdapter
from app.parser import normalize_dish_name
from tests.fake_cronometer import FakeCronometerClient
from tests.fixtures import FRIDAY_DISH, SAMPLE_PASTE


@pytest.fixture
def api(tmp_path, monkeypatch):
    """A TestClient whose database is a temp file and whose Cronometer is fake."""
    fake = FakeCronometerClient()
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "api.sqlite3")
    monkeypatch.setattr(main, "get_adapter", lambda: CronometerAdapter(client=fake))
    client = TestClient(main.app)
    client.fake = fake
    return client


def parse(api, text=SAMPLE_PASTE):
    r = api.post("/api/parse", json={"text": text})
    assert r.status_code == 200
    return r.json()


def payload_from(parsed, *, breakfast=True, decisions=None):
    by_date: dict[str, list] = {}
    for row in parsed["rows"]:
        by_date.setdefault(row["date"], []).append(row)
    return {
        "decisions": decisions or {},
        "days": [
            {"date": d, "breakfast": breakfast, "rows": rows}
            for d, rows in sorted(by_date.items())
        ],
    }


# --- the read-only endpoints ----------------------------------------------


def test_healthz_never_touches_cronometer(api):
    assert api.get("/healthz").json() == {"status": "ok"}
    assert api.fake.calls.total() == 0


def test_parse_is_pure(api):
    parse(api)
    assert api.fake.calls.total() == 0, "parsing must not contact Cronometer"


def test_parse_reports_issues_with_line_numbers(api):
    data = parse(api, "2026-08-31 Hétfő\nED1 1 adag Leves: 1095 FT (1095 FT)\n")
    assert data["ok"] is False
    assert data["rows"] == []
    assert data["issues"][0]["line_no"] == 2


def test_config_serves_the_breakfast_constants(api):
    cfg = api.get("/api/config").json()
    assert [f["name"] for f in cfg["breakfast"]] == [f.name for f in BREAKFAST]
    assert cfg["breakfast"][0]["kcal"] == BREAKFAST[0].kcal


# --- resolve ---------------------------------------------------------------


def test_resolve_reads_but_never_writes(api):
    body = payload_from(parse(api))
    data = api.post("/api/resolve", json=body).json()

    assert {d["status"] for d in data["dishes"]} == {"will_create"}
    assert api.fake.calls["create_custom_food"] == 0
    assert api.fake.calls["add_serving"] == 0


def test_resolve_surfaces_a_conflict_before_anything_is_written(api):
    api.fake.add_food(FRIDAY_DISH, kcal=400.0, carbs_g=15.0, protein_g=26.6, fat_g=1.3)
    body = payload_from(parse(api))

    data = api.post("/api/resolve", json=body).json()

    conflict = next(d for d in data["dishes"] if d["status"] == "conflict")
    assert conflict["conflict"]["existing"]["kcal"] == 400.0
    assert conflict["conflict"]["pasted"]["kcal"] == 191.0
    assert conflict["conflict"]["new_version_name"].startswith("[ETK] ")
    assert api.fake.calls["add_serving"] == 0


# --- import ----------------------------------------------------------------


def test_import_writes_and_reports_per_entry(api):
    body = payload_from(parse(api))
    data = api.post("/api/import", json=body).json()

    assert data["failed"] == 0
    assert data["created"] == len(data["entries"])
    assert data["days_written"] == 5
    assert {e["status"] for e in data["entries"]} == {"created"}


def test_importing_twice_writes_once_over_http(api):
    body = payload_from(parse(api))
    first = api.post("/api/import", json=body).json()
    second = api.post("/api/import", json=body).json()

    assert second["created"] == 0
    assert second["skipped"] == first["created"]
    assert len(api.fake.servings) == first["created"]


def test_import_rejects_an_empty_payload(api):
    r = api.post("/api/import", json={"days": []})
    assert r.status_code == 400
    assert api.fake.calls["add_serving"] == 0


def test_history_marks_imported_days(api):
    assert api.get("/api/history").json()["imported_days"] == []

    api.post("/api/import", json=payload_from(parse(api)))

    history = api.get("/api/history").json()
    assert history["imported_days"] == [
        "2026-08-31",
        "2026-09-01",
        "2026-09-02",
        "2026-09-03",
        "2026-09-04",
    ]
    assert history["runs"][0]["created"] > 0


# --- link ------------------------------------------------------------------


def test_link_binds_a_dish_to_a_food_id(api):
    food_id = api.fake.add_food(
        "Valami egészen más néven", kcal=191.0, carbs_g=15.0, protein_g=26.6, fat_g=1.3
    )

    r = api.post("/api/foods/link", json={"name": FRIDAY_DISH, "food_id": food_id})
    assert r.status_code == 200
    assert r.json()["normalized_name"] == normalize_dish_name(FRIDAY_DISH)

    # The linked food is now used instead of creating a new one.
    data = api.post("/api/resolve", json=payload_from(parse(api))).json()
    linked = next(
        d
        for d in data["dishes"]
        if d["normalized_name"] == normalize_dish_name(FRIDAY_DISH)
    )
    assert linked["status"] == "cached"
    assert linked["food"]["food_id"] == food_id


def test_link_with_an_unknown_food_id_fails_without_binding(api):
    r = api.post("/api/foods/link", json={"name": FRIDAY_DISH, "food_id": 12345})
    assert r.status_code == 502
    assert "detail" in r.json()

    with db.connect(db.DB_PATH) as conn:
        assert db.get_cached_food(conn, normalize_dish_name(FRIDAY_DISH)) is None


# --- credential hygiene ----------------------------------------------------


def test_no_endpoint_leaks_credentials(api, monkeypatch):
    monkeypatch.setenv("CRONOMETER_USERNAME", "secret-user@example.com")
    monkeypatch.setenv("CRONOMETER_PASSWORD", "hunter2-should-never-appear")

    bodies = [
        api.get("/healthz").text,
        api.get("/api/config").text,
        api.get("/api/history").text,
        api.post("/api/parse", json={"text": SAMPLE_PASTE}).text,
        api.post("/api/resolve", json=payload_from(parse(api))).text,
        api.post("/api/import", json=payload_from(parse(api))).text,
        api.post("/api/foods/link", json={"name": "x", "food_id": 999}).text,
        api.get("/").text,
        api.get("/static/app.js").text,
    ]
    for body in bodies:
        assert "hunter2-should-never-appear" not in body
        assert "secret-user@example.com" not in body
