"""
The caveats a person writes into a contract, counted against the table where a caveat is a count.

A caveat is free text and the engine stores it as written. Every analysis then prints it beside
the figures it measured, and a model carries it into its answer -- so a caveat's number reads as a
measurement although nothing measured it. Seen on the retail fixture (25/09/2026): the contract
said 'unknown' customer_age in 3,470 rows and blank customer_state in 3,470 rows, pasted from the
generator's notes; the table holds 3,471 and 3,473, the profiler said so, and the answer repeated
3,470 twice under "data quality".

Only claims of four shapes are counted, each against one column the table has, and only when the
caveat carries exactly one row count ("in 3,470 rows", "120 exact duplicate rows"):

- exact duplicate rows: rows beyond the first of each identical set, as the profile counts them;
- a quoted value in a column: rows where the column equals it exactly;
- a blank, empty, missing or null column: rows where it is NULL or only whitespace;
- a comparison with a number (``units = -1``, ``units < 0``): rows where it holds.

Anything else is reported as not checked rather than guessed at: "13 lines have delivery_date
before order date" names two columns and a relation this module does not parse, and a wrong
"agrees" would be worse than no check. What was counted is always said in words, so a check that
covered part of a caveat ("units = -1 and negative revenue/cost") shows which part.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.engine.clean import sql

#: The unit a row count is written in. A number followed by one of these, up to three words
#: later, is the caveat's claimed count ("120 exact duplicate rows", "in 3,470 rows").
_COUNT = re.compile(
    r"(?<![\w.,\-])(\d{1,3}(?:,\d{3})+|\d+)(?![\w.,]\d)"
    r"(?=(?:\s+[A-Za-z_'-]+){0,3}?\s+(?:rows?|lines?|records?|values?|entries|cases)\b)",
    re.IGNORECASE)
_QUOTED = re.compile(r"'([^']*)'|\"([^\"]*)\"|‘([^’]*)’|“([^”]*)”")
_BLANK = re.compile(r"\b(blank|empty|missing|null|nulls)\b", re.IGNORECASE)
_DUPLICATE = re.compile(r"\bduplicat", re.IGNORECASE)
_OPS = {"=": "=", "==": "=", "<": "<", ">": ">", "<=": "<=", ">=": ">=", "!=": "<>", "<>": "<>"}


@dataclass(frozen=True)
class CaveatCheck:
    caveat: str
    #: "agrees", "differs" or "unchecked"
    status: str
    claimed: int | None = None
    measured: int | None = None
    #: What was counted, in words; for "unchecked", why nothing was.
    what: str = ""

    def to_text(self) -> str:
        if self.status == "differs":
            return (f'CAVEAT DIFFERS FROM THE DATA: "{self.caveat}" says {self.claimed:,}; the '
                    f"table holds {self.measured:,} ({self.what}). Correct the caveat before "
                    f"confirming -- every result prints it, and it reads as a measurement.")
        if self.status == "agrees":
            return f'Caveat checked: "{self.caveat}" -- {self.what}: {self.measured:,}.'
        return f'Caveat not checked ({self.what}): "{self.caveat}"'


def _scalar(con, query: str) -> int:
    return int(con.execute(query).fetchone()[0] or 0)


def _columns_named(text: str, columns: list[str]) -> list[str]:
    """Columns the caveat names as whole words, longest first so 'order_ts' beats 'order'."""
    found = []
    for c in sorted(columns, key=len, reverse=True):
        if re.search(rf"(?<![\w]){re.escape(c)}(?![\w])", text) and not any(
                c in f for f in found):
            found.append(c)
    return found


def check_caveat(con, table: str, caveat: str, columns: list[str],
                 numeric: set[str]) -> CaveatCheck:
    counts = [int(m.group(1).replace(",", "")) for m in _COUNT.finditer(caveat)]
    if len(counts) != 1:
        return CaveatCheck(caveat, "unchecked", what=(
            "no row count in it" if not counts else "more than one row count in it"))
    claimed = counts[0]
    t = sql.ident(table)

    if _DUPLICATE.search(caveat):
        measured = _scalar(con, f"SELECT count(*) - (SELECT count(*) FROM "
                                f"(SELECT DISTINCT * FROM {t})) FROM {t}")
        return _verdict(caveat, claimed, measured,
                        "rows that exactly repeat an earlier row, all columns equal")

    named = _columns_named(caveat, columns)
    if len(named) != 1:
        return CaveatCheck(caveat, "unchecked", claimed=claimed, what=(
            "it names no column of the table" if not named
            else f"it names {len(named)} columns ({', '.join(named)})"))
    column = named[0]
    c = sql.ident(column)

    op = re.search(rf"(?<![\w]){re.escape(column)}\s*(==|<=|>=|!=|<>|=|<|>)\s*(-?\d+(?:\.\d+)?)"
                   rf"(?![\w.])", caveat)
    if op and column in numeric:
        sign, value = _OPS[op.group(1)], op.group(2)
        measured = _scalar(con, f"SELECT count(*) FROM {t} WHERE {c} {sign} {value}")
        return _verdict(caveat, claimed, measured, f"rows where {column} {sign} {value}")

    quoted = [next(g for g in m.groups() if g is not None) for m in _QUOTED.finditer(caveat)]
    quoted = [q for q in quoted if q not in columns]
    if len(quoted) == 1:
        token = quoted[0]
        measured = _scalar(con, f"SELECT count(*) FROM {t} "
                                f"WHERE CAST({c} AS VARCHAR) = {sql.literal(token)}")
        if measured == 0:
            # Most often the loader read the token as NULL ('-', 'NA'): the claim may be right
            # about the file and unanswerable from the table. Unchecked, never "differs".
            return CaveatCheck(caveat, "unchecked", claimed=claimed, what=(
                f"no {token!r} is left in {column}; a load that reads it as NULL removes it"))
        return _verdict(caveat, claimed, measured, f"rows where {column} is exactly {token!r}")

    if not quoted and _BLANK.search(caveat):
        measured = _scalar(con, f"SELECT count(*) FROM {t} WHERE {c} IS NULL "
                                f"OR trim(CAST({c} AS VARCHAR)) = ''")
        return _verdict(caveat, claimed, measured, f"rows where {column} is NULL or blank")

    return CaveatCheck(caveat, "unchecked", claimed=claimed,
                       what="not a claim of a shape the engine counts")


def _verdict(caveat: str, claimed: int, measured: int, what: str) -> CaveatCheck:
    return CaveatCheck(caveat, "agrees" if claimed == measured else "differs",
                       claimed=claimed, measured=measured, what=what)


def check_caveats(con, table: str, caveats: list[str]) -> list[CaveatCheck]:
    if not caveats:
        return []
    rows = con.execute(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = ? "
        "ORDER BY ordinal_position", [table]).fetchall()
    columns = [r[0] for r in rows]
    numeric = {r[0] for r in rows if any(
        k in r[1].upper() for k in ("INT", "DOUBLE", "DECIMAL", "FLOAT", "REAL", "NUMERIC"))}
    return [check_caveat(con, table, cav, columns, numeric) for cav in caveats]


def notes(checks: list[CaveatCheck]) -> list[str]:
    """Differences first -- they are the reason this module exists -- then agreements; the
    unchecked are one line, not one each."""
    out = [k.to_text() for k in checks if k.status == "differs"]
    out += [k.to_text() for k in checks if k.status == "agrees"]
    unchecked = [k for k in checks if k.status == "unchecked"]
    if unchecked:
        out.append(f"{len(unchecked)} caveat(s) are not a count the engine can check; they are "
                   f"carried as the person's statement, not as a measurement: "
                   + "; ".join(f'"{k.caveat}"' for k in unchecked))
    return out


__all__ = ["CaveatCheck", "check_caveat", "check_caveats", "notes"]
