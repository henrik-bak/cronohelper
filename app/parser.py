"""Parser for the Hungarian food-delivery weekly summary.

Pure functions. No I/O, no Cronometer, no side effects.

SPEC
====

The input is UTF-8 text, NFC-normalized before matching (so a decomposed
"e + combining acute" pastes the same as a precomposed "é"). It is processed
line by line; every reported line number is 1-indexed into the original text.

Line kinds, in the order they are tried:

1.  NOISE -- skipped silently.
      - blank or whitespace-only lines
      - separator lines: three or more '-' and nothing else

2.  DAY HEADER -- `YYYY-MM-DD <weekday>`
      e.g. `2026-08-31 Hétfő`
      The weekday name is Hungarian and is *ignored entirely*: not validated,
      not used. The date sets the "current day" for all following item lines.
      An invalid calendar date (e.g. 2026-02-30) is an error.

3.  ITEM LINE -- `<CODE> <N> adag <NAME>: <unit> FT (<total> FT)`
      e.g. `ED10 1 adag Óvári sertésborda (sonka, gomba, sajt), tepsis burgonyával: 1940 FT (1940 FT)`
      - NAME may contain commas, parentheses and colons. The line is anchored
        on the LAST `: <digits> FT (` in the line, not the first colon. This
        falls out of a greedy `.+` for the name.
      - A trailing '*' on NAME is an allergen footnote marker, not part of the
        name, and is stripped.
      - Prices may use spaces as thousand separators (`1 940 FT`); the spaces
        are removed before parsing. Prices are parsed to validate the line
        shape and are carried through for display -- they are never sent to
        Cronometer.
      - N is the portion count. See EXPANSION below.
      - An item line before any day header is an error.

4.  MACRO LINE -- `(<kcal>kcal, <carbs>g szénh., <protein>g fehérje, <fat>g zsír)`
      e.g. `(174kcal, 19.9g szénh., 5.2g fehérje, 6.8g zsír)`
      - Must follow its item line, with only noise lines allowed in between.
      - Numbers may have no decimal part (`15g`). Both '.' and ',' are accepted
        as the decimal separator; the surrounding `g szénh.` / `g fehérje` /
        `g zsír` keywords disambiguate a decimal comma from the field comma.
      - A macro line with no preceding item line is an error.

5.  ANYTHING ELSE -- an error. Nothing is skipped silently: dropping a line we
      do not understand is exactly the failure mode this parser exists to
      prevent.

An item line not followed by a macro line is an error. We never emit a meal
with guessed or missing macros.

PER-PORTION MACROS
==================

The macro line is per portion. `ED1 2 adag Húsleves ... (191kcal, ...)` is
2 x 191 kcal, not 191 total.

EXPANSION
=========

An item line with N portions expands into N separate rows of exactly one
portion each, all carrying the same per-portion macros. They are separate
because a multi-portion line almost always means the extra portions are eaten
on other days -- Friday's `2 adag` soup is realistically one portion Friday and
one Saturday -- and the preview's whole job is letting those rows be moved
independently.

ERRORS
======

Every problem is reported as a ParseIssue carrying the 1-indexed line number,
the offending line verbatim, and what was expected. Parsing does not stop at
the first error; all issues in the paste are collected so they can be fixed in
one pass. If there is at least one issue, the caller must treat the entire
parse as failed -- see parse_text().
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta

# --- line patterns ---------------------------------------------------------

_RE_SEPARATOR = re.compile(r"^-{3,}\s*$")

_RE_DAY_HEADER = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})\s+(\S+)\s*$")

# Greedy `.+` for the name is what anchors on the LAST ': <digits> FT ('.
_PRICE = r"\d[\d  ]*(?:[.,]\d+)?"
_RE_ITEM = re.compile(
    r"^(?P<code>\S+)"
    r"\s+(?P<portions>\d+)"
    r"\s+adag\s+"
    r"(?P<name>.+)"
    r":\s*(?P<unit_price>" + _PRICE + r")\s*FT"
    r"\s*\(\s*(?P<total_price>" + _PRICE + r")\s*FT\s*\)\s*$",
    re.IGNORECASE,
)

_NUM = r"\d+(?:[.,]\d+)?"
_RE_MACROS = re.compile(
    r"^\(\s*"
    r"(?P<kcal>" + _NUM + r")\s*kcal\s*,\s*"
    r"(?P<carbs>" + _NUM + r")\s*g\s*szénh\.?\s*,\s*"
    r"(?P<protein>" + _NUM + r")\s*g\s*fehérje\s*,\s*"
    r"(?P<fat>" + _NUM + r")\s*g\s*zsír\s*"
    r"\)\s*$",
    re.IGNORECASE,
)

# Leading delivery code (ED1, ED10, ...) defensively stripped during
# normalization: the cache is keyed on the dish, and the same dish reappears
# under different codes from week to week.
_RE_LEADING_CODE = re.compile(r"^ED\s*\d+\s+", re.IGNORECASE)
_RE_APP_PREFIX = re.compile(r"^\[ETK\]\s*", re.IGNORECASE)
# The month suffix this app appends when the macros of an existing food
# conflict with a new week's numbers, e.g. "[ETK] Húsleves (2026-09)". Stripped
# during normalization so a versioned food is still recognised as the same dish
# if the cache is ever lost and resolution has to fall back to search.
_RE_VERSION_SUFFIX = re.compile(r"\s*\(\d{4}-\d{2}\)$")


# --- data ------------------------------------------------------------------


@dataclass(frozen=True)
class ParseIssue:
    line_no: int
    line: str
    expected: str

    def as_dict(self) -> dict:
        return {"line_no": self.line_no, "line": self.line, "expected": self.expected}


@dataclass
class Row:
    """One portion of one dish on one day. The unit the preview moves around."""

    id: str
    date: str
    code: str
    name: str
    normalized_name: str
    kcal: float
    carbs_g: float
    protein_g: float
    fat_g: float
    unit_price: float
    source_line_no: int

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "date": self.date,
            "code": self.code,
            "name": self.name,
            "normalized_name": self.normalized_name,
            "kcal": self.kcal,
            "carbs_g": self.carbs_g,
            "protein_g": self.protein_g,
            "fat_g": self.fat_g,
            "unit_price": self.unit_price,
            "source_line_no": self.source_line_no,
        }


@dataclass
class ParseResult:
    days: list[str] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)
    issues: list[ParseIssue] = field(default_factory=list)
    suggested_extra_days: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "days": self.days,
            "rows": [r.as_dict() for r in self.rows],
            "issues": [i.as_dict() for i in self.issues],
            "suggested_extra_days": self.suggested_extra_days,
        }


# --- helpers ---------------------------------------------------------------


def normalize_dish_name(name: str) -> str:
    """Cache key for a dish: lowercased, whitespace-collapsed, code- and
    marker-free.

    Keyed on the name rather than the delivery code on purpose -- the same dish
    comes back under a different ED<n> code in a later week. The [ETK] prefix
    this app adds is stripped too, so a hand-made `Húsleves` and an
    app-created `[ETK] Húsleves` normalize to the same key.

    Accents are preserved: comparison happens on unfolded NFC text. Folding
    both sides of a comparison risks merging genuinely different dishes.
    """
    s = unicodedata.normalize("NFC", name).strip()
    s = _RE_APP_PREFIX.sub("", s)
    s = _RE_LEADING_CODE.sub("", s)
    s = s.rstrip("*").strip()
    s = _RE_VERSION_SUFFIX.sub("", s)
    s = re.sub(r"\s+", " ", s)
    return s.casefold()


def fold_accents(text: str) -> str:
    """Húsleves -> Husleves. For building *search queries* only.

    The mobile API's handling of Hungarian diacritics is unknown, so we search
    with a folded query to maximise recall, then compare the returned names
    NFC-normalized but unfolded to decide what actually matched.
    """
    return "".join(
        c
        for c in unicodedata.normalize("NFD", text)
        if not unicodedata.combining(c)
    )


def _num(raw: str) -> float:
    """Parse a number that may use ',' as a decimal separator and spaces
    (including non-breaking spaces) as thousand separators."""
    return float(raw.replace(" ", "").replace(" ", "").replace(",", "."))


def _strip_marker(name: str) -> str:
    """Drop the trailing allergen-footnote '*' and surrounding whitespace."""
    return unicodedata.normalize("NFC", name).strip().rstrip("*").strip()


# --- the parser ------------------------------------------------------------


def parse_text(text: str) -> ParseResult:
    """Parse a pasted weekly summary into one row per portion.

    Never raises on malformed input: problems are collected into
    result.issues. When issues is non-empty the result carries no rows -- a
    half-parsed week rendered as a preview is indistinguishable from a correct
    one, and that is precisely how a meal goes missing.
    """
    text = unicodedata.normalize("NFC", text)
    lines = text.splitlines()

    issues: list[ParseIssue] = []
    rows: list[Row] = []
    days: list[str] = []

    current_day: str | None = None
    # The item line still awaiting its macro line, if any.
    pending: dict | None = None
    next_row_id = 1

    def flush_pending_without_macros() -> None:
        """An item line reached the end of its window with no macro line."""
        nonlocal pending
        if pending is not None:
            issues.append(
                ParseIssue(
                    pending["line_no"],
                    pending["line"],
                    "a macro line `(<kcal>kcal, <carbs>g szénh., <protein>g fehérje, "
                    "<fat>g zsír)` immediately after this item line",
                )
            )
            pending = None

    for idx, raw_line in enumerate(lines):
        line_no = idx + 1
        line = raw_line.strip()

        # 1. noise
        if not line or _RE_SEPARATOR.match(line):
            continue

        # 4. macro line -- checked before the day header/item so it can never be
        #    mistaken for anything else.
        m_macro = _RE_MACROS.match(line)
        if m_macro:
            if pending is None:
                issues.append(
                    ParseIssue(
                        line_no,
                        raw_line,
                        "an item line `<CODE> <N> adag <NAME>: <price> FT (<total> FT)` "
                        "before this macro line",
                    )
                )
                continue

            kcal = _num(m_macro.group("kcal"))
            carbs = _num(m_macro.group("carbs"))
            protein = _num(m_macro.group("protein"))
            fat = _num(m_macro.group("fat"))

            # EXPANSION: N portions -> N independently movable rows of 1
            # portion each, every one carrying the full per-portion macros.
            for _ in range(pending["portions"]):
                rows.append(
                    Row(
                        id=f"r{next_row_id}",
                        date=pending["day"],
                        code=pending["code"],
                        name=pending["name"],
                        normalized_name=normalize_dish_name(pending["name"]),
                        kcal=kcal,
                        carbs_g=carbs,
                        protein_g=protein,
                        fat_g=fat,
                        unit_price=pending["unit_price"],
                        source_line_no=pending["line_no"],
                    )
                )
                next_row_id += 1
            pending = None
            continue

        # 2. day header
        m_day = _RE_DAY_HEADER.match(line)
        if m_day:
            flush_pending_without_macros()
            y, mo, d = (int(m_day.group(i)) for i in (1, 2, 3))
            try:
                day = date(y, mo, d)
            except ValueError as exc:
                issues.append(
                    ParseIssue(line_no, raw_line, f"a real calendar date ({exc})")
                )
                current_day = None
                continue
            current_day = day.isoformat()
            if current_day not in days:
                days.append(current_day)
            continue

        # 3. item line
        m_item = _RE_ITEM.match(line)
        if m_item:
            flush_pending_without_macros()
            if current_day is None:
                issues.append(
                    ParseIssue(
                        line_no,
                        raw_line,
                        "a day header `YYYY-MM-DD <weekday>` before this item line",
                    )
                )
                continue
            portions = int(m_item.group("portions"))
            if portions < 1:
                issues.append(
                    ParseIssue(line_no, raw_line, "a portion count of at least 1")
                )
                continue
            pending = {
                "line_no": line_no,
                "line": raw_line,
                "day": current_day,
                "code": m_item.group("code"),
                "name": _strip_marker(m_item.group("name")),
                "portions": portions,
                "unit_price": _num(m_item.group("unit_price")),
            }
            continue

        # 5. anything else
        issues.append(
            ParseIssue(
                line_no,
                raw_line,
                "a day header, an item line, a macro line, or a separator",
            )
        )

    flush_pending_without_macros()

    # Report in source order. Without this the end-of-input flush appends its
    # issue last even though its line comes earlier in the paste.
    issues.sort(key=lambda i: i.line_no)

    result = ParseResult(days=days, rows=rows, issues=issues)
    if not result.ok:
        # Refuse to hand back a partial week -- see the module docstring.
        result.rows = []
        result.days = []
        return result

    result.suggested_extra_days = _suggest_extra_days(days)
    return result


def _suggest_extra_days(days: list[str], count: int = 2) -> list[str]:
    """The two days after the pasted range, offered empty and ready to receive
    a dropped portion. Moving a portion onto Saturday is a first-class action,
    not an edge case."""
    if not days:
        return []
    last = max(date.fromisoformat(d) for d in days)
    out = []
    for i in range(1, count + 1):
        nxt = (last + timedelta(days=i)).isoformat()
        if nxt not in days:
            out.append(nxt)
    return out
