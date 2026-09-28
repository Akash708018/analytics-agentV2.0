"""Run a domain tool: a preset over engine analyses. No new maths lives here.

Resolve (concepts -> contract columns, approved metrics, slots, params) -> compile the
person's approved validity rules and the step's structured filter into the engine's guarded
`where=` -> run each step through the engine (`_produce`: contract gate, scope, refusals) ->
assemble a ToolResult with provenance, chart series and caveats.

SQL is written here only from structure: quoted identifiers, validated words and numbers. The
pack supplies no SQL and the LLM supplies none.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from backend.engine.analysis.tools import _produce, _Refused
from backend.packs.loader import Merged
from backend.packs.models import Tool
from backend.services.sessions import ServiceError

MAX_FIGURES_PER_STEP = 25
_TERM = re.compile(r"^[a-z0-9][a-z0-9 &'.-]{0,40}$")
_NUM = re.compile(r"^-?[\d,]*\.?\d+%?$")
# concepts whose columns must never be added across sources (no_cross_source_conversion_sum)
CONVERSION_FAMILY = {"conversions", "conv_value", "key_events"}


def q(col: str) -> str:
    return '"' + col.replace('"', '""') + '"'


def num(v):
    """A formatted engine cell as a number where it is one ('1,234.5', '30.2%'); else as is."""
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str) and _NUM.match(v.strip()):
        s = v.strip().replace(",", "")
        return float(s[:-1]) if s.endswith("%") else float(s)
    return v if isinstance(v, str) or v is None else str(v)


@dataclass
class Ctx:
    con: object
    workspace_id: str
    table: str
    merged: Merged
    bound: dict[str, list[str]]         # concept -> columns of the table
    measures: set[str]                  # contract measure names
    metrics: dict[str, str]             # template id -> contract measure name
    fork_choices: dict[str, str]
    validity: list[str]                 # approved rule ids
    window: tuple[date, date] | None
    festivals: dict
    params: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    used_concepts: set[str] = field(default_factory=set)
    used_metrics: set[str] = field(default_factory=set)
    filters_applied: list[str] = field(default_factory=list)
    spans: list = field(default_factory=list)


class Skip(Exception):
    pass


def column_for(ctx: Ctx, concept: str) -> str:
    override = (ctx.params.get("bindings") or {}).get(concept)
    cols = [override] if override else ctx.bound.get(concept, [])
    if not cols:
        raise Skip(f"no column is bound to '{concept}'")
    if len(cols) > 1:
        rule = (" Conversions from different sources overlap and are never added "
                "(no_cross_source_conversion_sum)." if concept in CONVERSION_FAMILY else "")
        raise ServiceError(422, "ambiguous_binding",
                           f"{len(cols)} columns could be '{concept}': {cols}. Pick one with "
                           f"params.bindings.{concept}.{rule}",
                           {"concept": concept, "columns": cols})
    ctx.used_concepts.add(concept)
    return cols[0]


def _slot(ctx: Ctx, tool: Tool, name: str) -> str:
    if name in ctx.params and isinstance(ctx.params[name], str):
        return ctx.params[name]               # the person's choice of column
    for concept in tool.slots.get(name, []):
        if ctx.bound.get(concept):
            return column_for(ctx, concept)
    raise Skip(f"none of {tool.slots.get(name)} is in this data (slot '{name}')")


def _festival(ctx: Ctx, which: str) -> str:
    fid, year = ctx.params.get("festival"), str(ctx.params.get("year", ""))
    f = ctx.festivals.get(fid)
    if f is None:
        raise ServiceError(422, "unknown_festival", f"festival {fid!r}; known: "
                           f"{sorted(ctx.festivals)}")
    y = year if which == "this" else str(int(year) - 1)
    if y not in f.dates:
        raise ServiceError(422, "festival_dates_missing",
                           f"{f.name} has no dates stored for {y}; add them to the core pack")
    if ctx.params.get("dates_confirmed") is not True:
        raise ServiceError(422, "festival_dates_unconfirmed",
                           "Festival dates move every year: confirm them, then re-run with "
                           "params.dates_confirmed = true.",
                           {"dates": {k: f.dates.get(k) for k in (year, str(int(year) - 1))}})
    s, e = f.dates[y]
    return f"{s}/{e}"


def resolve(ctx: Ctx, tool: Tool, value):
    if not (isinstance(value, str) and value.startswith("@")):
        return value
    ref = value[1:]
    if ref.startswith("metric:"):
        tid = ref.split(":", 1)[1]
        if tid not in ctx.metrics:
            raise Skip(f"metric '{tid}' is not approved for this dataset "
                       f"(POST /datasets/{{id}}/metrics/approve)")
        ctx.used_metrics.add(tid)
        tpl = ctx.merged.templates.get(tid)
        if tpl is not None:
            ctx.used_concepts.update(tpl.required_concepts)
        return ctx.metrics[tid]
    if ref.startswith("param?:"):
        return ctx.params.get(ref.split(":", 1)[1])        # None: the analysis's own default
    if ref.startswith("param:"):
        k, _, default = ref.split(":", 1)[1].partition("=")
        if k not in ctx.params:
            if default:
                return int(default) if default.isdigit() else default
            raise ServiceError(422, "param_required", f"this tool needs params.{k}")
        return ctx.params[k]
    if ref.startswith("festival:"):
        return _festival(ctx, ref.split(":", 1)[1])
    if ref == "revenue":
        basis = ctx.fork_choices.get("roas_revenue_basis", "platform")
        if basis == "net_excl_gst":
            ctx.notes.append("Revenue change is decomposed on order revenue INCLUDING GST: the "
                             "contract holds no net-revenue column (net exists only inside the "
                             "ROAS ratio). ROAS figures above are net of GST, as chosen.")
        return column_for(ctx, "conv_value" if basis == "platform" else "order_revenue")
    if ref in tool.slots:
        return _slot(ctx, tool, ref)
    return column_for(ctx, ref)


def _terms(ctx: Ctx, key: str) -> list[str]:
    raw = ctx.params.get(key)
    terms = [t.strip().lower() for t in (raw if isinstance(raw, list) else
                                         str(raw or "").split(",")) if str(t).strip()]
    bad = [t for t in terms if not _TERM.match(t)]
    if not terms or bad:
        raise ServiceError(422, "bad_terms", f"params.{key} must be plain words/phrases "
                           f"(letters, digits, spaces, & ' . -); rejected: {bad or 'empty'}")
    return terms


def compile_filters(ctx: Ctx, tool: Tool, step) -> list[str]:
    preds = []
    for rid in ctx.validity:
        r = ctx.merged.validity_rules.get(rid)
        if r is None:
            continue
        if r.applies_to and not (set(r.applies_to) & ctx.used_metrics):
            continue
        try:
            col = column_for(ctx, r.concept)
        except Skip:
            continue
        if r.kind == "exclude_matching":
            preds.append(f"NOT coalesce(regexp_matches(lower(CAST({q(col)} AS VARCHAR)), "
                         f"'{r.pattern}'), false)")
        elif r.kind == "require_positive":
            preds.append(f"{q(col)} > 0")
        else:
            continue            # flag rules never filter; see flags()
        if rid not in ctx.filters_applied:
            ctx.filters_applied.append(rid)
    for f in ([step.filter] if isinstance(step.filter, dict) else step.filter or []):
        preds += _step_filter(ctx, tool, f)
    return preds


def _step_filter(ctx: Ctx, tool: Tool, f: dict) -> list[str]:
    preds = []
    if True:
        concept = f["concept"]
        if f.get("exclude_truthy"):
            try:
                col = column_for(ctx, concept)
            except Skip:
                ctx.notes.append(f"No '{concept}' column: those rows could not be removed, so "
                                 f"the figures still include them.")
                return []
            preds.append(f"NOT coalesce(lower(trim(CAST({q(col)} AS VARCHAR))) IN "
                         f"('1', 'true', 't', 'yes', 'y'), false)")
            if f"exclude:{concept}" not in ctx.filters_applied:
                ctx.filters_applied.append(f"exclude:{concept}")
            return preds
        col = _slot(ctx, tool, concept[1:]) if concept.startswith("@") else column_for(
            ctx, concept)
        if "between" in f:
            lo, hi = (float(x) for x in f["between"])
            preds.append(f"{q(col)} BETWEEN {lo:g} AND {hi:g}")
        if "date_range" in f:
            lo, hi = resolve(ctx, tool, f["date_range"]).split("/")
            lo, hi = date.fromisoformat(lo), date.fromisoformat(hi)
            ctx.spans.append((lo, hi))
            preds.append(f"CAST({q(col)} AS DATE) BETWEEN DATE '{lo}' AND DATE '{hi}'")
        if "terms_param" in f:
            alt = "|".join(re.escape(t).replace("'", "''") for t in _terms(ctx, f["terms_param"]))
            p = f"coalesce(regexp_matches(lower(CAST({q(col)} AS VARCHAR)), '({alt})'), false)"
            preds.append(f"NOT {p}" if f.get("negate") else p)
    return preds


def flags(ctx: Ctx) -> list[str]:
    out = []
    for rid in ctx.validity:
        r = ctx.merged.validity_rules.get(rid)
        if r is None or r.kind not in ("flag_zero", "flag_rows"):
            continue
        try:
            col = column_for(ctx, r.concept)
            if r.kind == "flag_zero":
                other = column_for(ctx, r.other)
                n = ctx.con.execute(f"SELECT count(*) FROM {q(ctx.table)} WHERE "
                                    f"{q(col)} > 0 AND coalesce({q(other)}, 0) = 0").fetchone()[0]
            else:
                n = ctx.con.execute(
                    f"SELECT count(*) FROM {q(ctx.table)} WHERE regexp_matches("
                    f"lower(CAST({q(col)} AS VARCHAR)), '{r.pattern}')").fetchone()[0]
        except Skip:
            continue
        if n:
            out.append(f"{r.id}: {n} row(s) flagged, not dropped -- {r.description}")
            ctx.filters_applied.append(rid)
    return out


def _period_bounds(value: str) -> tuple[date, date] | None:
    """A calendar label's span: '2026-01' (month), '2026-01-05' (day), 'a/b' (range)."""
    import calendar
    try:
        if "/" in value:
            a, b = value.split("/")
            return date.fromisoformat(a), date.fromisoformat(b)
        if len(value) == 7:
            y, m = int(value[:4]), int(value[5:])
            return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
        d = date.fromisoformat(value)
        return d, d
    except (ValueError, AttributeError):
        return None


def context_caveats(ctx: Ctx, spans: list[tuple[date, date]]) -> list[str]:
    """festival_confound and measurement_change: said whenever a period touches one."""
    out = []
    spans = spans or ([ctx.window] if ctx.window else [])
    for f in ctx.festivals.values():
        for y, (s, e) in f.dates.items():
            fs, fe = date.fromisoformat(s), date.fromisoformat(e)
            if any(fs <= b and a <= fe for a, b in spans):
                out.append(f"festival_confound: {f.name} ({s} to {e}) falls in the period; "
                           f"a change may be the festival, not the marketing.")
    for c in ctx.merged.changelog:
        d = date.fromisoformat(c.date)
        if any(a <= d <= b for a, b in spans) and (not c.affects or set(c.affects) &
                                                   ctx.used_concepts):
            out.append(f"measurement_change: {c.title} on {c.date} -- {c.detail} "
                       f"(source: {c.source})")
    return out


def _value_index(headers: list[str], rows: list[list]) -> int | None:
    if "total" in headers:
        return headers.index("total")
    for i in range(1, len(headers)):
        if headers[i] in ("n", "nulls", "rank", "rows"):
            continue
        if rows and all(isinstance(num(r[i]), (int, float)) for r in rows[:5] if r[i] is not None):
            return i
    return None


def run(ctx: Ctx, tool: Tool, min_group: int) -> dict:
    missing = [p for p in tool.params_required if p not in ctx.params]
    if missing:
        raise ServiceError(422, "param_required", f"{tool.id} needs params {missing}",
                           {"missing": missing})
    unanswered = [f for f in tool.forks if f in ctx.merged.forks and f not in ctx.fork_choices
                  and _fork_relevant(ctx, f)]
    if unanswered:
        forks = [{"fork_id": f, "question": ctx.merged.forks[f].question} for f in unanswered]
        raise ServiceError(422, "forks_unanswered", "Answer these before this tool runs; none "
                           "is defaulted.", {"missing": unanswered, "forks": forks})
    figures, series, caveats, spans, ran = [], [], [], [], 0
    caveats += flags(ctx)
    for step in tool.steps:
        title = step.title or step.analysis
        try:
            params = {k: resolve(ctx, tool, v) for k, v in step.params.items()}
            params = {k: v for k, v in params.items() if v is not None}
            preds = compile_filters(ctx, tool, step)
        except Skip as e:
            if not step.optional:
                raise ServiceError(422, "needs_data", f"{title}: {e}") from None
            caveats.append(f"{title}: skipped -- {e}")
            continue
        for k in ("period", "baseline"):
            if isinstance(params.get(k), str) and (b := _period_bounds(params[k])):
                spans.append(b)
        spans += ctx.spans
        ctx.spans = []
        if preds:
            params["where"] = " AND ".join(f"({p})" for p in preds)
        try:
            _, out, _ = _produce(ctx.con, ctx.table, step.analysis, params, ctx.workspace_id)
        except _Refused as e:
            text = str(e)
            if step.optional:
                caveats.append(f"{title}: the engine refused -- {text.splitlines()[0]}")
                continue
            raise ServiceError(422, "engine_refused", text) from None
        ran += 1
        provisional = any("PROVISIONAL" in s for s in out.summary)
        caveats += [s for s in out.summary[1:] if s not in caveats]
        vi = _value_index(out.headers, out.rows)
        ni = out.headers.index("n") if "n" in out.headers else None
        pts = []
        for r in out.rows[:MAX_FIGURES_PER_STEP]:
            label = str(r[0])
            value = num(r[vi]) if vi is not None else None
            if ni is not None and isinstance(num(r[ni]), (int, float)) and num(r[ni]) < min_group:
                caveats.append(f"{title}: '{label}' has {int(num(r[ni]))} row(s), under the "
                               f"minimum group size {min_group}; its figure is suppressed.")
                value = None
            prov = "provisional" if provisional else "contract"
            figures.append({"name": f"{title}: {label}", "value": value,
                            "unit": out.headers[vi] if vi is not None else None,
                            "provenance": prov})
            # every other numeric cell is a figure too, named by its column (a cohort row carries
            # CAC, payback and LTV:CAC beside its size); suppressed rows stay suppressed
            for ci in range(1, len(out.headers)):
                if ci in (vi, ni) or out.headers[ci] in ("nulls", "rank", "rows"):
                    continue
                cell = num(r[ci])
                if isinstance(cell, (int, float)):
                    figures.append({"name": f"{title}: {label} [{out.headers[ci]}]",
                                    "value": None if value is None and ni is not None else cell,
                                    "unit": out.headers[ci], "provenance": prov})
            if isinstance(value, (int, float)) or value is None:
                pts.append({"x": label, "y": value})
        chart = "line" if step.analysis in ("trend", "changepoint") else \
            tool.output.get("chart", "bar")
        if chart not in ("line", "bar", "stacked_bar", "scatter", "table", "funnel"):
            chart = "bar"
        series.append({"chart": chart, "name": title, "x_label": out.headers[0],
                       "y_label": out.headers[vi] if vi is not None else "", "points": pts})
    for check in tool.output.get("checks", []):
        found = dq_check(ctx, check)
        if found:
            caveats.append(found)
    if not ran:
        raise ServiceError(422, "needs_data", f"{tool.id}: no step could run -- "
                           + "; ".join(caveats[-3:]))
    caveats += ctx.notes + context_caveats(ctx, spans)
    interp = [r for r in tool.rules if r in ctx.merged.interpretation_rules]
    return {"tool_id": tool.id, "summary": f"{tool.ui_label}: {ran} step(s) run",
            "figures": figures, "series": series,
            "validity_filters_applied": ctx.filters_applied,
            "pack_rules_applied": interp,
            "forks": {f: ctx.fork_choices[f] for f in tool.forks if f in ctx.fork_choices},
            "caveats": caveats, "figure_check": {"status": "not_run", "notes": [
                "figures come straight from the engine; the figure check runs on LLM text (B5)"]}}


def _fork_relevant(ctx: Ctx, fork_id: str) -> bool:
    f = ctx.merged.forks[fork_id]
    return not f.applies_when or any(ctx.bound.get(c) for c in f.applies_when)


def dq_check(ctx: Ctx, check: str) -> str | None:
    """Tagging checks, in SQL over bound columns. Returns a finding, or None."""
    T = q(ctx.table)
    try:
        src = column_for(ctx, "session_source")
    except Skip:
        return None
    if check == "case_variants":
        rows = ctx.con.execute(
            f"SELECT lower(trim(CAST({q(src)} AS VARCHAR))) k, "
            f"list(DISTINCT CAST({q(src)} AS VARCHAR) ORDER BY CAST({q(src)} AS VARCHAR)), "
            f"count(*) FROM {T} WHERE {q(src)} IS NOT NULL GROUP BY 1 "
            f"HAVING count(DISTINCT CAST({q(src)} AS VARCHAR)) > 1 ORDER BY 3 DESC").fetchall()
        if rows:
            return ("utm case_variants: " + "; ".join(f"{v} are one source ({n} rows)"
                                                      for _, v, n in rows[:10]))
    if check == "not_set":
        n = ctx.con.execute(f"SELECT count(*) FROM {T} WHERE lower(trim(CAST({q(src)} AS "
                            f"VARCHAR))) IN ('(not set)', 'not set', '(none)', '')").fetchone()[0]
        if n:
            return f"utm not_set: {n} row(s) have source (not set) or empty"
    if check == "missing_medium":
        try:
            med = column_for(ctx, "session_medium")
        except Skip:
            return "utm missing_medium: there is no medium column at all"
        n = ctx.con.execute(f"SELECT count(*) FROM {T} WHERE {q(med)} IS NULL OR "
                            f"lower(trim(CAST({q(med)} AS VARCHAR))) IN "
                            f"('', '(not set)', '(none)')").fetchone()[0]
        if n:
            return f"utm missing_medium: {n} row(s) have no medium"
    return None
