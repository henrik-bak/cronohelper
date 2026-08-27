"""Parser tests. No network, no Cronometer, no I/O -- parse_text is pure."""

from __future__ import annotations

import unicodedata

import pytest

from app.parser import fold_accents, normalize_dish_name, parse_text
from tests.fixtures import (
    FRIDAY,
    FRIDAY_DISH,
    FRIDAY_MACROS,
    MULTI_COMMA_DISH,
    SAMPLE_DAYS,
    SAMPLE_PASTE,
)


# --- the full sample -------------------------------------------------------


def test_sample_parses_without_issues():
    result = parse_text(SAMPLE_PASTE)
    assert result.issues == [], [i.as_dict() for i in result.issues]
    assert result.ok


def test_sample_days_in_order():
    result = parse_text(SAMPLE_PASTE)
    assert result.days == SAMPLE_DAYS


def test_sample_row_count_counts_portions_not_lines():
    # 7 item lines, one of which is `2 adag` -> 8 one-portion rows.
    result = parse_text(SAMPLE_PASTE)
    assert len(result.rows) == 8


def test_every_row_is_exactly_one_portion():
    # There is no portion field on a Row: a Row *is* one portion. This test
    # pins the invariant that nothing downstream has to multiply by anything.
    result = parse_text(SAMPLE_PASTE)
    friday = [r for r in result.rows if r.date == FRIDAY]
    assert len(friday) == 2
    assert all(r.kcal == FRIDAY_MACROS["kcal"] for r in friday)


# --- the hard line: commas, parentheses, and the price anchor ---------------


def test_multi_comma_dish_name_kept_whole():
    result = parse_text(SAMPLE_PASTE)
    names = [r.name for r in result.rows]
    assert MULTI_COMMA_DISH in names


def test_multi_comma_dish_macros():
    result = parse_text(SAMPLE_PASTE)
    row = next(r for r in result.rows if r.name == MULTI_COMMA_DISH)
    assert (row.kcal, row.carbs_g, row.protein_g, row.fat_g) == (697.0, 62.4, 45.2, 24.7)
    assert row.code == "ED10"
    assert row.date == "2026-08-31"


def test_anchors_on_last_colon_price_not_the_first_colon():
    # A dish name that itself contains ': <digits> FT'-ish text. A parser that
    # anchored on the first colon would truncate the name here.
    text = (
        "2026-09-07 Hétfő\n"
        "ED2 1 adag Menü: leves, főétel: 1 250 FT (1 250 FT)\n"
        "(400kcal, 30g szénh., 20g fehérje, 12g zsír)\n"
    )
    result = parse_text(text)
    assert result.ok, [i.as_dict() for i in result.issues]
    assert result.rows[0].name == "Menü: leves, főétel"
    assert result.rows[0].unit_price == 1250.0


# --- portion expansion -----------------------------------------------------


def test_two_adag_expands_to_two_movable_rows():
    result = parse_text(SAMPLE_PASTE)
    friday = [r for r in result.rows if r.name == FRIDAY_DISH]

    assert len(friday) == 2, "a `2 adag` line must expand into two separate rows"
    assert friday[0].id != friday[1].id, "rows must be independently addressable"
    assert all(r.date == FRIDAY for r in friday)
    for r in friday:
        assert r.kcal == FRIDAY_MACROS["kcal"]
        assert r.carbs_g == FRIDAY_MACROS["carbs_g"]
        assert r.protein_g == FRIDAY_MACROS["protein_g"]
        assert r.fat_g == FRIDAY_MACROS["fat_g"]


def test_macros_are_per_portion_so_the_day_doubles():
    # 2 adag x 191 kcal = 382 kcal on Friday, not 191.
    result = parse_text(SAMPLE_PASTE)
    friday_kcal = sum(r.kcal for r in result.rows if r.date == FRIDAY)
    assert friday_kcal == 382.0


def test_expanded_rows_share_the_source_line():
    result = parse_text(SAMPLE_PASTE)
    friday = [r for r in result.rows if r.name == FRIDAY_DISH]
    assert friday[0].source_line_no == friday[1].source_line_no


# --- number and marker handling --------------------------------------------


def test_trailing_allergen_star_stripped():
    result = parse_text(SAMPLE_PASTE)
    names = [r.name for r in result.rows]
    assert "Könnyű tejfölös zöldbableves" in names
    assert not any(n.endswith("*") for n in names)


def test_integer_macro_without_decimal_part():
    # `15g szénh.` -- no decimal point.
    result = parse_text(SAMPLE_PASTE)
    row = next(r for r in result.rows if r.name == FRIDAY_DISH)
    assert row.carbs_g == 15.0


def test_price_with_space_thousand_separator():
    result = parse_text(SAMPLE_PASTE)
    row = next(r for r in result.rows if r.code == "ED3")
    assert row.unit_price == 1640.0


def test_comma_decimal_separator_in_macros():
    # Hungarian decimal comma, disambiguated from the field comma by the
    # trailing `g szénh.` / `g fehérje` keywords.
    text = (
        "2026-09-07 Hétfő\n"
        "ED1 1 adag Leves: 100 FT (100 FT)\n"
        "(174kcal, 19,9g szénh., 5,2g fehérje, 6,8g zsír)\n"
    )
    result = parse_text(text)
    assert result.ok, [i.as_dict() for i in result.issues]
    r = result.rows[0]
    assert (r.carbs_g, r.protein_g, r.fat_g) == (19.9, 5.2, 6.8)


def test_separator_and_blank_lines_are_noise():
    text = (
        "2026-09-07 Hétfő\n"
        "\n"
        "ED1 1 adag Leves: 100 FT (100 FT)\n"
        "\n"
        "(100kcal, 1g szénh., 2g fehérje, 3g zsír)\n"
        "-------------\n"
        "\n"
    )
    result = parse_text(text)
    assert result.ok, [i.as_dict() for i in result.issues]
    assert len(result.rows) == 1


# --- failing loudly --------------------------------------------------------


def test_item_without_macro_line_is_an_error_not_a_dropped_meal():
    text = (
        "2026-08-31 Hétfő\n"
        "ED1 1 adag Leves: 1095 FT (1095 FT)\n"
        "ED2 1 adag Főétel: 1940 FT (1940 FT)\n"
        "(697kcal, 62.4g szénh., 45.2g fehérje, 24.7g zsír)\n"
    )
    result = parse_text(text)
    assert not result.ok
    assert len(result.issues) == 1
    issue = result.issues[0]
    assert issue.line_no == 2
    assert "Leves" in issue.line
    assert "macro line" in issue.expected


def test_trailing_item_without_macro_line_is_caught_at_eof():
    text = "2026-08-31 Hétfő\nED1 1 adag Leves: 1095 FT (1095 FT)\n"
    result = parse_text(text)
    assert not result.ok
    assert result.issues[0].line_no == 2


def test_issues_suppress_all_rows():
    # A half-parsed week rendered as a preview looks exactly like a correct
    # one. Refuse to hand any rows back.
    text = "2026-08-31 Hétfő\nED1 1 adag Leves: 1095 FT (1095 FT)\n"
    result = parse_text(text)
    assert result.rows == []
    assert result.days == []


def test_item_before_any_day_header_is_an_error():
    text = (
        "ED1 1 adag Leves: 1095 FT (1095 FT)\n"
        "(174kcal, 19.9g szénh., 5.2g fehérje, 6.8g zsír)\n"
    )
    result = parse_text(text)
    assert not result.ok
    assert result.issues[0].line_no == 1
    assert "day header" in result.issues[0].expected


def test_macro_line_without_an_item_is_an_error():
    text = "2026-08-31 Hétfő\n(174kcal, 19.9g szénh., 5.2g fehérje, 6.8g zsír)\n"
    result = parse_text(text)
    assert not result.ok
    assert result.issues[0].line_no == 2
    assert "item line" in result.issues[0].expected


def test_unrecognised_line_is_an_error():
    text = (
        "2026-08-31 Hétfő\n"
        "ED1 1 adag Leves: 1095 FT (1095 FT)\n"
        "(174kcal, 19.9g szénh., 5.2g fehérje, 6.8g zsír)\n"
        "Összesen: 3035 FT\n"
    )
    result = parse_text(text)
    assert not result.ok
    assert result.issues[0].line_no == 4
    assert "Összesen" in result.issues[0].line


def test_invalid_calendar_date_is_an_error():
    result = parse_text("2026-02-30 Hétfő\n")
    assert not result.ok
    assert "calendar date" in result.issues[0].expected


def test_all_issues_collected_not_just_the_first():
    text = "garbage one\ngarbage two\ngarbage three\n"
    result = parse_text(text)
    assert [i.line_no for i in result.issues] == [1, 2, 3]


def test_issues_are_reported_in_source_order():
    # The dangling item on line 2 is only detected at end of input, after the
    # junk on line 3 was already flagged. It must still be reported first.
    text = (
        "2026-08-31 Hétfő\n"
        "ED1 1 adag Leves: 1095 FT (1095 FT)\n"
        "Összesen: 3035 FT\n"
    )
    result = parse_text(text)
    assert [i.line_no for i in result.issues] == [2, 3]


# --- the extra days the grid offers ----------------------------------------


def test_suggests_the_two_days_after_the_pasted_range():
    # Friday 2026-09-04 -> Saturday and Sunday, empty and ready for a drop.
    result = parse_text(SAMPLE_PASTE)
    assert result.suggested_extra_days == ["2026-09-05", "2026-09-06"]


# --- normalization ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Húsleves sovány pulykamellből", "húsleves sovány pulykamellből"),
        ("  Húsleves   sovány  pulykamellből ", "húsleves sovány pulykamellből"),
        ("Könnyű tejfölös zöldbableves*", "könnyű tejfölös zöldbableves"),
        ("ED1 Húsleves", "húsleves"),
        ("[ETK] Húsleves", "húsleves"),
        ("[ETK] ED10 Húsleves*", "húsleves"),
        ("HÚSLEVES", "húsleves"),
    ],
)
def test_normalize_dish_name(raw, expected):
    assert normalize_dish_name(raw) == expected


def test_handmade_and_app_created_normalize_to_the_same_key():
    assert normalize_dish_name("Húsleves") == normalize_dish_name("[ETK] Húsleves")


def test_normalization_preserves_accents():
    # Folding both sides of a comparison risks merging genuinely different
    # dishes, so the cache key keeps its diacritics.
    assert normalize_dish_name("Húsleves") != normalize_dish_name("Husleves")


def test_fold_accents_is_for_queries_only():
    assert fold_accents("Húsleves sovány pulykamellből") == "Husleves sovany pulykamellbol"
    assert fold_accents("Óvári sertésborda") == "Ovari sertesborda"


def test_decomposed_input_normalizes_like_precomposed():
    # A paste carrying NFD text (e.g. copied out of macOS) must parse
    # identically to NFC text -- including the accented keywords `szénh.`,
    # `fehérje` and `zsír` that the macro regex itself matches on.
    nfc = (
        "2026-08-31 Hétfő\n"
        "ED1 1 adag Húsleves: 100 FT (100 FT)\n"
        "(100kcal, 1g szénh., 2g fehérje, 3g zsír)\n"
    )
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfd != nfc, "fixture must really be decomposed or this test proves nothing"

    result = parse_text(nfd)
    assert result.ok, [i.as_dict() for i in result.issues]
    assert result.rows[0].name == "Húsleves"
    assert result.rows[0].normalized_name == normalize_dish_name("Húsleves")
    assert result.as_dict() == parse_text(nfc).as_dict()
