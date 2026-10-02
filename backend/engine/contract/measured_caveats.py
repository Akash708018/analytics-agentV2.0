"""
Caveats the engine counts itself, so nobody has to type them (25/09/2026).

The retail contract's caveats were typed from a generator's notes, and two of their counts were
wrong: 3,470 where the table held 3,471 and 3,473. Every result printed them, and the answer
repeated them as findings. The profiler had the right counts all along. So the counts a caveat
needs are taken here, from the table, at the moment the contract is proposed -- and again when it
is confirmed, whatever the JSON coming back says.

What is counted, each a rule with an id:

  M1 placeholder   text that stands for absent without being NULL: 'unknown', 'n/a', '-', '?'
  M2 blank         NULL or whitespace-only; columns blank in exactly the same rows are one line,
                   and when those rows are exactly one value of a dimension or flag, it is named
                   ("blank exactly where channel = 'Store'")
  M3 negative      a numeric column below zero in a few rows (at most 5% of its values)
  M4 text dates    a text column most of whose values read as dates, with how many do not
  M5 empty months  months inside the date column's span holding no rows
  M6 subtotals     a text value 'Total', 'Grand total', 'Subtotal' or 'All' -- a row that doubles
                   every sum
  M7 coordinates   a latitude outside [-90, 90] or a longitude outside [-180, 180]; a pair whose
                   latitude is out of range while its longitude would fit (swapped); rows at
                   (0, 0), which is a missing location written as zero more often than a place
  M8 spellings     a text column whose values differ only by case or spaces ('Pune_West',
                   'PUNE_WEST'): counted apart by every group-by until cleaning folds them
  M9 over 100%     a percentage above 100 (or a fraction above 1) in a column that elsewhere
                   never is: a part larger than its whole -- clicks above impressions
  M10 split        an identifier that falls under more than one value of a dimension for a few
                   of its values (users in both A/B variants, orders claimed by two platforms):
                   a breakdown by the dimension counts those in each
  M11 future       a date after today
  M12 currencies   money beside a currency column holding several codes: a total adds unlike money
  M13 date order   two dates in the order one always follows the other, except in a few rows
                   (a qualified lead before the lead existed; a sale after the rep left)
  M14 near miss    a column blank in every row of one flag value but a few: those few hold a
                   value where nothing else does (a deal amount on a lost deal)
(v2 marketing bench, 25/09/2026, for M10-M14.)

Duplicate rows are not here: the gate counts them live on every result when no key is stated
(state.Gate.caveats), and a key that is stated is verified unique.
"""

from __future__ import annotations

from backend.engine.clean import sql
from backend.engine.contract.evidence import (
    DatasetEvidence,
    _is_numeric,
    _is_temporal,
    _is_text,
)

PLACEHOLDERS = ("unknown", "n/a", "na", "none", "null", "nil", "-", "--", "?", "missing",
                "not available", "tbd", "#n/a", "(blank)", "undefined", "not known",
                # Google Analytics' own words for missing (marketing bench, 25/09/2026). Not
                # '(direct)', '(none)' or '(other)': those are real GA values.
                "(not set)", "not set", "(not provided)", "not provided", "(unknown)")
SUBTOTALS = ("total", "grand total", "subtotal", "sub total", "all", "overall")
NEGATIVE_SHARE = 0.05
TEXT_DATE_SHARE = 0.5
SAMPLE_DATE_SHARE = 0.25  # of a 2,000-row sample, to be counted in full
EXPLAIN_MAX_DISTINCT = 12
MAX_LINES = 16


def _scalar(con, query: str):
    return con.execute(query).fetchone()[0]


def _pct(n: int, total: int) -> str:
    share = n / total if total else 0
    return f"{share:.1%}" if share >= 0.001 else "under 0.1%"


def _text_counts(con, t: str, text: list[str]) -> dict[str, tuple[int, int, int]]:
    """Per text column: placeholders, subtotal words, whitespace-only values -- one scan."""
    if not text:
        return {}
    held = sql.token_list(list(PLACEHOLDERS)).lower()
    subs = sql.token_list(list(SUBTOTALS)).lower()
    exprs = []
    for c in text:
        v = f"lower(trim({sql.ident(c)}))"
        exprs += [f"count(*) FILTER (WHERE {v} IN ({held}))",
                  f"count(*) FILTER (WHERE {v} IN ({subs}))",
                  f"count(*) FILTER (WHERE {v} = '')"]
    row = con.execute(f"SELECT {', '.join(exprs)} FROM {t}").fetchone()
    return {c: tuple(row[3 * k:3 * k + 3]) for k, c in enumerate(text)}


def _placeholders(con, t: str, counts: dict, rows: int) -> list[str]:
    listed = sql.token_list(list(PLACEHOLDERS)).lower()
    out = []
    for c, (n, _, _) in counts.items():
        if not n:
            continue
        q = sql.ident(c)
        tokens = con.execute(
            f"SELECT trim({q}), count(*) FROM {t} WHERE lower(trim({q})) IN ({listed}) "
            f"GROUP BY 1 ORDER BY 2 DESC, 1").fetchall()
        what = ", ".join(f"{v!r} in {k:,}" for v, k in tokens)
        out.append(f"{c} holds a placeholder for a missing value: {what} row(s) "
                   f"({_pct(n, rows)}). It is text, not NULL, so it is counted as a value.")
    return out


def _blanks(con, t: str, ev: DatasetEvidence, counts: dict, explain: list[str]) -> list[str]:
    rows = ev.row_count
    if not rows:
        return []
    whitespace = {c: k[2] for c, k in counts.items()}
    blank = {c.name: c.null_count + whitespace.get(c.name, 0) for c in ev.columns}
    groups: dict[int, list[str]] = {}
    for c in ev.columns:
        if 0 < blank[c.name] < rows:
            groups.setdefault(blank[c.name], []).append(c.name)
    out = []
    for n, cols in sorted(groups.items(), key=lambda kv: -kv[0]):
        is_blank = [f"({sql.ident(c)} IS NULL" + (f" OR trim({sql.ident(c)}) = '')"
                                                   if c in whitespace else ")") for c in cols]
        together = " AND ".join(is_blank)
        same = len(cols) == 1 or _scalar(con, f"SELECT count(*) FROM {t} WHERE {together}") == n
        subsets = [[c] for c in cols] if not same else [cols]
        for part in subsets:
            cond = " AND ".join(b for c, b in zip(cols, is_blank) if c in part)
            others = [e for e in explain if e not in part]
            where = _explained(con, t, cond, n, others)
            near = None if where or len(part) > 1 else _near(con, t, cond, n, rows, others)
            names = part[0] if len(part) == 1 else ", ".join(part[:-1]) + f" and {part[-1]}"
            verb = "is" if len(part) == 1 else "are"
            same_rows = " in the same" if len(part) > 1 else " in"
            tail = (f" -- exactly the rows where {where}." if where else
                    f" -- every one of them where {near[0]}; {names} holds a value in "
                    f"{near[1]:,} row(s) where {near[0]}, where nothing else does." if near
                    else ".")
            # Unexplained and small first: a 1.5% blank is a data problem more often than an 88%
            # one, or one that is exactly a flag's rows. The cap cut customer_state's 3,473 when
            # blanks were listed largest first (retail, 25/09/2026).
            out.append(((1 if where else 0), n,
                        f"{names} {verb} blank{same_rows} {n:,} row(s) ({_pct(n, rows)}){tail}"))
    return [text for _, _, text in sorted(out)]


def _explained(con, t: str, cond: str, n: int, explain: list[str]) -> str | None:
    """The values of dimensions or flags whose rows are exactly the blank rows, if any: at most
    two, a text dimension first -- "channel = 'Store'" says more than "delivery_days = 0"."""
    if not explain:
        return None
    found: list[str] = []
    sets = ", ".join(f"({sql.ident(e)})" for e in explain)
    cols = ", ".join(sql.ident(e) for e in explain)
    rows = con.execute(
        f"SELECT {cols}, GROUPING_ID({cols}), count(*), count(*) FILTER (WHERE {cond}) "
        f"FROM {t} GROUP BY GROUPING SETS ({sets})").fetchall()
    k = len(explain)
    for r in rows:
        total, hit = r[k + 1], r[k + 2]
        if total == n and hit == n:
            for idx, e in enumerate(explain):
                if not (r[k] >> (k - 1 - idx)) & 1:  # this grouping set is e's
                    v = r[idx]
                    shown = "NULL" if v is None else (repr(v) if isinstance(v, str)
                                                      else str(v).lower())
                    found.append((v is None, 0 if isinstance(v, str) else 1 if isinstance(v, bool)
                                  else 2, f"{e} = {shown}"))
    # A value before NULL, text before a flag before a number -- sorted, not the order the
    # grouping sets come back in, which varies (v2 bench, 25/09/2026).
    return " and ".join(f for _, _, f in sorted(found)[:2]) or None


def _near(con, t: str, cond: str, n: int, rows: int, explain: list[str]):
    """(value, extra) for a flag or dimension value whose rows hold every blank row plus a few
    more -- at most 1% of the table: a value where the value says there should be none."""
    if not explain:
        return None
    sets = ", ".join(f"({sql.ident(e)})" for e in explain)
    cols = ", ".join(sql.ident(e) for e in explain)
    k = len(explain)
    found: list = []
    for r in con.execute(
            f"SELECT {cols}, GROUPING_ID({cols}), count(*), count(*) FILTER (WHERE {cond}) "
            f"FROM {t} GROUP BY GROUPING SETS ({sets})").fetchall():
        total, hit = r[k + 1], r[k + 2]
        if hit == n and 0 < total - n <= 0.01 * rows:
            for idx, e in enumerate(explain):
                if not (r[k] >> (k - 1 - idx)) & 1:
                    v = r[idx]
                    shown = "NULL" if v is None else (repr(v) if isinstance(v, str)
                                                      else str(v).lower())
                    found.append((v is None, 0 if isinstance(v, str) else 1 if isinstance(v, bool)
                                  else 2, f"{e} = {shown}", total - n))
    # Deterministic, a value before NULL: "days_to_close = NULL" won over "is_won = 0" when the
    # grouping sets happened to come back in that order (v2 bench, 25/09/2026).
    return (lambda b: (b[2], b[3]))(sorted(found)[0]) if found else None


def _negatives(con, t: str, numeric: list[str], ev: DatasetEvidence) -> list[str]:
    cands = [c for c in numeric if ev.column(c).min_value is not None
             and _lt_zero(ev.column(c).min_value)]
    if not cands:
        return []
    exprs = ", ".join(f"count(*) FILTER (WHERE {sql.ident(c)} < 0), min({sql.ident(c)})"
                      for c in cands)
    row = con.execute(f"SELECT {exprs} FROM {t}").fetchone()
    out = []
    for k, c in enumerate(cands):
        n, lo = row[2 * k], row[2 * k + 1]
        nn = ev.column(c).non_null
        if n and n <= NEGATIVE_SHARE * nn:
            out.append(f"{c} is below zero in {n:,} row(s) (as low as {lo:g}) -- a return, a "
                       f"correction or an entry error; every total includes them.")
        elif n:
            # B12 QA: above the share this was silent; refunds beating sales on a few days of a
            # short file went unmentioned. A signed column is said, not assumed.
            out.append(f"{c} is below zero in {n:,} row(s) ({_pct(n, nn)}, as low as {lo:g}) "
                       f"-- a signed measure (refunds, corrections, losses); every total nets "
                       f"them.")
    return out


def _lt_zero(v) -> bool:
    try:
        return float(v) < 0
    except (TypeError, ValueError):
        return False


def _text_dates(con, t: str, ev: DatasetEvidence, text: list[str]) -> list[str]:
    cands = [c for c in text if ev.column(c).non_null]
    if not cands:
        return []
    # A sample first: casting every text column of 225k rows took 4.7 s, and a column none of
    # whose first 2,000 values is a date is not a column of dates.
    sample = ", ".join(f"count(TRY_CAST({sql.ident(c)} AS DATE)), count({sql.ident(c)})"
                       for c in cands)
    got = con.execute(f"SELECT {sample} FROM (SELECT * FROM {t} LIMIT 2000)").fetchone()
    cands = [c for k, c in enumerate(cands)
             if got[2 * k + 1] and got[2 * k] >= SAMPLE_DATE_SHARE * got[2 * k + 1]]
    if not cands:
        return []
    exprs = ", ".join(f"count(TRY_CAST({sql.ident(c)} AS DATE))" for c in cands)
    parsed = con.execute(f"SELECT {exprs} FROM {t}").fetchone()
    out = []
    for c, ok in zip(cands, parsed):
        nn = ev.column(c).non_null
        if TEXT_DATE_SHARE * nn <= ok < nn:
            q = sql.ident(c)
            examples = [r[0] for r in con.execute(
                f"SELECT DISTINCT {q} FROM {t} WHERE {q} IS NOT NULL AND TRY_CAST({q} AS DATE) "
                f"IS NULL ORDER BY 1 LIMIT 2").fetchall()]
            out.append(f"{c} is text: {ok:,} value(s) read as dates and {nn - ok:,} are written "
                       f"another way (e.g. {', '.join(repr(e) for e in examples)}). The Clean "
                       f"screen converts both.")
    return out


def _empty_months(con, t: str, date_column: str | None, ev: DatasetEvidence) -> list[str]:
    if not date_column or date_column not in {c.name for c in ev.columns}:
        return []
    if not _is_temporal(ev.column(date_column).dtype):
        return []
    q = sql.ident(date_column)
    # Up to today: a 2031 typo stretched promo_orders' span and named 69 empty months (v2 bench).
    lo, hi = con.execute(f"SELECT min({q}), max({q}) FROM {t} WHERE {q} <= current_date"
                         ).fetchone()
    if lo is None:
        return []
    seen = {r[0] for r in con.execute(
        f"SELECT DISTINCT strftime(date_trunc('month', {q}), '%Y-%m') FROM {t} "
        f"WHERE {q} IS NOT NULL").fetchall()}
    months, y, m = [], lo.year, lo.month
    while (y, m) <= (hi.year, hi.month):
        months.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    empty = [x for x in months if x not in seen]
    if not empty:
        return []
    shown = ", ".join(empty[:6]) + (f" and {len(empty) - 6} more" if len(empty) > 6 else "")
    span = f"{months[0]} to {months[-1]}"
    return [f"{date_column} has no rows in {shown} -- inside its span {span}. A trend shows the "
            f"month as empty; nothing in the data says why."]


def _subtotals(con, t: str, counts: dict) -> list[str]:
    listed = sql.token_list(list(SUBTOTALS)).lower()
    out = []
    for c, (_, n, _) in counts.items():
        if n:
            q = sql.ident(c)
            vals = [r[0] for r in con.execute(
                f"SELECT DISTINCT trim({q}) FROM {t} WHERE lower(trim({q})) IN ({listed}) "
                f"ORDER BY 1").fetchall()]
            out.append(f"{c} holds {', '.join(repr(v) for v in vals)} in {n:,} row(s): if those "
                       f"are subtotal rows, every sum over the table counts their rows twice.")
    return out


def _coordinates(con, t: str, ev: DatasetEvidence) -> list[str]:
    from backend.engine.contract.suggest import _LAT, _LNG, tokens

    numeric = [c.name for c in ev.columns if _is_numeric(c.dtype)]
    lats = [c for c in numeric if tokens(c) & _LAT]
    lngs = [c for c in numeric if tokens(c) & _LNG and not tokens(c) & {"term", "run", "haul"}]
    out = []
    for c, bound in [(c, 90) for c in lats] + [(c, 180) for c in lngs]:
        n = _scalar(con, f"SELECT count(*) FROM {t} WHERE abs({sql.ident(c)}) > {bound}")
        if n:
            out.append(f"{c} is outside [-{bound}, {bound}] in {n:,} row(s): not a "
                       f"{'latitude' if bound == 90 else 'longitude'} anywhere on Earth.")
    for lat in lats:
        stem = _stem(lat, _LAT)
        lng = next((g for g in lngs if _stem(g, _LNG) == stem), None)
        if lng is None:
            continue
        a, b = sql.ident(lat), sql.ident(lng)
        swapped, zero = con.execute(
            f"SELECT count(*) FILTER (WHERE abs({a}) > 90 AND abs({a}) <= 180 AND abs({b}) <= 90),"
            f" count(*) FILTER (WHERE {a} = 0 AND {b} = 0) FROM {t}").fetchone()
        if swapped:
            out.append(f"{lat} and {lng} look swapped in {swapped:,} row(s): the latitude is out "
                       f"of range and the longitude would fit as one.")
        if zero:
            out.append(f"{lat}, {lng} is (0, 0) in {zero:,} row(s) -- a point in the Gulf of "
                       f"Guinea, and far more often a missing location written as zero.")
    return out


def _stem(name: str, words: set[str]) -> str:
    """'pickup_lat' -> 'pickup_', 'geolocation_lng' -> 'geolocation_': what pairs a lat with its
    lng."""
    import re
    return re.sub(r"(?i)(" + "|".join(sorted(words, key=len, reverse=True)) + r")", "", name,
                  count=1)


def _spellings(con, t: str, text: list[str], ev: DatasetEvidence) -> list[str]:
    cands = [c for c in text if 1 < ev.column(c).distinct <= 500]
    if not cands:
        return []
    exprs = ", ".join(f"count(DISTINCT {sql.ident(c)}) - count(DISTINCT lower(trim({sql.ident(c)})))"
                      for c in cands)
    merged = con.execute(f"SELECT {exprs} FROM {t}").fetchone()
    out = []
    for c, n in zip(cands, merged):
        if not n:
            continue
        q = sql.ident(c)
        rows = con.execute(
            f"SELECT lower(trim({q})) AS k, list(DISTINCT {q} ORDER BY {q}), count(*) FROM {t} "
            f"WHERE {q} IS NOT NULL GROUP BY k HAVING count(DISTINCT {q}) > 1 ORDER BY k LIMIT 3"
        ).fetchall()
        shown = "; ".join(" / ".join(repr(v) for v in vals) for _, vals, _ in rows)
        affected = sum(r[2] for r in rows)
        out.append(f"{c} writes {n:,} value(s) more than one way ({shown}"
                   f"{'; ...' if n > 3 else ''}; {affected:,} row(s) shown): every group-by counts "
                   f"the spellings apart until the Clean screen folds them.")
    return out


def _over_whole(con, t: str, ev: DatasetEvidence) -> list[str]:
    from backend.engine.contract.suggest import tokens

    rates = {"pct", "percent", "percentage", "rate", "ratio", "ctr", "cvr", "share"}
    cands = [c for c in ev.columns if _is_numeric(c.dtype) and tokens(c.name) & rates]
    out = []
    for c in cands:
        q = sql.ident(c.name)
        n, frac, pct, over1, over100, top = con.execute(
            f"SELECT count({q}), count(*) FILTER (WHERE {q} <= 1), "
            f"count(*) FILTER (WHERE {q} <= 100), count(*) FILTER (WHERE {q} > 1), "
            f"count(*) FILTER (WHERE {q} > 100), max({q}) FROM {t}").fetchone()
        if not n:
            continue
        if 0 < n - frac and frac >= 0.99 * n:
            out.append(f"{c.name} is above 1 in {over1:,} row(s) (as high as {top:g}) where every "
                       f"other value is a fraction: a part larger than its whole.")
        elif 0 < over100 and pct >= 0.99 * n and n - frac > 0.01 * n:
            out.append(f"{c.name} is above 100 in {over100:,} row(s) (as high as {top:g}): a "
                       f"percentage over 100% is a part larger than its whole -- the part and "
                       f"the whole it was computed from disagree on those rows.")
    return out


def _split(con, t: str, ev: DatasetEvidence, roles: dict) -> list[str]:
    """M10: an identifier under more than one value of a dimension, for at most 30% of the
    identifier's repeating values."""
    from backend.engine.contract.suggest import tokens
    # Named as an entity: a text column of many values is not one -- customer_age (61 values)
    # "fell under more than one channel" (v2 bench, 25/09/2026).
    # An event's identifier -- at most 3 rows each (an order's lines, a conversion's claims, a
    # user's exposure), not a person's with many: customers meeting several campaigns is no
    # anomaly (retail, 25/09/2026).
    ids = [c for c in ev.columns if tokens(c.name) & {"id", "key", "number", "no", "sku", "uuid"}
           and 1 < c.distinct < c.row_count and c.row_count <= 3 * c.distinct]
    # Text categories only: a 0/1 flag that changes over time (is_churned) splits every entity
    # it touches, and saying so is noise (v2 bench, 25/09/2026). Filled in at least half the
    # rows: a mostly-blank attribute (return_reason) describes a few lines, not the event.
    dims = [c.name for c in ev.columns if 2 <= c.distinct <= 12 and _is_text(c.dtype)
            and c.non_null >= 0.5 * c.row_count]
    out = []
    for c in ids:
        ds = [d for d in dims if d != c.name]
        if not ds:
            continue
        i = sql.ident(c.name)
        exprs = ", ".join(f"count(*) FILTER (WHERE n{k} > 1)" for k in range(len(ds)))
        inner = ", ".join(f"count(DISTINCT {sql.ident(d)}) AS n{k}" for k, d in enumerate(ds))
        row = con.execute(f"SELECT {exprs} FROM (SELECT {i}, {inner} FROM {t} GROUP BY {i})"
                          ).fetchone()
        # Against every value, not only those that repeat: the 151 users in both variants were
        # all of the users that repeat, and were missed (v2 bench, 25/09/2026).
        for d, multi in zip(ds, row):
            if 0 < multi <= 0.3 * c.distinct:
                out.append(f"{c.name} falls under more than one {d} for {multi:,} value(s) "
                           f"({multi / c.distinct:.1%} of its {c.distinct:,}): a breakdown by {d} "
                           f"counts each of them once in every {d} it falls under.")
    return out


def _future(con, t: str, ev: DatasetEvidence) -> list[str]:
    """M11: dates after today."""
    out = []
    for c in ev.columns:
        if not _is_date(c.dtype):
            continue
        q = sql.ident(c.name)
        n, top = con.execute(f"SELECT count(*), max({q}) FROM {t} WHERE CAST({q} AS DATE) > "
                             f"current_date").fetchone()
        if n:
            out.append(f"{c.name} is in the future in {n:,} row(s) (as late as "
                       f"{str(top)[:10]}): after today, a typo or a placeholder more often than "
                       f"a plan.")
    return out


def _currencies(con, t: str, ev: DatasetEvidence, table: str) -> list[str]:
    """M12: money columns beside a currency column with several codes, not named converted."""
    from backend.engine.contract.suggest import _CONVERTED, _MONEY, currency_codes, tokens
    found = currency_codes(con, table, ev)
    if not found:
        return []
    col, codes = found
    out = []
    from backend.engine.contract.suggest import _RATIO_WORDS
    for c in ev.columns:
        w = tokens(c.name)
        # Not a percentage: discount_pct is no money (v2 bench, 25/09/2026).
        if _is_numeric(c.dtype) and w & _MONEY and not w & (_CONVERTED | _RATIO_WORDS):
            out.append(f"{c.name} mixes {len(codes)} currencies ({col}: {', '.join(codes)}): a "
                       f"total across them adds unlike money -- group by {col}, or use a "
                       f"converted column.")
    return out


def _is_date(dtype: str) -> bool:
    """A calendar date or timestamp, not a time of day: a TIME column neither casts to DATE nor
    compares with one (stress round 4, time_only_column, 26/09/2026: M11 and M13 raised)."""
    base = dtype.upper().split("(")[0].strip()
    return _is_temporal(dtype) and base not in ("TIME", "TIME WITH TIME ZONE", "TIMETZ")


def _date_order(con, t: str, ev: DatasetEvidence) -> list[str]:
    """M13: of two dates, the one that follows the other in 99% of rows where both exist, and
    the few rows where it comes first."""
    # A time of day is not a date: DATE < TIME is a binder error (stress round 4, time_only_column,
    # 26/09/2026), and "before" means nothing between them.
    dates = [c.name for c in ev.columns if _is_date(c.dtype)]
    out = []
    for k, a in enumerate(dates):
        for b in dates[k + 1:]:
            qa, qb = sql.ident(a), sql.ident(b)
            both, a_first, b_first = con.execute(
                f"SELECT count(*), count(*) FILTER (WHERE {qa} < {qb}), "
                f"count(*) FILTER (WHERE {qb} < {qa}) FROM {t} "
                f"WHERE {qa} IS NOT NULL AND {qb} IS NOT NULL").fetchone()
            if not both:
                continue
            for late, early, wrong, right in ((b, a, b_first, a_first), (a, b, a_first, b_first)):
                if 0 < wrong <= 0.01 * both and right >= 0.9 * both:
                    out.append(f"{late} is before {early} in {wrong:,} row(s), where in every "
                               f"other row that has both it comes after.")
    return out


def measure(con, table: str, ev: DatasetEvidence, *, date_column: str | None = None,
            roles: dict[str, str] | None = None) -> list[str]:
    """Every rule above over one table, in rule order, at most MAX_LINES lines."""
    t = sql.ident(table)
    roles = roles or {}
    text = [c.name for c in ev.columns if _is_text(c.dtype)]
    numeric = [c.name for c in ev.columns if _is_numeric(c.dtype)
               and roles.get(c.name) not in ("identifier",)]
    explain = [c.name for c in ev.columns
               if 1 < c.distinct <= EXPLAIN_MAX_DISTINCT
               and (roles.get(c.name) in ("dimension", "flag") or c.dtype.upper() == "BOOLEAN")]
    counts = _text_counts(con, t, text)
    # What makes a number wrong first, what describes a column last: the cap cut the newest
    # rules on the retail fixture, and said the rest were in the profile -- they were not
    # (v2 bench, 25/09/2026).
    errors = (_placeholders(con, t, counts, ev.row_count)
              + _negatives(con, t, numeric, ev)
              + _over_whole(con, t, ev)
              + _date_order(con, t, ev)
              + _future(con, t, ev)
              + _currencies(con, t, ev, table)
              + _split(con, t, ev, roles)
              + _text_dates(con, t, ev, text)
              + _spellings(con, t, text, ev)
              + _coordinates(con, t, ev)
              + _subtotals(con, t, counts)
              + _empty_months(con, t, date_column, ev))
    blanks = _blanks(con, t, ev, counts, explain)
    lines = errors + blanks
    if len(lines) > MAX_LINES:
        cut = lines[MAX_LINES:]
        lines = lines[:MAX_LINES]
        if all(line in blanks for line in cut):
            lines.append(f"{len(cut)} more column(s) have blanks; profile_dataset's table counts "
                         f"every column's.")
        else:
            lines.append(f"{len(cut)} more caveat(s) are not listed here: the list stops at "
                         f"{MAX_LINES}.")
    return lines


def for_table(con, table: str, date_column: str | None = None) -> list[str]:
    """measure() from scratch, for a caller holding only a table name: confirmation recounts
    rather than storing what a JSON payload says was counted."""
    from backend.engine.contract.evidence import gather, suggest_role

    ev = gather(con, table, probe_pairs=False)
    roles = {c.name: suggest_role(c)[0] for c in ev.columns}
    return measure(con, table, ev, date_column=date_column, roles=roles)


__all__ = ["PLACEHOLDERS", "SUBTOTALS", "for_table", "measure"]
