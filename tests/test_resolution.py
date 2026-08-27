"""Food resolution: the three-step ladder and the macro-conflict path.

The ladder must never skip a step, and must never bind the cache to a food
whose macros disagree with the pasted numbers -- a wrong adopt poisons every
future week silently.
"""

from __future__ import annotations

import pytest

from app import db
from app.constants import APP_FOOD_PREFIX
from app.parser import normalize_dish_name
from app.resolve import (
    Dish,
    Macros,
    ensure_food,
    macros_match,
    resolve_dish,
    versioned_name,
)

SOUP = "Húsleves sovány pulykamellből"
SOUP_MACROS = Macros(kcal=191.0, carbs_g=15.0, protein_g=26.6, fat_g=1.3)


def dish(name: str = SOUP, macros: Macros | None = None) -> Dish:
    return Dish(
        name=name,
        normalized_name=normalize_dish_name(name),
        macros=macros or SOUP_MACROS,
    )


# --- step 1: cache hit -----------------------------------------------------


def test_cache_hit_asks_cronometer_nothing(conn, adapter, fake):
    db.bind_food(
        conn,
        normalized_name=normalize_dish_name(SOUP),
        food_id=42,
        measure_id=7,
        grams_per_serving=100.0,
        translation_id=3,
        display_name=f"{APP_FOOD_PREFIX}{SOUP}",
        kcal=191.0,
        carbs_g=15.0,
        protein_g=26.6,
        fat_g=1.3,
        created_by_app=True,
    )

    res = resolve_dish(conn, adapter, dish())

    assert res.status == "cached"
    assert res.food.food_id == 42
    assert res.food.measure_id == 7
    assert fake.calls["search_food"] == 0, "a cache hit must not hit the API"
    assert fake.calls["get_food"] == 0


# --- step 2: cache miss, food already on the account -----------------------


def test_cache_miss_adopts_existing_food_instead_of_creating(conn, adapter, fake):
    food_id = fake.add_food(
        SOUP, kcal=191.0, carbs_g=15.0, protein_g=26.6, fat_g=1.3, source="Custom"
    )

    res = resolve_dish(conn, adapter, dish())

    assert res.status == "adopted"
    assert res.food.food_id == food_id
    assert fake.calls["create_custom_food"] == 0, "must adopt, never create"

    cached = db.get_cached_food(conn, normalize_dish_name(SOUP))
    assert cached is not None, "adopting must bind the cache"
    assert cached["cronometer_food_id"] == food_id


def test_adopts_a_hand_made_food_with_no_app_prefix(conn, adapter, fake):
    # Matching happens on the unprefixed normalized name, so a hand-made
    # "Húsleves" and an app-made "[ETK] Húsleves" are the same dish.
    food_id = fake.add_food(
        SOUP, kcal=191.0, carbs_g=15.0, protein_g=26.6, fat_g=1.3
    )
    res = resolve_dish(conn, adapter, dish())
    assert res.status == "adopted"
    assert res.food.food_id == food_id
    assert not res.food.name.startswith(APP_FOOD_PREFIX)


def test_app_prefixed_food_is_recognised_as_the_same_dish(conn, adapter, fake):
    food_id = fake.add_food(
        f"{APP_FOOD_PREFIX}{SOUP}", kcal=191.0, carbs_g=15.0, protein_g=26.6, fat_g=1.3
    )
    res = resolve_dish(conn, adapter, dish())
    assert res.status == "adopted"
    assert res.food.food_id == food_id


def test_never_adopts_a_public_database_food(conn, adapter, fake):
    # Same name, same macros, but source is a public database -- not mine.
    fake.add_food(
        SOUP, kcal=191.0, carbs_g=15.0, protein_g=26.6, fat_g=1.3, source="NCCDB"
    )
    res = resolve_dish(conn, adapter, dish())
    assert res.status == "will_create"


def test_never_adopts_a_food_owned_by_someone_else(conn, adapter, fake):
    # `source` says Custom but `owner` disagrees with the logged-in user.
    fake.add_food(
        SOUP,
        kcal=191.0,
        carbs_g=15.0,
        protein_g=26.6,
        fat_g=1.3,
        source="Custom",
        owner=999999,
    )
    res = resolve_dish(conn, adapter, dish())
    assert res.status == "will_create"


def test_ranked_fuzzy_near_miss_is_not_adopted(conn, adapter, fake):
    # The spike's real failure mode: searching "Fitt májgaluskaleves" also
    # returns "Fitt májgaluska leves". The decoy is inserted FIRST so it ranks
    # first -- resolution must match on the name, not on the ranking.
    decoy = fake.add_food(
        "Húsleves sovány pulyka mellből",  # note the extra space
        kcal=191.0,
        carbs_g=15.0,
        protein_g=26.6,
        fat_g=1.3,
    )
    real = fake.add_food(
        SOUP, kcal=191.0, carbs_g=15.0, protein_g=26.6, fat_g=1.3
    )

    res = resolve_dish(conn, adapter, dish())

    assert res.status == "adopted"
    assert res.food.food_id == real
    assert res.food.food_id != decoy


def test_adopting_scales_macros_from_a_non_100g_serving(conn, adapter, fake):
    # Nutrients are stored per 100 g; a 250 g serving must read back as the
    # per-serving numbers, not the per-100g ones.
    fake.add_food(
        SOUP,
        kcal=191.0,
        carbs_g=15.0,
        protein_g=26.6,
        fat_g=1.3,
        serving_grams=250.0,
    )
    res = resolve_dish(conn, adapter, dish())
    assert res.status == "adopted"
    assert res.food.grams_per_serving == 250.0
    assert res.food.kcal == pytest.approx(191.0, rel=1e-6)


# --- step 3: cache miss, nothing on the account ----------------------------


def test_cache_miss_with_nothing_creates(conn, adapter, fake):
    res = resolve_dish(conn, adapter, dish())
    assert res.status == "will_create"
    assert fake.calls["create_custom_food"] == 0, "resolve must not write"

    created = ensure_food(conn, adapter, res)

    assert created.status == "created"
    assert fake.calls["create_custom_food"] == 1
    assert created.food.name == f"{APP_FOOD_PREFIX}{SOUP}"

    cached = db.get_cached_food(conn, normalize_dish_name(SOUP))
    assert cached["cronometer_food_id"] == created.food.food_id
    assert cached["created_by_app"] == 1


def test_created_food_keeps_the_stated_kcal(conn, adapter, fake):
    # 19.9*4 + 5.2*4 + 6.8*9 = 161, but the site says 174. The site's number
    # is what must land, or the daily total drifts by several percent.
    d = dish("Könnyű tejfölös zöldbableves", Macros(174.0, 19.9, 5.2, 6.8))
    created = ensure_food(conn, adapter, resolve_dish(conn, adapter, d))
    assert created.status == "created"
    assert created.food.kcal == pytest.approx(174.0, rel=1e-6)


def test_created_food_has_a_real_measure_id_despite_upstream_returning_none(
    conn, adapter, fake
):
    # create_custom_food hardcodes measure_id: None. The adapter must read the
    # food back rather than trusting it, or nothing can be logged.
    created = ensure_food(conn, adapter, resolve_dish(conn, adapter, dish()))
    assert created.food.measure_id != 0
    assert created.food.measure_id is not None


def test_a_created_food_is_adopted_on_the_next_week(conn, adapter, fake):
    ensure_food(conn, adapter, resolve_dish(conn, adapter, dish()))
    # Simulate the volume being wiped: cache gone, food still on the account.
    conn.execute("DELETE FROM food_cache")

    res = resolve_dish(conn, adapter, dish())

    assert res.status == "adopted"
    assert fake.calls["create_custom_food"] == 1, "must not create a second time"


# --- the macro-conflict path ----------------------------------------------


def test_macro_conflict_is_surfaced_and_not_bound(conn, adapter, fake):
    # Same dish name, protein off by ~19% -- well beyond the 2% tolerance.
    fake.add_food(SOUP, kcal=191.0, carbs_g=15.0, protein_g=22.0, fat_g=1.3)

    res = resolve_dish(conn, adapter, dish())

    assert res.status == "conflict"
    assert res.conflict["existing"]["protein_g"] == pytest.approx(22.0)
    assert res.conflict["pasted"]["protein_g"] == pytest.approx(26.6)
    assert res.conflict["delta_pct"]["protein_g"] > 2.0
    assert db.get_cached_food(conn, normalize_dish_name(SOUP)) is None, (
        "a conflicting food must never be bound to the cache"
    )
    assert fake.calls["create_custom_food"] == 0, "and must not silently create"


def test_conflict_resolved_by_use_existing_binds_the_existing_food(conn, adapter, fake):
    food_id = fake.add_food(SOUP, kcal=191.0, carbs_g=15.0, protein_g=22.0, fat_g=1.3)

    res = resolve_dish(conn, adapter, dish(), decision="use_existing")

    assert res.status == "adopted"
    assert res.food.food_id == food_id
    cached = db.get_cached_food(conn, normalize_dish_name(SOUP))
    # The cache records what Cronometer will actually log, not what was pasted.
    assert cached["protein_g"] == pytest.approx(22.0)


def test_conflict_resolved_by_new_version_creates_a_suffixed_food(conn, adapter, fake):
    fake.add_food(SOUP, kcal=191.0, carbs_g=15.0, protein_g=22.0, fat_g=1.3)

    res = resolve_dish(conn, adapter, dish(), decision="create_new_version")
    assert res.status == "will_create"

    created = ensure_food(conn, adapter, res, decision="create_new_version")

    assert created.status == "created"
    assert created.food.name == versioned_name(SOUP)
    assert created.food.name.startswith(APP_FOOD_PREFIX)
    assert created.food.protein_g == pytest.approx(26.6), "the pasted macros win"


def test_versioned_food_is_still_the_same_dish_after_a_cache_wipe(conn, adapter, fake):
    # The (YYYY-MM) suffix is stripped during normalization, so search can
    # still find a versioned food if the cache is ever lost.
    fake.add_food(
        versioned_name(SOUP), kcal=191.0, carbs_g=15.0, protein_g=26.6, fat_g=1.3
    )
    res = resolve_dish(conn, adapter, dish())
    assert res.status == "adopted"


def test_tolerance_boundary(conn, adapter, fake):
    # 191 -> 194 is 1.55%, inside the 2% tolerance: adopt.
    fake.add_food(SOUP, kcal=194.0, carbs_g=15.0, protein_g=26.6, fat_g=1.3)
    assert resolve_dish(conn, adapter, dish()).status == "adopted"


def test_tolerance_boundary_just_outside(conn, adapter, fake):
    # 191 -> 200 is 4.5%: conflict.
    fake.add_food(SOUP, kcal=200.0, carbs_g=15.0, protein_g=26.6, fat_g=1.3)
    assert resolve_dish(conn, adapter, dish()).status == "conflict"


def test_small_absolute_differences_do_not_trip_the_conflict_path(conn, adapter, fake):
    # 1.3 g -> 1.5 g of fat is 13% relative but 0.2 g absolute: rounding noise,
    # not a different food.
    fake.add_food(SOUP, kcal=191.0, carbs_g=15.0, protein_g=26.6, fat_g=1.5)
    assert resolve_dish(conn, adapter, dish()).status == "adopted"


def test_every_macro_is_checked_independently():
    # Matching on energy alone is not enough to call it the same food.
    same_kcal_different_split = Macros(191.0, 30.0, 12.0, 1.3)
    assert not macros_match(same_kcal_different_split, SOUP_MACROS)


# --- normalization at the resolution boundary ------------------------------


def test_same_dish_under_a_different_delivery_code_resolves_to_one_food(
    conn, adapter, fake
):
    # The cache is keyed on the name, not the ED<n> code, because the same dish
    # reappears under a different code in a later week.
    first = ensure_food(conn, adapter, resolve_dish(conn, adapter, dish()))
    again = resolve_dish(conn, adapter, dish())
    assert again.status == "cached"
    assert again.food.food_id == first.food.food_id
    assert fake.calls["create_custom_food"] == 1
