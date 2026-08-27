"""Shared test data.

SAMPLE_PASTE is the primary fixture: the sample from the spec, verbatim. The
spec elided Wednesday and Thursday with `...`; those two days are filled in
here with realistic entries so the fixture is a complete Mon-Fri week. Every
line that appeared in the spec is byte-for-byte unchanged.
"""

# --- the primary fixture ---------------------------------------------------

SAMPLE_PASTE = """2026-08-31 Hétfő
ED1 1 adag Könnyű tejfölös zöldbableves*: 1095 FT (1095 FT)
(174kcal, 19.9g szénh., 5.2g fehérje, 6.8g zsír)
ED10 1 adag Óvári sertésborda (sonka, gomba, sajt), tepsis burgonyával, bébirépával: 1940 FT (1940 FT)
(697kcal, 62.4g szénh., 45.2g fehérje, 24.7g zsír)
--------------------------------------------------------------
2026-09-01 Kedd
ED1 1 adag Marhahúsleves zöldségekkel gazdagon: 1395 FT (1395 FT)
(193kcal, 10.6g szénh., 21.2g fehérje, 6.4g zsír)
--------------------------------------------------------------
2026-09-02 Szerda
ED3 1 adag Csirkemell rizzsel, párolt zöldségekkel: 1 640 FT (1 640 FT)
(612kcal, 71.2g szénh., 38.9g fehérje, 18.4g zsír)
--------------------------------------------------------------
2026-09-03 Csütörtök
ED1 1 adag Gulyásleves*: 1295 FT (1295 FT)
(286kcal, 22.4g szénh., 18.1g fehérje, 13.5g zsír)
ED7 1 adag Rakott krumpli (tojás, kolbász, tejföl): 1790 FT (1790 FT)
(534kcal, 41.3g szénh., 22.7g fehérje, 30.2g zsír)
--------------------------------------------------------------
2026-09-04 Péntek
ED1 2 adag Húsleves sovány pulykamellből: 1355 FT (2710 FT)
(191kcal, 15g szénh., 26.6g fehérje, 1.3g zsír)
"""

# Dates present in SAMPLE_PASTE, in order.
SAMPLE_DAYS = [
    "2026-08-31",
    "2026-09-01",
    "2026-09-02",
    "2026-09-03",
    "2026-09-04",
]

# The dish whose name contains commas, parentheses -- and which is followed by
# the ': <price> FT' the parser must anchor on (the LAST one, not the first
# colon, which does not exist here, but the commas alone break a naive split).
MULTI_COMMA_DISH = (
    "Óvári sertésborda (sonka, gomba, sajt), tepsis burgonyával, bébirépával"
)

FRIDAY = "2026-09-04"
FRIDAY_DISH = "Húsleves sovány pulykamellből"
FRIDAY_MACROS = {"kcal": 191.0, "carbs_g": 15.0, "protein_g": 26.6, "fat_g": 1.3}
