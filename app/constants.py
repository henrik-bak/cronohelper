"""Fixed values that define what this app writes. One edit here changes the app.

Everything in this module is deliberately data, not behaviour: the breakfast
block, the meal-group numbers, and the naming/tolerance policy for foods this
app creates.
"""

from __future__ import annotations

from dataclasses import dataclass

# Cronometer diary group numbers (from the upstream client's add_serving docs:
# 0 = auto by time of day, 1 = Breakfast, 2 = Lunch, 3 = Dinner, 4 = Snacks).
# We never use 0 -- "auto" would stamp meals into whichever group matches the
# wall clock at import time, which is not the group they belong to.
DIARY_GROUP_BREAKFAST = 1
DIARY_GROUP_LUNCH = 2

# Every food this app creates is prefixed with this. It does not help us find
# hand-made foods (those have no prefix -- we match on the unprefixed
# normalized name), but it makes app-created entries greppable in the food
# list, makes accidental duplicates obvious, and gives a clean bulk-delete path.
APP_FOOD_PREFIX = "[ETK] "

# A resolved food whose stored nutrition differs from the pasted values by more
# than this fraction is NOT adopted silently -- it is surfaced as a conflict in
# the preview. A wrong adopt poisons every future week, so the check is strict
# and applies to every macro independently.
MACRO_TOLERANCE = 0.02

# Absolute floor for the tolerance check, so tiny values (e.g. 1.3 g fat) do not
# trip the conflict path on rounding noise alone. A macro passes if it is within
# MACRO_TOLERANCE relative OR within this absolute amount.
MACRO_ABS_EPSILON = 0.5


@dataclass(frozen=True)
class BreakfastFood:
    """One of the two fixed breakfast entries.

    grams is the serving weight registered with the custom food. Because we
    define 1 serving == the listed macros, the numbers below are per serving,
    and a serving is what gets logged.
    """

    name: str
    serving_name: str
    grams: float
    kcal: float
    carbs_g: float
    protein_g: float
    fat_g: float


# The fixed breakfast, added to every day column in the preview, on by default
# and independently toggleable per day -- with or without lunch on that day.
# Written as two separate diary entries in the Breakfast group.
BREAKFAST: tuple[BreakfastFood, ...] = (
    BreakfastFood(
        name="Optimum Nutrition Gold Standard 100% Whey Protein, Chocolate",
        serving_name="1 scoop",
        grams=31.0,
        kcal=120.0,
        carbs_g=2.0,
        protein_g=24.0,
        fat_g=3.0,
    ),
    BreakfastFood(
        name="Cerbona Zab-Pehely Fine Oat Flakes",
        serving_name="40 g",
        grams=40.0,
        kcal=150.0,
        carbs_g=24.0,
        protein_g=5.0,
        fat_g=3.0,
    ),
)
