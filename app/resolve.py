"""Dish -> Cronometer food resolution.

The ladder, in order, never skipping a step:

  1. Local cache. SQLite keyed on the normalized dish name. Zero API calls.
  2. An existing food on the account. A cache miss does not mean the food does
     not exist -- it may have been made by hand, or the volume may have been
     wiped. The spike (see app/cronometer.py) proved that own foods come back
     from search with `source == "Custom"`, so this step is: search, keep only
     my own foods, exact-match the normalized name, verify the macros, adopt.
  3. Create, only if 1 and 2 both miss.

Nothing is ever adopted without checking its macros first. If a resolved food's
stored nutrition differs from the pasted values by more than the tolerance, the
cache is NOT bound to it and the dish is surfaced as a conflict for the user to
decide. A wrong adopt here poisons every future week silently, which is the
worst failure this app can produce.
"""

from __future__ import annotations

import logging
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import date

from app import db
from app.constants import APP_FOOD_PREFIX, MACRO_ABS_EPSILON, MACRO_TOLERANCE
from app.cronometer import SOURCE_CUSTOM, CronometerAdapter, CronometerUnavailable, Food
from app.parser import fold_accents, normalize_dish_name

logger = logging.getLogger(__name__)

MACRO_KEYS = ("kcal", "carbs_g", "protein_g", "fat_g")


@dataclass
class Macros:
    kcal: float
    carbs_g: float
    protein_g: float
    fat_g: float

    def as_dict(self) -> dict:
        return {k: round(getattr(self, k), 2) for k in MACRO_KEYS}


@dataclass
class Dish:
    """A unique dish to resolve: the display name plus its per-portion macros.

    serving_name/serving_grams describe the serving that carries those macros.
    They matter only when the food has to be created: a delivered dish has no
    published portion weight (so 100 g, and the measure reads "1 adag"), while
    the breakfast items have real weights.
    """

    name: str
    normalized_name: str
    macros: Macros
    serving_name: str = "1 adag"
    serving_grams: float = 100.0


@dataclass
class Resolution:
    """What resolution decided for one dish.

    status:
      cached      -- found in the local cache, nothing asked of Cronometer
      adopted     -- found on the account, macros verified, cache now bound
      will_create -- nothing found; import will create it
      created     -- created during this import
      conflict    -- found on the account but the macros disagree; needs a
                     decision before anything is written
      error       -- Cronometer could not be reached
    """

    dish: Dish
    status: str
    food: Food | None = None
    conflict: dict | None = None
    message: str = ""

    def as_dict(self) -> dict:
        return {
            "normalized_name": self.dish.normalized_name,
            "name": self.dish.name,
            "status": self.status,
            "food": self.food.as_dict() if self.food else None,
            "conflict": self.conflict,
            "message": self.message,
        }


# --- macro comparison ------------------------------------------------------


def macro_delta(existing: Macros, pasted: Macros) -> dict[str, float]:
    """Relative difference per macro, 0.0 when both sides are effectively equal."""
    out: dict[str, float] = {}
    for key in MACRO_KEYS:
        a = float(getattr(existing, key))
        b = float(getattr(pasted, key))
        diff = abs(a - b)
        # An absolute floor keeps rounding noise on tiny values (1.3 g of fat)
        # from tripping the conflict path.
        if diff <= MACRO_ABS_EPSILON:
            out[key] = 0.0
            continue
        denom = max(abs(a), abs(b))
        out[key] = diff / denom if denom else 0.0
    return out


def macros_match(existing: Macros, pasted: Macros) -> bool:
    """True when every macro is within tolerance. Every macro is checked
    independently -- a food that matches on kcal but not on protein is not the
    same food."""
    return all(d <= MACRO_TOLERANCE for d in macro_delta(existing, pasted).values())


def _macros_of(food: Food) -> Macros:
    return Macros(food.kcal, food.carbs_g, food.protein_g, food.fat_g)


def versioned_name(name: str, on: date | None = None) -> str:
    """The name for a new version of a conflicting dish, e.g.
    `[ETK] Húsleves (2026-09)`."""
    on = on or date.today()
    base = name.strip().rstrip("*").strip()
    return f"{APP_FOOD_PREFIX}{base} ({on.year:04d}-{on.month:02d})"


def app_food_name(name: str) -> str:
    """Every food this app creates is prefixed, so app-created entries are
    greppable in the food list and accidental duplicates are obvious."""
    return f"{APP_FOOD_PREFIX}{name.strip().rstrip('*').strip()}"


# --- the ladder ------------------------------------------------------------


def _food_from_cache(row: dict) -> Food:
    return Food(
        food_id=int(row["cronometer_food_id"]),
        name=row["display_name"],
        measure_id=int(row["measure_id"] or 0),
        grams_per_serving=float(row["grams_per_serving"] or 100.0),
        translation_id=int(row["translation_id"] or 0),
        kcal=float(row["kcal"]),
        carbs_g=float(row["carbs_g"]),
        protein_g=float(row["protein_g"]),
        fat_g=float(row["fat_g"]),
        source=SOURCE_CUSTOM,
    )


def find_my_foods(adapter: CronometerAdapter, dish: Dish) -> list[dict]:
    """Search hits that are my own foods and whose name matches this dish.

    Search is ranked-fuzzy -- querying "Fitt májgaluskaleves" also returns
    "Fitt májgaluska leves" -- so the exact normalized name is what decides a
    match, never the ranking.

    The verbatim query is primary. The spike showed that folding accents off
    *narrows* results (3 hits -> 1), because the backend handles diacritics
    perfectly well, so the folded query runs only as a fallback. Names are
    compared NFC-normalized but unfolded: folding both sides risks merging
    genuinely different dishes.
    """

    def exact_mine(hits: list[dict]) -> list[dict]:
        out = []
        for h in hits:
            if h.get("source") != SOURCE_CUSTOM:
                continue
            name = unicodedata.normalize("NFC", str(h.get("name") or ""))
            if normalize_dish_name(name) == dish.normalized_name:
                out.append(h)
        return out

    matches = exact_mine(adapter.search(dish.name))
    if matches:
        return matches

    folded = fold_accents(dish.name)
    if folded != dish.name:
        matches = exact_mine(adapter.search(folded))
    return matches


def resolve_dish(
    conn: sqlite3.Connection,
    adapter: CronometerAdapter,
    dish: Dish,
    *,
    decision: str | None = None,
) -> Resolution:
    """Run the ladder for one dish. Never writes to Cronometer.

    `decision` carries the user's answer to a previously reported conflict:
      "use_existing"        -- adopt the conflicting food anyway and bind it
      "create_new_version"  -- ignore it; import will create a versioned food
    """
    # --- 1. local cache
    cached = db.get_cached_food(conn, dish.normalized_name)
    if cached and decision != "create_new_version":
        return Resolution(dish, "cached", food=_food_from_cache(cached))

    # --- 2. an existing food on the account
    try:
        hits = find_my_foods(adapter, dish)
    except CronometerUnavailable as exc:
        return Resolution(dish, "error", message=str(exc))

    if decision == "create_new_version":
        # The user already saw the conflict and chose a new version. Do not
        # re-examine the existing food.
        return Resolution(dish, "will_create", message="new version requested")

    best: Food | None = None
    for hit in hits:
        try:
            food = adapter.get_food(int(hit["id"]))
        except CronometerUnavailable as exc:
            return Resolution(dish, "error", message=str(exc))

        # `owner` is only present on the detail payload, and is a stronger
        # signal than `source`. When it disagrees with the logged-in user the
        # food is not mine, whatever `source` claimed.
        if food.owner is not None:
            try:
                if food.owner != adapter.user_id():
                    continue
            except CronometerUnavailable:
                pass

        if macros_match(_macros_of(food), dish.macros) or decision == "use_existing":
            db.bind_food(
                conn,
                normalized_name=dish.normalized_name,
                food_id=food.food_id,
                measure_id=food.measure_id,
                grams_per_serving=food.grams_per_serving,
                translation_id=food.translation_id,
                display_name=food.name,
                # Bind the food's own stored macros, not the pasted ones: the
                # cache must describe what Cronometer will actually log.
                kcal=food.kcal,
                carbs_g=food.carbs_g,
                protein_g=food.protein_g,
                fat_g=food.fat_g,
                created_by_app=food.name.startswith(APP_FOOD_PREFIX),
            )
            return Resolution(dish, "adopted", food=food)

        if best is None:
            best = food

    if best is not None:
        # Found on the account, but the numbers disagree. Do not bind, do not
        # log stale numbers -- hand it back for a decision.
        return Resolution(
            dish,
            "conflict",
            food=best,
            conflict={
                "food_id": best.food_id,
                "name": best.name,
                "existing": _macros_of(best).as_dict(),
                "pasted": dish.macros.as_dict(),
                "delta_pct": {
                    k: round(v * 100, 1)
                    for k, v in macro_delta(_macros_of(best), dish.macros).items()
                },
                "tolerance_pct": round(MACRO_TOLERANCE * 100, 1),
                "new_version_name": versioned_name(dish.name),
            },
            message=(
                f"“{best.name}” already exists on your account but its macros "
                f"differ by more than {MACRO_TOLERANCE:.0%}."
            ),
        )

    # --- 3. nothing found
    return Resolution(dish, "will_create")


def ensure_food(
    conn: sqlite3.Connection,
    adapter: CronometerAdapter,
    resolution: Resolution,
    *,
    decision: str | None = None,
) -> Resolution:
    """Turn a `will_create` resolution into a real food. This writes."""
    if resolution.status != "will_create":
        return resolution

    dish = resolution.dish
    name = (
        versioned_name(dish.name)
        if decision == "create_new_version"
        else app_food_name(dish.name)
    )
    try:
        food = adapter.create_food(
            name,
            kcal=dish.macros.kcal,
            carbs_g=dish.macros.carbs_g,
            protein_g=dish.macros.protein_g,
            fat_g=dish.macros.fat_g,
            serving_name=dish.serving_name,
            serving_grams=dish.serving_grams,
        )
    except CronometerUnavailable as exc:
        return Resolution(dish, "error", message=str(exc))

    # Read-back check: the food was fetched fresh after creation, so if what
    # Cronometer stored disagrees with what we sent, we find out now rather
    # than by way of a wrong diary total later.
    if not macros_match(_macros_of(food), dish.macros):
        return Resolution(
            dish,
            "error",
            food=food,
            message=(
                f"Created “{name}” but Cronometer stored different macros than "
                f"were sent (stored {_macros_of(food).as_dict()}, "
                f"sent {dish.macros.as_dict()}). Nothing was logged for it."
            ),
        )

    db.bind_food(
        conn,
        normalized_name=dish.normalized_name,
        food_id=food.food_id,
        measure_id=food.measure_id,
        grams_per_serving=food.grams_per_serving,
        translation_id=food.translation_id,
        display_name=food.name,
        kcal=food.kcal,
        carbs_g=food.carbs_g,
        protein_g=food.protein_g,
        fat_g=food.fat_g,
        created_by_app=True,
    )
    return Resolution(dish, "created", food=food)


def resolve_all(
    conn: sqlite3.Connection,
    adapter: CronometerAdapter,
    dishes: list[Dish],
    *,
    decisions: dict[str, str] | None = None,
) -> dict[str, Resolution]:
    """Resolve every dish, keyed by normalized name. Read-only."""
    decisions = decisions or {}
    out: dict[str, Resolution] = {}
    for dish in dishes:
        if dish.normalized_name in out:
            continue
        out[dish.normalized_name] = resolve_dish(
            conn, adapter, dish, decision=decisions.get(dish.normalized_name)
        )
    return out
