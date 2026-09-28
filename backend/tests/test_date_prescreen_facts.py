"""Ground facts for the date pre-screen in clean/detect.py (Phase 14 Step 13).

The mixed-date reading tries up to 16 TRY_STRPTIME formats per value. A value that parses by any
of them opens with whitespace, a sign, a digit or a month name -- measured here, including the
surprise that strptime skips a leading tab, which trim() leaves in place.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.clean import detect  # noqa: E402

FORMATS = detect._DAY_FIRST + detect._MONTH_FIRST + detect._DATE_FORMATS
PARSED = "COALESCE(" + ", ".join(f"TRY_STRPTIME(trim(v), '{f}')" for f in FORMATS) + ")"
SCREEN = f"regexp_matches(trim(v), '{detect.DATE_PREFIX}')"


def _ask(values, expr):
    return duckdb.connect().execute(f"SELECT {expr} FROM unnest(?) t(v)", [values]).fetchall()


def test_strptime_skips_a_leading_tab_that_trim_keeps():
    assert _ask(["\t2024-01-05"], f"{PARSED} IS NOT NULL") == [(True,)]
    assert _ask(["\t2024-01-05"], "trim(v)") == [("\t2024-01-05",)]


def test_no_parsed_value_is_screened_out():
    rng = random.Random(2)
    alpha = "0123456789-/., :TJanFebMrApyulgSOctNovDecjJAN+%x\t\n\r\x0b\x0c\u00a0\u2003_#"
    bases = ["2024-01-05", "05/01/2024", "Jan 05, 2024", "5 January 2024", "20240105",
             "2024-01-05 10:11:12", "05-Jan-2024", "12.31.24", "", "2024/01/05"]
    values = []
    for _ in range(60_000):
        s = list(rng.choice(bases))
        for _ in range(rng.randint(0, 4)):
            op, pos = rng.random(), rng.randint(0, len(s))
            if op < .45:
                s.insert(pos, rng.choice(alpha))
            elif op < .7 and s:
                s.pop(min(pos, len(s) - 1))
            elif s:
                s[min(pos, len(s) - 1)] = rng.choice(alpha)
        values.append("".join(s))
    con = duckdb.connect()
    parsed, lost = con.execute(
        f"SELECT count(*) FILTER (WHERE {PARSED} IS NOT NULL), "
        f"count(*) FILTER (WHERE {PARSED} IS NOT NULL AND NOT {SCREEN}) "
        f"FROM unnest(?) t(v)", [values]).fetchone()
    assert parsed > 10_000 and lost == 0


def test_ids_and_labels_are_screened_out():
    got = _ask(["TXN000123", "r0001", "Food", "x2024-01-05", "Mon 5 Jan 2024"], SCREEN)
    assert got == [(False,)] * 5
