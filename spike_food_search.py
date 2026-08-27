"""Food-resolution spike.

Answers the three questions that decide how step 2 of dish resolution
("is this food already on my account?") gets implemented:

  Q1. Does a custom food I created by hand in the Cronometer UI come back
      from search_food() at all?
  Q2. Is there a source / owner / isCustom field that identifies it as mine
      (as opposed to a CRDB/NCCDB/FDC database entry)?
  Q3. Is the match exact, or ranked-fuzzy with near-misses mixed in?

Run:
    uv run python spike_food_search.py "Teszt Halaszle Spike"

Credentials come from CRONOMETER_USERNAME / CRONOMETER_PASSWORD in the
environment (or .env). Nothing is written to Cronometer by this script --
it only reads. Credentials are never printed.
"""

from __future__ import annotations

import json
import os
import sys
import unicodedata

from dotenv import load_dotenv

from cronometer_api_mcp.client import CronometerClient, CronometerError

load_dotenv()

# A query guaranteed to hit the public food database, so we can see what a
# known-not-mine result looks like and diff its fields against the custom one.
CONTROL_QUERY = "banana"


def fold(text: str) -> str:
    """Accent-fold: Húsleves -> Husleves. Used only for the query, never for
    comparing returned names (folding both sides risks merging real dishes)."""
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c)
    )


def dump(label: str, obj: object) -> None:
    print(f"\n===== {label} =====")
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def summarize(foods: list[dict], needle: str) -> None:
    """Print the field shape of results and flag likely matches."""
    if not foods:
        print("  (no results)")
        return
    keys: set[str] = set()
    for f in foods:
        keys.update(f.keys())
    print(f"  result count: {len(foods)}")
    print(f"  union of keys across results: {sorted(keys)}")

    # Any field that might carry ownership information.
    interesting = [
        k
        for k in sorted(keys)
        if any(
            t in k.lower()
            for t in ("source", "owner", "custom", "user", "private", "mine", "type")
        )
    ]
    print(f"  ownership-ish fields present: {interesting or 'NONE'}")

    print(f"  distinct values per ownership-ish field:")
    for k in interesting:
        vals = {json.dumps(f.get(k), ensure_ascii=False, default=str) for f in foods}
        print(f"    {k}: {sorted(vals)[:12]}")

    print("  first 15 results (rank order):")
    needle_nfc = unicodedata.normalize("NFC", needle).casefold()
    for i, f in enumerate(foods[:15]):
        name = unicodedata.normalize("NFC", str(f.get("name", "")))
        hit = "  <-- EXACT NAME MATCH" if name.casefold() == needle_nfc else ""
        extras = " ".join(f"{k}={f.get(k)!r}" for k in interesting)
        print(f"    [{i}] id={f.get('id')} {name!r} {extras}{hit}")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        print("ERROR: pass the exact name of the custom food you created by hand.")
        return 2
    needle = sys.argv[1]

    if not os.getenv("CRONOMETER_USERNAME") or not os.getenv("CRONOMETER_PASSWORD"):
        print("ERROR: set CRONOMETER_USERNAME and CRONOMETER_PASSWORD first.")
        return 2

    client = CronometerClient()
    try:
        client.login()
    except CronometerError as exc:
        print(f"ERROR: login failed: {exc}")
        return 1
    print(f"Logged in. account timezone = {client._timezone!r}")

    # --- Q1/Q2/Q3: search for the hand-made custom food, verbatim -----------
    print(f"\n######## SEARCH (verbatim): {needle!r} ########")
    try:
        verbatim = client.search_food(needle)
    except Exception as exc:  # noqa: BLE001 - spike: we want the raw failure
        print(f"  search raised: {type(exc).__name__}: {exc}")
        verbatim = []
    summarize(verbatim, needle)
    dump("RAW RESPONSE (verbatim query, first 5)", verbatim[:5])

    # --- Accent handling: does the folded query find it too? ---------------
    folded = fold(needle)
    if folded != needle:
        print(f"\n######## SEARCH (accent-folded): {folded!r} ########")
        try:
            folded_res = client.search_food(folded)
        except Exception as exc:  # noqa: BLE001
            print(f"  search raised: {type(exc).__name__}: {exc}")
            folded_res = []
        summarize(folded_res, needle)
        ids_v = [f.get("id") for f in verbatim]
        ids_f = [f.get("id") for f in folded_res]
        print(f"  verbatim ids == folded ids? {ids_v == ids_f}")
        print(f"  target present in folded results? "
              f"{any(unicodedata.normalize('NFC', str(f.get('name',''))).casefold() == unicodedata.normalize('NFC', needle).casefold() for f in folded_res)}")
    else:
        print("\n(name has no accents to fold -- skipping folded query)")

    # --- Control: a food that is definitely NOT mine ----------------------
    print(f"\n######## CONTROL SEARCH (public DB): {CONTROL_QUERY!r} ########")
    try:
        control = client.search_food(CONTROL_QUERY)
    except Exception as exc:  # noqa: BLE001
        print(f"  search raised: {type(exc).__name__}: {exc}")
        control = []
    summarize(control, CONTROL_QUERY)
    dump("RAW RESPONSE (control query, first 3)", control[:3])

    # --- get_food on the exact match, to see the full stored payload -------
    needle_nfc = unicodedata.normalize("NFC", needle).casefold()
    exact = [
        f
        for f in verbatim
        if unicodedata.normalize("NFC", str(f.get("name", ""))).casefold() == needle_nfc
    ]
    if exact:
        fid = exact[0].get("id")
        print(f"\n######## GET_FOOD on exact match id={fid} ########")
        try:
            full = client.get_food(int(fid))
            dump("RAW get_food RESPONSE", full)
        except Exception as exc:  # noqa: BLE001
            print(f"  get_food raised: {type(exc).__name__}: {exc}")
    else:
        print("\n(no exact-name match in search results -- get_food skipped)")

    # --- Verdict ----------------------------------------------------------
    print("\n\n######## VERDICT ########")
    found = bool(exact)
    print(f"Q1 searchable at all?      {'YES' if found else 'NO'}")
    if found:
        f0 = exact[0]
        ident = {
            k: f0.get(k)
            for k in f0
            if any(
                t in k.lower()
                for t in ("source", "owner", "custom", "user", "private", "type")
            )
        }
        print(f"Q2 ownership fields:       {ident or 'NONE FOUND'}")
        ctrl_sources = {str(f.get("source")) for f in control}
        print(f"   control (public) sources: {sorted(ctrl_sources)[:10]}")
        print(f"   -> distinguishable?      "
              f"{'YES' if ident.get('source') and str(ident.get('source')) not in ctrl_sources else 'INCONCLUSIVE - compare above'}")
        print(f"Q3 rank of exact match:    index {verbatim.index(f0)} of {len(verbatim)}")
        print(f"   -> {'exact-only' if len(verbatim) == 1 else 'ranked-fuzzy, near-misses mixed in'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
