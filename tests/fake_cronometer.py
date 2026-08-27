"""An in-memory stand-in for CronometerClient. The tests never touch the real API.

The payload shapes here are copied from the real responses captured during the
food-resolution spike, because resolution depends on their details:

  * search hits carry `source` ("Custom" for my own foods, "CRDB"/"NCCDB"/...
    for the public databases), `id`, `name`, `measureId`, `translationId`.
  * `get_food` returns `owner` (the numeric user id), nutrients stored
    **per 100 g** keyed by nutrient id, and `measures[].value` as the gram
    weight of the measure.
  * `create_custom_food` returns `measure_id: None` -- always, it is hardcoded
    upstream -- so callers must read the food back to learn its measure id.
  * search is ranked-fuzzy and returns near-misses, so this fake returns
    everything whose name shares a word with the query, in insertion order.
    Tests insert decoys first to prove resolution never trusts results[0].
"""

from __future__ import annotations

from collections import Counter

from cronometer_api_mcp.client import NUTRIENT_IDS

from app.parser import fold_accents

DEFAULT_USER_ID = 10485391


def _norm(text: str) -> str:
    return fold_accents(str(text)).casefold()


class FakeCronometerClient:
    def __init__(self, user_id: int = DEFAULT_USER_ID) -> None:
        self._user_id = user_id
        self.foods: dict[int, dict] = {}
        self.servings: list[dict] = []
        self.calls: Counter[str] = Counter()
        self._next_food_id = 53_600_000
        self._next_measure_id = 175_700_000
        self._next_translation_id = 60_300_000
        self._next_serving_id = 900_000

    # --- what the adapter uses -------------------------------------------

    @property
    def user_id(self) -> int:
        return self._user_id

    def search_food(self, query: str) -> list[dict]:
        self.calls["search_food"] += 1
        words = [w for w in _norm(query).split() if w]
        hits = []
        for food in self.foods.values():
            name = _norm(food["name"])
            if any(w in name for w in words):
                measure = food["measures"][0]
                hits.append(
                    {
                        "id": food["id"],
                        "name": food["name"],
                        "source": food["source"],
                        "measureId": measure["id"],
                        "measureDisplayName": measure["name"],
                        "translationId": food["translations"][0]["translationId"],
                        "score": 1000,
                        "src": 6 if food["source"] == "Custom" else 4,
                        "category": 0,
                        "language": "en",
                        "globalPopularity": 0,
                        "userPopularity": 1,
                    }
                )
        return hits

    def get_food(self, food_id: int) -> dict:
        self.calls["get_food"] += 1
        if int(food_id) not in self.foods:
            raise KeyError(f"no such food: {food_id}")
        return self.foods[int(food_id)]

    def create_custom_food(
        self,
        name: str,
        *,
        calories: float,
        protein_g: float,
        fat_g: float,
        carbs_g: float,
        fiber_g: float = 0,
        sugar_g: float = 0,
        sodium_mg: float = 0,
        saturated_fat_g: float = 0,
        serving_name: str = "1 serving",
        serving_grams: float = 100.0,
    ) -> dict:
        self.calls["create_custom_food"] += 1
        food_id = self.add_food(
            name,
            kcal=calories,
            carbs_g=carbs_g,
            protein_g=protein_g,
            fat_g=fat_g,
            source="Custom",
            owner=self._user_id,
            serving_name=serving_name,
            serving_grams=serving_grams,
        )
        # Upstream hardcodes measure_id to None here. Reproduce that exactly:
        # code that trusts it would break against the real API.
        return {"food_id": food_id, "measure_id": None}

    def add_serving(
        self,
        food_id: int,
        measure_id: int | None,
        grams: float,
        translation_id: int = 0,
        day=None,
        diary_group: int = 0,
    ) -> dict:
        self.calls["add_serving"] += 1
        self._next_serving_id += 1
        entry = {
            "id": self._next_serving_id,
            "foodId": food_id,
            "measureId": measure_id,
            "grams": grams,
            "translationId": translation_id,
            "day": day.isoformat() if day is not None else None,
            "diaryGroup": diary_group,
        }
        self.servings.append(entry)
        return entry

    # --- test helpers -----------------------------------------------------

    def add_food(
        self,
        name: str,
        *,
        kcal: float,
        carbs_g: float,
        protein_g: float,
        fat_g: float,
        source: str = "Custom",
        owner: int | None = None,
        serving_name: str = "1 Serving",
        serving_grams: float = 100.0,
    ) -> int:
        """Put a food on the fake account. Macros are per serving; they are
        stored per 100 g, exactly as Cronometer does."""
        self._next_food_id += 1
        self._next_measure_id += 1
        self._next_translation_id += 1
        food_id = self._next_food_id
        scale = 100.0 / serving_grams if serving_grams else 1.0

        self.foods[food_id] = {
            "id": food_id,
            "name": name,
            "source": source,
            "owner": owner if owner is not None else self._user_id,
            "category": 0,
            "labelType": "EU",
            "retired": False,
            "defaultMeasureId": self._next_measure_id,
            "measures": [
                {
                    "id": self._next_measure_id,
                    "name": serving_name,
                    "value": serving_grams,
                    "amount": 1,
                    "type": "Atomic",
                    "hidden": False,
                }
            ],
            "translations": [
                {
                    "translationId": self._next_translation_id,
                    "name": name,
                    "languageCode": "en",
                }
            ],
            "nutrients": [
                {"id": NUTRIENT_IDS["energy"], "amount": kcal * scale},
                {"id": NUTRIENT_IDS["carbs"], "amount": carbs_g * scale},
                {"id": NUTRIENT_IDS["protein"], "amount": protein_g * scale},
                {"id": NUTRIENT_IDS["fat"], "amount": fat_g * scale},
            ],
            "properties": {},
            "foodTags": [],
        }
        return food_id

    def servings_on(self, day: str, diary_group: int | None = None) -> list[dict]:
        return [
            s
            for s in self.servings
            if s["day"] == day
            and (diary_group is None or s["diaryGroup"] == diary_group)
        ]

    def name_of(self, food_id: int) -> str:
        return self.foods[int(food_id)]["name"]
