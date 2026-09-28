"""
How each measure should combine, suggested with a reason -- never applied (25/09/2026).

`Measure.agg` has no default, on purpose: a guessed `sum` gets confirmed without being read. That
stays. What this adds is the engine's reading beside the blank, with the evidence that produced it,
so a person answers with the reason in front of them -- and answers only where the evidence is
weak. Frontier tools get their accuracy from a semantic layer somebody approves (Cortex Analyst,
Genie, Pulse); Power BI's Copilot summed temperatures because its default was Sum. So: the engine
suggests, a person confirms.

Three strengths, and what each means to a caller:

- **strong** -- a measurement agrees with the name, or a measurement alone settles it: the column
  equals another column divided by a third on 98% of rows; it is constant within every order; its
  name says price and nothing in its values says otherwise. The web form's "use the engine's
  suggestions" fills these.
- **likely** -- the name alone. Shown with a plain question; never filled in.
- **unsure** -- nothing to go on. Only the question.

Rules, each with an id so an eval can score it (scripts/suggest_bench.py):

  P1  per-unit     constant within every value of a repeating identifier -> per=[that id]
  R1  ratio        = a/b, a/b*100, (a-b)/a, (a-b)/a*100 of two other measures on >= 98% of rows
  N1  code         a number named like a code or a calendar part (zip, year) -> none
  F1  flag         only 0 and 1 (or true/false) -> mean, the share of rows that are 1; and, for a
                   flag that is not a measure, a `<flag>_rate` helper measure reading it
  G1  coordinate   named like a latitude or longitude, values in range -> none: a location
  G2  distance     named like a distance: travelled -> sum (mean is the typical trip); a gap
                   between a recorded and an expected point (gps, drift, error) -> none
  N2  per-row      named like a price, rate, percentage, score, age or coordinate -> none
  N3  bounded      a fraction: every value in [0, 1], not all whole -> none
  A2  credit       a fraction that sums to 1 within each value of a repeating identifier: it
                   splits one event (attribution credit), so its sum counts events -> sum
  U1  audience     named like a count of distinct people (reach, uniques, DAU): summed across
                   rows the same people are counted again -> none
  S2  flow         a level's movement (new, expansion, churned MRR; follower growth): movements
                   over a period add -> sum
  C1  currencies   money beside a currency column holding several codes, and not named as
                   converted (usd, base, reporting): a total adds unlike money -> none
  S1  snapshot     named like a balance or a stock level -> none (no last-value aggregation yet)
  D1  duration     named like days, hours or seconds -> mean (a total of time spent can mean
                   something: hours worked, session time)
  D2  time taken   a duration named like how long something TOOK (delivery, delay, latency,
                   wait, response, turnaround) -> mean, never a sum: 50 + 70 minutes is no KPI
  A1  additive     named like an amount, a cost or a count, varying within the unit -> sum
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from backend.engine.clean import sql
from backend.engine.contract.evidence import (
    ColumnEvidence,
    DatasetEvidence,
    _is_numeric,
    _is_temporal,
    _is_whole,
    suggest_role,
)

STRONG, LIKELY, UNSURE = "strong", "likely", "unsure"

#: A row share a formula must reach to be called what the column IS, and the sample it is read on.
RATIO_MATCH = 0.98
RATIO_SAMPLE = 5_000
RATIO_MIN_ROWS = 50
#: A text column with more values than this may be a unit a measure repeats within.
UNIT_MIN_DISTINCT = 20

# The name-token classes live in the core pack (packs/core/pack.yaml: word_classes), moved
# verbatim from here in v2 B2; test_packs.py pins them equal to v1's sets.
from backend.packs.loader import core_word_classes as _core_word_classes  # noqa: E402

_W = _core_word_classes()
_CODE = frozenset(_W["code"])
_PER_ROW = frozenset(_W["per_row"])
_LAT = frozenset(_W["lat"])
_LNG = frozenset(_W["lng"])
_DISTANCE = frozenset(_W["distance"])
_DISCREPANCY = frozenset(_W["discrepancy"])
_TAKEN = frozenset(_W["taken"])
_SNAPSHOT = frozenset(_W["snapshot"])
_FLOW = frozenset(_W["flow"])
_MONEY = frozenset(_W["money"])
_CONVERTED = frozenset(_W["converted"])
_RATIO_WORDS = frozenset(_W["ratio_words"])
_DISTINCT_PEOPLE = frozenset(_W["distinct_people"])
_CREDIT = frozenset(_W["credit"])
_DURATION = frozenset(_W["duration"])
_ADDITIVE = frozenset(_W["additive"])


@dataclass(frozen=True)
class Suggestion:
    measure: str
    agg: str | None
    strength: str
    rule: str
    reason: str
    per: list[str] = field(default_factory=list)
    #: For R1, a ratio of sums to ADD beside the column: {"name", "numerator", "denominator",
    #: "scale", "definition"}. The column itself is suggested `none`: per row it is right.
    ratio: dict | None = None
    #: For F1's helper measure: the flag column it reads (a measure named `<flag>_rate`).
    column: str | None = None

    def question(self) -> str:
        """The one plain question a person answers when the engine cannot settle it."""
        m = self.measure
        if self.strength == STRONG:
            return ""
        if self.per:
            unit = " + ".join(self.per)
            return (f"{m} is one value per {unit} -- the same on every row of each. Counted once "
                    f"per {unit}: is it an amount to add up, like a fee (sum), or a value to "
                    f"average, like a rating (mean)?")
        if self.strength == LIKELY:
            yes = {"sum": "an amount to add up (sum)", "mean": "a value to average (mean)",
                   "none": "a per-row value, like a price or a rate (none)"}[self.agg or "none"]
            return (f"{m}: the engine reads it as {yes}, from its name only. If you add up {m} "
                    f"for every row, is that total meaningful? Yes -> sum; no, it is per row -> "
                    f"none.")
        return (f"{m}: if you add up {m} for every row, is that total meaningful? (a) yes, it is "
                f"an amount -> sum; (b) no, it is per row, like a price -> none; (c) it repeats on "
                f"every line of an order or a customer -> say which, and it is counted once each.")

    def to_text(self) -> str:
        per = f" per {' + '.join(self.per)}" if self.per else ""
        head = f"{self.measure}: {self.agg or '(no suggestion)'}{per} [{self.strength}, {self.rule}]"
        out = f"{head} -- {self.reason}"
        if self.ratio:
            out += (f' Add ratios={{"{self.ratio["name"]}": {{"numerator": '
                    f'{self.ratio["numerator"]}, "denominator": {self.ratio["denominator"]}, '
                    f'"scale": {self.ratio["scale"]:g}}}}} for group figures.')
        return out


def tokens(name: str) -> set[str]:
    """'rep_monthly_salary' -> {rep, monthly, salary}; 'stockOnHand' -> {stock, on, hand, onhand}."""
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()
    words = [w for w in re.split(r"[^a-z0-9]+", parts) if w]
    joined = {a + b for a, b in zip(words, words[1:])}  # on_hand -> onhand, lead_time -> leadtime
    return set(words) | joined


def _num(v: str | None) -> float | None:
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


def _per_units(con, table: str, ev: DatasetEvidence, measures: list[str]) -> dict[str, str]:
    """P1: for each measure, the coarsest repeating identifier it is constant within.

    One grouped scan per candidate identifier, every measure at once: within each value of the
    identifier, a measure is constant when its min equals its max and it is null in all rows or
    none (count(DISTINCT (id, m)) per measure took 3.9 s on the retail fixture). A candidate must
    repeat -- at most 90% as many values as rows -- or every column is constant within it.
    """
    t = sql.ident(table)
    ids = [c for c in ev.columns
           if c.name not in measures and c.row_count and 1 < c.distinct <= 0.9 * c.row_count
           and c.non_null == c.row_count and _id_like(c)]
    found: dict[str, tuple[int, str]] = {}
    varying = [m for m in measures if ev.column(m).distinct > 1]
    if not varying:
        return {}
    for c in ids:
        i = sql.ident(c.name)
        flags = ", ".join(
            f"CAST(min({q}) IS DISTINCT FROM max({q}) OR (count({q}) > 0 AND count({q}) < count(*))"
            f" AS INTEGER) AS v{k}"
            for k, q in enumerate(sql.ident(m) for m in varying))
        sums = ", ".join(f"sum(v{k})" for k in range(len(varying)))
        row = con.execute(f"SELECT count(*), {sums} FROM "
                          f"(SELECT {flags} FROM {t} GROUP BY {i})").fetchone()
        for m, bad in zip(varying, row[1:]):
            if bad == 0 and (m not in found or row[0] < found[m][0]):
                found[m] = (row[0], c.name)
    return {m: name for m, (_, name) in found.items()}


def currency_codes(con, table: str, ev: DatasetEvidence) -> tuple[str, list[str]] | None:
    """(column, codes) for a text column named like a currency that holds 2-20 ISO-like codes."""
    for c in ev.columns:
        w = tokens(c.name)
        if not (w & {"currency", "curr", "ccy", "iso4217"}) or not (2 <= c.distinct <= 20):
            continue
        if _is_numeric(c.dtype) or _is_temporal(c.dtype):
            continue
        q = sql.ident(c.name)
        codes = [r[0] for r in con.execute(
            f"SELECT DISTINCT {q} FROM {sql.ident(table)} WHERE {q} IS NOT NULL ORDER BY 1"
        ).fetchall()]
        if codes and all(isinstance(v, str) and len(v.strip()) == 3 and v.strip().isalpha()
                         for v in codes):
            return c.name, codes
    return None


def _sums_to_one_within(con, table: str, ev: DatasetEvidence, m: str) -> str | None:
    """A2: the repeating identifier within which `m` sums to 1 (rounding allowed) in 99% of
    values -- the shape of fractional attribution -- or None."""
    t, q = sql.ident(table), sql.ident(m)
    for c in ev.columns:
        if c.name == m or not (1 < c.distinct <= 0.9 * c.row_count) or not _id_like(c):
            continue
        n, ok = con.execute(
            f"SELECT count(*), count(*) FILTER (WHERE abs(s - 1) <= 0.011) FROM "
            f"(SELECT sum({q}) AS s FROM {t} GROUP BY {sql.ident(c.name)})").fetchone()
        if n and ok >= 0.99 * n:
            return c.name
    return None


def _id_like(c: ColumnEvidence) -> bool:
    """Named like an identifier, or text with more than UNIT_MIN_DISTINCT values. Not a date: a
    hire date is shared by the rows of one rep, and is not what a salary is paid per. The text
    half exists for the bench's renamed columns (c1, c2): a unit is found by its values, not only
    by a name ending in _id (scripts/suggest_bench.py, 25/09/2026)."""
    if _is_temporal(c.dtype):
        return False
    # Not "code": coupon_code (3 values) made discount_pct "one value per coupon" (v2 bench,
    # 25/09/2026). A code is a category unless it has many values (the text rule below).
    if tokens(c.name) & {"id", "key", "number", "no", "sku", "uuid"}:
        return True
    # Many values, not the role guess: the role rules call any *_code an identifier, and
    # coupon_code has three (v2 bench, 25/09/2026).
    return not _is_numeric(c.dtype) and c.distinct > UNIT_MIN_DISTINCT


_FORMS = (  # (label, sql over a b, scale, numerator, denominator)
    ("{a} / {b}", "{a} / {b}", 1.0, ["{a}"], ["{b}"]),
    ("{a} / {b} x 100", "100.0 * {a} / {b}", 100.0, ["{a}"], ["{b}"]),
    ("({a} - {b}) / {a}", "({a} - {b}) / {a}", 1.0, ["{a}", "-{b}"], ["{a}"]),
    ("({a} - {b}) / {a} x 100", "100.0 * ({a} - {b}) / {a}", 100.0, ["{a}", "-{b}"], ["{a}"]),
    # per mille: CPM is spend per thousand impressions (marketing bench, 25/09/2026)
    ("{a} / {b} x 1000", "1000.0 * {a} / {b}", 1000.0, ["{a}"], ["{b}"]),
)


#: Rows read into Python to screen pairs before any is counted in SQL.
RATIO_SCREEN_ROWS = 64
_PY_FORMS = {  # the arithmetic of each _FORMS entry, for the screen
    "{a} / {b}": lambda a, b: a / b,
    "{a} / {b} x 100": lambda a, b: 100.0 * a / b,
    "({a} - {b}) / {a}": lambda a, b: (a - b) / a,
    "({a} - {b}) / {a} x 100": lambda a, b: 100.0 * (a - b) / a,
    "{a} / {b} x 1000": lambda a, b: 1000.0 * a / b,
}


def _close(x: float, y: float, scale: float) -> bool:
    tol = 0.00006 if scale == 1.0 else 0.006
    return abs(x - y) <= tol + 1e-4 * abs(y)


def _ratio_of(con, table: str, m: str, parts: list[str], screen: list[tuple] | None = None,
              names: list[str] | None = None) -> dict | None:
    """R1: the form and pair of other measures that `m` equals on RATIO_MATCH of sampled rows.

    Screened first in Python on RATIO_SCREEN_ROWS rows: every pair of every form in SQL was
    about 10,000 FILTER clauses on the marketing bench's 18 measures (4.4 s -> 7.8 s); a pair
    that fails a quarter of 64 rows cannot reach 98% of 5,000, and only survivors are counted.
    """
    pairs = [(a, b) for a in parts for b in parts if a != b and m not in (a, b)]
    if not pairs:
        return None
    if screen is not None and names is not None:
        at = {n: k for k, n in enumerate(names)}
        kept = []
        for a, b in pairs:
            for label, expr, scale, num, den in _FORMS:
                fn, hits, seen = _PY_FORMS[label], 0, 0
                for row in screen:
                    x, va, vb = row[at[m]], row[at[a]], row[at[b]]
                    if x is None or va is None or vb is None or float(x) == 0:
                        continue
                    try:
                        y = fn(float(va), float(vb))
                    except ZeroDivisionError:
                        continue
                    seen += 1
                    hits += _close(float(x), y, scale)
                if seen and hits >= 0.75 * seen:
                    kept.append((a, b, label, expr, scale, num, den))
        if not kept:
            return None
    else:
        kept = [(a, b, *form) for a, b in pairs for form in _FORMS]
    t, q = sql.ident(table), sql.ident(m)
    checks, labels = [], []
    for a, b, label, expr, scale, num, den in kept:
        qa, qb = sql.ident(a), sql.ident(b)
        e = expr.format(a=qa, b=qb)
        den_col = qb if "/ {b}" in expr else qa
        tol = 0.00006 if scale == 1.0 else 0.006
        checks.append(f"count(*) FILTER (WHERE {den_col} <> 0 AND "
                      f"abs({q} - ({e})) <= {tol} + 1e-4 * abs({e}))")
        labels.append((label.format(a=a, b=b), scale,
                       [n.format(a=a, b=b) for n in num], [d.format(a=a, b=b) for d in den]))
    cols = [q] + sorted({sql.ident(x) for a, b, *_ in kept for x in (a, b)})
    # Rows where the column is 0 prove nothing: churned_mrr, 0 on 98% of rows, "equalled"
    # contraction_mrr / anything on the zeros (v2 bench, 25/09/2026).
    where = " AND ".join(f"{c} IS NOT NULL" for c in cols) + f" AND {q} <> 0"
    row = con.execute(
        f"SELECT count(*), {', '.join(checks)} FROM "
        f"(SELECT * FROM {t} WHERE {where} LIMIT {RATIO_SAMPLE})").fetchone()
    n = row[0]
    if n < RATIO_MIN_ROWS:
        return None
    best = max(range(len(labels)), key=lambda k: row[k + 1])
    if row[best + 1] < RATIO_MATCH * n:
        return None
    label, scale, num, den = labels[best]
    return {"label": label, "share": row[best + 1] / n, "rows": n, "scale": scale,
            "numerator": num, "denominator": den}


def suggest(con, table: str, ev: DatasetEvidence, measures: list[str],
            date_column: str | None = None) -> dict[str, Suggestion]:
    """A suggestion for every measure named, in order. Reads the table; writes nothing."""
    cols = {c.name: c for c in ev.columns}
    present = [m for m in measures if m in cols and _is_numeric(cols[m].dtype)]
    per = _per_units(con, table, ev, present) if present else {}
    first: dict[str, Suggestion] = {}
    for m in present:
        first[m] = _by_name_and_values(cols[m], per.get(m))
    # C1: money beside a currency column with several codes, not named as converted.
    currencies = currency_codes(con, table, ev)
    if currencies:
        col, codes = currencies
        for m in present:
            w = tokens(m)
            if w & _MONEY and not (w & _CONVERTED) and first[m].agg == "sum":
                first[m] = Suggestion(
                    m, "none", STRONG, "C1",
                    f"{m} is money in {len(codes)} currencies ({col}: {', '.join(codes)}): a "
                    f"total adds rupees to dollars. Use a converted column, or group by {col}.",
                    per=list(first[m].per))
    # R1: any measure the rules above did not settle as an amount, a unit's value or a code is
    # tried as one per-row measure over another -- by values, so a renamed column is still found.
    # Not a 0/1 flag: a / flag equals a wherever the flag is 1 -- churned_mrr "was" mrr /
    # is_churned (v2 bench, 25/09/2026).
    parts = [m for m in present if not first[m].per and not is_flag(cols[m])]
    for m in present:
        if first[m].rule in ("N3", "A2"):
            unit = _sums_to_one_within(con, table, ev, m)
            if unit:
                first[m] = Suggestion(
                    m, "sum", STRONG, "A2",
                    f"{m} sums to 1 within every {unit}: it splits one {unit} into shares (as "
                    f"attribution credit does), so its sum counts {unit}s -- a total, not a "
                    f"rate.")
    out: dict[str, Suggestion] = {}
    screen: list[tuple] | None = None
    for m in measures:
        if m not in cols:
            continue
        if m not in first:
            c = cols[m]
            out[m] = Suggestion(m, None, UNSURE, "T1",
                                f"{m} holds {c.dtype}, not numbers, so it can only be counted: if "
                                f"it is numbers written as text, convert it on the Clean screen "
                                f"first (propose_cleaning_plan), and ask again.")
            continue
        s = first[m]
        # Not a whole-number column: amount = price x quantity makes quantity = amount / price
        # too, and a count is not a rate (the bench's renamed quantity, 25/09/2026). Not a column
        # already read as adding up: expansion_mrr equalled mrr / seats (one seat's price a
        # month) and is still a flow (v2 bench, 25/09/2026).
        if (s.rule not in ("P1", "N1") and s.agg != "sum" and len(parts) > 2
                and not _is_whole(cols[m].dtype)):
            if screen is None:
                cur = con.execute(
                    f"SELECT {', '.join(sql.ident(x) for x in present)} FROM "
                    f"{sql.ident(table)} USING SAMPLE {RATIO_SCREEN_ROWS} ROWS (reservoir, 11)")
                screen = cur.fetchall()
            found = _ratio_of(con, table, m, [p for p in parts if p != m], screen, present)
            if found:
                name = f"{m}_of_sums"
                s = Suggestion(
                    m, "none", STRONG, "R1",
                    f"{m} = {found['label']} on {found['share']:.1%} of {found['rows']:,} sampled "
                    f"rows: a ratio per row. Its average over rows is not a group's figure -- for "
                    f"that, a ratio of sums.",
                    ratio={"name": name, "numerator": found["numerator"],
                           "denominator": found["denominator"], "scale": found["scale"],
                           "definition": f"{found['label']}, from the summed columns "
                                         f"(a ratio of sums, not a mean of {m})"})
        out[m] = s
    return out


def is_flag(c: ColumnEvidence) -> bool:
    """Only two values, 0 and 1 (or true and false): an indicator, whatever its type says."""
    if c.dtype.upper() == "BOOLEAN":
        return c.distinct == 2
    if not _is_numeric(c.dtype) or c.distinct != 2:
        return False
    return _num(c.min_value) == 0 and _num(c.max_value) == 1


def _coordinate_kind(c: ColumnEvidence) -> tuple[str, bool] | None:
    """('latitude' | 'longitude', every value in range) for a column named like one."""
    if not _is_numeric(c.dtype):
        return None
    words = tokens(c.name)
    lo, hi = _num(c.min_value), _num(c.max_value)
    within = lambda bound: lo is not None and hi is not None and -bound <= lo and hi <= bound
    if words & _LAT:
        return "latitude", within(90)
    if words & _LNG and not (words & {"term", "run", "haul"}):  # a long-term rate is not a place
        return "longitude", within(180)
    return None


def flag_rates(ev: DatasetEvidence, measures: list[str]) -> list[Suggestion]:
    """F1 for a flag that is NOT a measure: a helper measure `<flag>_rate` reading it, so the
    flag stays a dimension to group by and its share is a measure to compare."""
    out = []
    for c in ev.columns:
        if c.name in measures or not is_flag(c):
            continue
        name = f"{c.name}_rate"
        out.append(Suggestion(
            name, "mean", STRONG, "F1",
            f"{c.name} holds only {'true and false' if c.dtype.upper() == 'BOOLEAN' else '0 and 1'}"
            f": the share of rows where it is {'true' if c.dtype.upper() == 'BOOLEAN' else '1'} is "
            f"a rate to compare across groups, while {c.name} itself stays a dimension.",
            column=c.name))
    return out


def _by_name_and_values(c: ColumnEvidence, unit: str | None) -> Suggestion:
    m, words = c.name, tokens(c.name)
    lo, hi = _num(c.min_value), _num(c.max_value)
    additive_name = bool(words & _ADDITIVE) and not (words & (_RATIO_WORDS | {"unit"}))

    if unit:
        # The unit is measured; whether one value per unit adds up (a fee) or averages (a rating,
        # a salary) is in the name or nowhere -- renamed to c13, a fee read as "mean, strong"
        # (scripts/suggest_bench.py, 25/09/2026). Without a name that says, it is only likely.
        averaged = bool(words & (_PER_ROW | _DURATION))
        agg = "sum" if additive_name else "mean"
        strength = STRONG if (additive_name or averaged) else LIKELY
        return Suggestion(m, agg, strength, "P1",
                          f"{m} is the same on every row of each {unit} ({c.distinct:,} distinct "
                          f"values): one value per {unit}, so it is counted once per {unit}"
                          + (", then added up." if agg == "sum" else ", then averaged."),
                          per=[unit])
    if words & _CODE and _is_whole(c.dtype):
        return Suggestion(m, "none", STRONG, "N1",
                          f"{m} is a whole number named like a code or a calendar part; adding "
                          f"codes or years means nothing -- group by it instead.")
    if is_flag(c):
        return Suggestion(m, "mean", STRONG, "F1",
                          f"{m} holds only 0 and 1: its mean is the share of rows that are 1 (a "
                          f"rate), its sum the number of them.")
    coordinate = _coordinate_kind(c)
    if coordinate:
        return Suggestion(m, "none", STRONG if coordinate[1] else LIKELY, "G1",
                          f"{m} is a {coordinate[0]}: a location, not an amount. Adding "
                          f"coordinates means nothing; group or map by them"
                          + ("." if coordinate[1] else
                             f" -- though some values lie outside {coordinate[0]} range."))
    if words & _DISTANCE:
        if words & _DISCREPANCY:
            return Suggestion(m, "none", LIKELY, "G2",
                              f"{m} is named like a distance between a recorded and an expected "
                              f"point (how far off, not how far travelled): its total means "
                              f"nothing; its mean or median is the typical error.")
        return Suggestion(m, "sum", LIKELY, "G2",
                          f"{m} is named like a distance travelled: its sum is the total "
                          f"distance covered, its mean the typical trip -- both are fair "
                          f"questions; sum is the total.")
    if words & _SNAPSHOT and words & _FLOW:
        return Suggestion(m, "sum", LIKELY, "S2",
                          f"{m} is named like a movement of a level (new, expansion, churned): "
                          f"movements over a period add, where the level itself does not.")
    if words & _SNAPSHOT:
        return Suggestion(m, "none", LIKELY, "S1",
                          f"{m} is named like a level measured at a moment (a balance, a stock "
                          f"count): summed across dates it counts the same stock again. The "
                          f"engine has no last-value aggregation yet, so none.")
    if words & _DISTINCT_PEOPLE:
        return Suggestion(m, "none", LIKELY, "U1",
                          f"{m} is named like a count of distinct people: each row counts its own, "
                          f"and rows share people -- summed across days or campaigns the same "
                          f"people are counted again.")
    if (words & _PER_ROW or (words & {"unit"} and words & _ADDITIVE)) and not additive_name:
        bounded = lo is not None and hi is not None and -100 <= lo and hi <= 100
        strength = STRONG if (words & (_RATIO_WORDS | {"lat", "latitude", "lng", "lon",
                                                       "longitude", "age", "unit"})
                              or bounded) else LIKELY
        return Suggestion(m, "none", strength, "N2",
                          f"{m} is named like a per-row value (a price, a rate, a percentage, a "
                          f"score): its total means nothing.")
    if (lo is not None and hi is not None and 0 <= lo and hi <= 1 and not _is_whole(c.dtype)
            and c.distinct > 2):
        if words & _CREDIT:
            return Suggestion(m, "sum", LIKELY, "A2",
                              f"{m} is a fraction named like a share of credit: summed, the shares "
                              f"count the events they split.")
        return Suggestion(m, "none", LIKELY, "N3",
                          f"every value of {m} lies between 0 and 1: a fraction or a rate, whose "
                          f"total means nothing.")
    if words & _DURATION and words & _TAKEN:
        return Suggestion(m, "mean", LIKELY, "D2",
                          f"{m} is named like how long something took: its mean or median is the "
                          f"usual question, and a total of such times describes nothing.")
    if words & _DURATION:
        return Suggestion(m, "mean", LIKELY, "D1",
                          f"{m} is named like a duration per row: its average is the usual "
                          f"question; a total of durations rarely is.")
    if additive_name:
        strength = STRONG if (lo is None or lo >= 0 or (hi is not None and hi > 0)) else LIKELY
        return Suggestion(m, "sum", strength, "A1",
                          f"{m} is named like an amount or a count and differs from row to row "
                          f"within every larger unit: rows add up.")
    return Suggestion(m, None, UNSURE, "-", f"nothing in {m}'s name or values says how it "
                                            f"combines.")


__all__ = ["LIKELY", "STRONG", "Suggestion", "UNSURE", "currency_codes", "flag_rates", "is_flag",
           "suggest", "tokens"]
