"""The only module that talks to Cronometer.

Everything upstream-specific is behind this adapter: if a Cronometer release
breaks the reverse-engineered mobile API, or `cronometer-api-mcp` changes its
method names, this is the one file to fix.

What the food-resolution spike established (2026-08-27, client 0.2.1), because
the code below depends on all of it:

  * Foods you created yourself come back from `search_food` with
    `source == "Custom"`. Every public database uses a different source string
    (CFCD, CNF, CRDB, CoFID, FDCBranded, IFCDB, NCCDB, NUTTAB, USDA), so the
    field cleanly separates "mine" from "the database".
  * `get_food` additionally returns `owner`, the numeric user id. When present
    it is checked against the logged-in user -- a stronger signal than `source`.
  * Search is ranked-fuzzy, not exact: querying "Fitt májgaluskaleves" also
    returned two "Fitt májgaluska leves" entries. Never trust results[0];
    match on the normalized name.
  * Querying with accents folded off *reduces* recall (3 results -> 1). The
    backend handles diacritics fine, so the verbatim query is primary and the
    folded one is only a fallback.
  * `create_custom_food` always returns `measure_id: None` -- it is hardcoded
    upstream. The real measure id has to be read back with `get_food`.
  * Nutrients are stored per 100 g; a measure's `value` is its gram weight.

Credentials: read from the environment by the upstream client, used only to
obtain a session token, never logged, never persisted by us, never returned to
the browser. The upstream client logs the account email and a token prefix at
INFO, so its logger is turned down to WARNING below.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx
from cronometer_api_mcp.client import NUTRIENT_IDS, CronometerClient, CronometerError

from app.db import SESSION_PATH

logger = logging.getLogger(__name__)

# The upstream client logs "Logging in to Cronometer as <email>" and
# "login successful (... token=abcd1234...)" at INFO. Neither belongs in our
# logs, and we cannot reach in and edit them, so the whole logger is raised to
# WARNING. Real failures still surface.
logging.getLogger("cronometer_api_mcp.client").setLevel(logging.WARNING)

# The `source` value Cronometer puts on foods owned by the account.
SOURCE_CUSTOM = "Custom"

# Cronometer stores every food's nutrients per 100 g.
GRAMS_BASIS = 100.0


class CronometerUnavailable(Exception):
    """Cronometer could not be reached, or answered in a shape we don't accept.

    Raised instead of letting an upstream exception or a stack trace escape:
    the message is safe to show in the UI and never contains credentials.
    """


@dataclass(frozen=True)
class Food:
    """A Cronometer food, with its macros expressed per serving.

    `grams_per_serving` is the gram weight of `measure_id`. Logging one serving
    means calling add_serving with exactly these two values.
    """

    food_id: int
    name: str
    measure_id: int
    grams_per_serving: float
    translation_id: int
    kcal: float
    carbs_g: float
    protein_g: float
    fat_g: float
    source: str
    owner: int | None = None

    @property
    def is_mine(self) -> bool:
        return self.source == SOURCE_CUSTOM

    def as_dict(self) -> dict:
        return {
            "food_id": self.food_id,
            "name": self.name,
            "kcal": self.kcal,
            "carbs_g": self.carbs_g,
            "protein_g": self.protein_g,
            "fat_g": self.fat_g,
            "source": self.source,
        }


def _nutrient_map(food: dict) -> dict[int, float]:
    out: dict[int, float] = {}
    for n in food.get("nutrients") or []:
        try:
            out[int(n["id"])] = float(n["amount"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _default_measure(food: dict) -> tuple[int, float]:
    """Return (measure_id, grams) for the food's default measure.

    Falls back to the first measure, then to a 100 g pseudo-measure, so a food
    with an unexpected shape still produces something loggable rather than
    raising deep inside a loop.
    """
    measures = food.get("measures") or []
    default_id = food.get("defaultMeasureId")

    chosen = None
    for m in measures:
        if default_id is not None and m.get("id") == default_id:
            chosen = m
            break
    if chosen is None and measures:
        chosen = measures[0]
    if chosen is None:
        return int(default_id or 0), GRAMS_BASIS

    try:
        grams = float(chosen.get("value") or GRAMS_BASIS)
    except (TypeError, ValueError):
        grams = GRAMS_BASIS
    # A measure with amount != 1 describes N units; we only ever log one.
    return int(chosen.get("id") or 0), grams or GRAMS_BASIS


def _food_from_detail(detail: dict) -> Food:
    """Build a Food from a get_food payload, converting per-100g nutrients into
    per-serving values."""
    nutrients = _nutrient_map(detail)
    measure_id, grams = _default_measure(detail)
    scale = grams / GRAMS_BASIS

    translation_id = 0
    for t in detail.get("translations") or []:
        if t.get("translationId"):
            translation_id = int(t["translationId"])
            break

    owner = detail.get("owner")
    return Food(
        food_id=int(detail["id"]),
        name=str(detail.get("name") or ""),
        measure_id=measure_id,
        grams_per_serving=grams,
        translation_id=translation_id,
        kcal=nutrients.get(NUTRIENT_IDS["energy"], 0.0) * scale,
        carbs_g=nutrients.get(NUTRIENT_IDS["carbs"], 0.0) * scale,
        protein_g=nutrients.get(NUTRIENT_IDS["protein"], 0.0) * scale,
        fat_g=nutrients.get(NUTRIENT_IDS["fat"], 0.0) * scale,
        source=str(detail.get("source") or ""),
        owner=int(owner) if isinstance(owner, int) else None,
    )


class CronometerAdapter:
    """Thin, intentionally small surface over the upstream client."""

    def __init__(self, client: CronometerClient | None = None) -> None:
        if client is None:
            # Park the session token on the mounted volume. Cronometer rate
            # limits login aggressively, so the token must outlive the
            # container; the client re-authenticates only on 401/403.
            SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
            client = CronometerClient(session_path=SESSION_PATH)
        self._client = client

    # --- error translation ------------------------------------------------

    @staticmethod
    def _wrap(op: str, exc: Exception) -> CronometerUnavailable:
        """Turn any upstream failure into one clear, credential-free message."""
        if isinstance(exc, httpx.HTTPStatusError):
            code = exc.response.status_code
            if code in (401, 403):
                msg = (
                    "Cronometer rejected the credentials. Check "
                    "CRONOMETER_USERNAME / CRONOMETER_PASSWORD."
                )
            elif code == 429:
                msg = (
                    "Cronometer is rate-limiting this account. Wait a few "
                    "minutes before retrying — do not retry in a loop."
                )
            else:
                msg = f"Cronometer returned HTTP {code}."
        elif isinstance(exc, httpx.HTTPError):
            msg = "Could not reach Cronometer (network error)."
        elif isinstance(exc, CronometerError):
            msg = f"Cronometer API error: {exc}"
        elif isinstance(exc, (KeyError, TypeError, ValueError)):
            msg = (
                "Cronometer's response was not in the expected shape — the "
                "mobile API has probably changed. See the recovery section of "
                "the README; the fix is confined to app/cronometer.py."
            )
        else:
            msg = f"Unexpected failure talking to Cronometer: {type(exc).__name__}"
        logger.error("cronometer %s failed: %s", op, type(exc).__name__)
        return CronometerUnavailable(f"{msg} (while trying to {op})")

    # --- reads ------------------------------------------------------------

    def user_id(self) -> int:
        try:
            return self._client.user_id
        except Exception as exc:  # noqa: BLE001
            raise self._wrap("authenticate", exc) from None

    def search(self, query: str) -> list[dict]:
        """Raw search hits. Ranked-fuzzy: callers must match names themselves."""
        try:
            return self._client.search_food(query)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(f"search for {query!r}", exc) from None

    def get_food(self, food_id: int) -> Food:
        try:
            detail = self._client.get_food(int(food_id))
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(f"fetch food {food_id}", exc) from None
        try:
            return _food_from_detail(detail)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(f"read food {food_id}", exc) from None

    # --- writes -----------------------------------------------------------

    def create_food(
        self,
        name: str,
        *,
        kcal: float,
        carbs_g: float,
        protein_g: float,
        fat_g: float,
        serving_name: str = "1 adag",
        serving_grams: float = GRAMS_BASIS,
    ) -> Food:
        """Create a custom food where one serving == the given macros.

        Energy is passed explicitly rather than left to be recomputed from the
        macros by a 4/4/9 rule -- the delivery site's stated kcal does not
        reconcile with its own macros (174 listed vs 161 computed on the first
        sample dish), and its number is the one we want.

        serving_grams is the real weight of one serving where it is known (the
        breakfast items), and 100 for delivered dishes whose portion weight is
        not published. It only affects how the measure reads in Cronometer's
        UI: we always log exactly one serving, so the macros are the listed
        ones either way.

        The upstream call hardcodes `measure_id: None` in its return value, so
        the created food is read back to obtain the real measure id.
        """
        try:
            created = self._client.create_custom_food(
                name,
                calories=kcal,
                protein_g=protein_g,
                fat_g=fat_g,
                carbs_g=carbs_g,
                serving_name=serving_name,
                serving_grams=serving_grams,
            )
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(f"create food {name!r}", exc) from None

        food_id = created.get("food_id") or created.get("id")
        if not food_id:
            raise CronometerUnavailable(
                f"Cronometer accepted the food {name!r} but returned no id."
            )
        # Read back: gets the measure id, and confirms what was actually stored
        # rather than assuming the write landed as sent.
        return self.get_food(int(food_id))

    def add_entry(
        self, *, food: Food, day: date, diary_group: int
    ) -> str | None:
        """Log exactly one serving of `food` on `day`. Returns the entry id."""
        try:
            result = self._client.add_serving(
                food_id=food.food_id,
                measure_id=food.measure_id,
                grams=food.grams_per_serving,
                translation_id=food.translation_id,
                day=day,
                diary_group=diary_group,
            )
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(
                f"log {food.name!r} on {day.isoformat()}", exc
            ) from None
        entry_id = result.get("id") if isinstance(result, dict) else None
        return str(entry_id) if entry_id is not None else None
