"""Pack schema. A pack is knowledge, never code: no SQL strings, no expressions (checked)."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MeasureType = Literal["additive", "ratio_of_sums", "weighted_mean", "distinct_count",
                      "percentile", "semi_additive", "derived"]
TemplateShape = Literal["comparison", "ratio_of_sums", "weighted_mean", "distinct_count",
                        "percentile", "semi_additive", "derived"]
_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_SQL = re.compile(r"\b(select|insert|update|delete|drop|create|alter|union|from\s+\w+\s+where)\b",
                  re.I)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _no_sql(v: str) -> str:
    if _SQL.search(v or ""):
        raise ValueError(f"packs never contain SQL: {v[:60]!r}")
    return v


class Locale(Strict):
    currency: str = "INR"
    fiscal_year_start: str = "04-01"
    timezone: str = "Asia/Kolkata"


class PackHeader(Strict):
    id: str
    version: str
    extends: list[str] = []
    title: str = ""
    locale: Locale = Locale()


class Concept(Strict):
    """A thing a column can be. Detected from name hints (token match) and value patterns."""
    id: str
    hints: list[str] = Field(description="Lower-case name tokens or joined names")
    value_patterns: list[str] = []
    weight: float = 1.0
    measure_type: MeasureType | None = None


class Source(Strict):
    id: str
    title: str
    concepts: list[str] = Field(description="Concepts whose presence signals this source")
    required: list[str] = Field([], description="Concepts that must all be present")
    min_score: float = 0.5
    signals_domain: bool = Field(True, description="False: supports the domain's "
                                 "analyses but does not by itself say the file is this domain")


class Option(Strict):
    id: str
    label: str


class Fork(Strict):
    id: str
    question: str
    options: list[Option]
    suggested: str | None = None
    suggested_reason: str | None = None
    applies_when: list[str] = Field([], description="Concepts that make this fork relevant")

    @model_validator(mode="after")
    def _suggestion_is_an_option(self):
        ids = {o.id for o in self.options}
        if self.suggested is not None and self.suggested not in ids:
            raise ValueError(f"fork {self.id}: suggested {self.suggested!r} is not an option")
        if (self.suggested is None) != (self.suggested_reason is None):
            raise ValueError(f"fork {self.id}: a suggestion needs a reason, and vice versa")
        return self


class MetricTemplate(Strict):
    id: str
    label: str
    shape: TemplateShape
    numerator: str | list[str] | None = Field(None, description="Concept ids; '-gst' subtracts")
    denominator: str | list[str] | None = None
    weight: str | None = None
    concept: str | None = None
    as_of: str | None = None
    required_concepts: list[str]
    forks: list[str] = []
    authority: str = ""
    source_type: Literal["standard", "platform_doc", "vendor_blog"] = "standard"
    scale: float = 1.0
    variants_by: str | None = Field(None, description="A fork whose answer picks a variant")
    variants: dict[str, dict] = Field({}, description="fork option -> {numerator, "
                                      "denominator} overrides, or {unsupported: reason}")
    engine_ready: bool = Field(True, description="False: the engine cannot compute this shape "
                               "yet; approval is refused with the reason")
    # comparison (B8): 1 where `concept op right` (or `concept op value`), else 0; agg mean = rate
    op: Literal[">", ">=", "<", "<=", "=", "<>"] | None = None
    right: str | None = Field(None, description="comparison: the concept on the right")
    value: float | None = Field(None, description="comparison: a number on the right")
    agg: Literal["mean", "sum"] = "mean"
    definition: str = Field("", description="comparison: what 1 means, in words")

    @model_validator(mode="after")
    def _shape_fields(self):
        need = {"ratio_of_sums": ("numerator", "denominator"),
                "weighted_mean": ("concept", "weight"),
                "distinct_count": ("concept",), "percentile": ("concept",),
                "semi_additive": ("concept", "as_of"), "derived": ("numerator", "denominator"),
                "comparison": ("concept", "op", "definition")}[self.shape]
        missing = [f for f in need if getattr(self, f) is None]
        if missing:
            raise ValueError(f"template {self.id} ({self.shape}) needs {missing}")
        if self.shape == "comparison" and (self.right is None) == (self.value is None):
            raise ValueError(f"template {self.id}: compare with a concept or a value, not both")
        return self


class ValidityRule(Strict):
    id: str
    description: str
    kind: Literal["exclude_matching", "flag_rows", "require_positive", "flag_zero",
                  "require_after"]
    concept: str
    pattern: str | None = Field(None, description="Lower-case words joined by |; no regex "
                                "syntax beyond alternation")
    other: str | None = Field(None, description="flag_zero: the concept that is zero; "
                              "require_after: the start timestamp `concept` must follow")
    applies_to: list[str] = Field([], description="Metric templates this rule guards; empty "
                                  "= every tool run on the dataset")

    _chk = field_validator("description")(lambda cls, v: _no_sql(v))

    @field_validator("pattern")
    @classmethod
    def _plain_pattern(cls, v):
        if v is not None and not re.fullmatch(r"[a-z0-9 _|\[\]?-]+", v):
            raise ValueError(f"pattern {v!r}: plain words joined by | only")
        return v


class InterpretationRule(Strict):
    id: str
    description: str
    params: dict = {}


class PlaybookStep(Strict):
    tool: str
    params: dict = Field({}, description="Literals, or '@slot:<name>' filled by the planner")
    optional: bool = Field(False, description="Skipped (and said) when it cannot run")


class Playbook(Strict):
    id: str
    description: str = Field(max_length=200)
    patterns: list[str] = Field(description="Question words that suggest it (planner hint)")
    slots: dict[str, str] = Field({}, description="slot -> what the planner must fill")
    steps: list[PlaybookStep]
    rules: list[str] = Field([], description="Interpretation rules always enforced")
    max_tool_calls: int = 6
    # boundary-aware procedure (B9, concept 9): what must hold before it runs, and what to do
    requires_metrics: list[str] = Field([], description="Metric templates that must be approved")
    requires_concepts: list[str] = Field([], description="Concepts the data must hold")
    recovery: str = Field("", max_length=300, description="What the person does when a "
                          "requirement is missing or the lead step fails")


class Step(Strict):
    analysis: str
    params: dict = {}
    optional: bool = Field(False, description="Skipped (and said so) when a binding is missing")
    filter: dict | list[dict] | None = Field(None, description="Structured row filter(s): "
                                             "{concept, between: [lo, hi]} | {concept, "
                                             "terms_param, negate} | {concept, date_range} | "
                                             "{concept, exclude_truthy: true} | {concept, "
                                             "keep_matching: words} | {concept, compare: op, "
                                             "other} | {concept, focus_param, worst_by} | "
                                             "{concept, older_than: {as_of_param, days_param}}")
    title: str = ""


class ParamSpec(Strict):
    """An optional parameter a person may set (B10, issue #23). Declared, never guessed."""
    name: str
    kind: Literal["text", "number", "date", "month", "list", "boolean", "column", "json"]
    help: str = Field(max_length=200)
    default: str | None = Field(None, description="What the tool uses when it is not given; "
                                "None: the tool decides and says so (e.g. the worst group)")


class Tool(Strict):
    id: str
    kind: Literal["preset", "pipeline"] = Field("preset", description="pipeline: a service "
                                                "run (keyword grouping), not engine steps")
    description: str = Field(max_length=200)
    ui_label: str
    base_analysis: str
    preset: dict = {}
    steps: list[Step] = []
    slots: dict[str, list[str]] = Field({}, description="slot -> candidate concepts, first "
                                        "bound wins; the person may override per run")
    params_required: list[str] = []
    params_optional: list[ParamSpec] = []
    required_concepts: list[str] = []
    required_sources: list[str] = []
    forks: list[str] = []
    rules: list[str] = []
    output: dict = {}

    @field_validator("preset")
    @classmethod
    def _preset_has_no_sql(cls, v: dict) -> dict:
        for x in v.values():
            if isinstance(x, str):
                _no_sql(x)
        return v


class DqCheck(Strict):
    id: str
    description: str


class Privacy(Strict):
    pii_classes: dict[str, list[str]] = {}
    min_group_size: int = 5


class Citation(Strict):
    id: str
    title: str
    url: str


class ChangelogEntry(Strict):
    date: str
    title: str
    detail: str
    source: str
    affects: list[str] = []


class Festival(Strict):
    id: str
    name: str
    dates: dict[str, list[str]] = Field(description="year -> [start, end] ISO dates")
    confirm_yearly: bool = True


class Pack(Strict):
    pack: PackHeader
    sources: list[Source] = []
    vocabulary: list[Concept] = []
    word_classes: dict[str, list[str]] = Field({}, description="Name-token classes the "
                                               "measure suggester reads (engine suggest.py)")
    measure_types: dict[str, str] = {}
    metric_templates: list[MetricTemplate] = []
    forks: list[Fork] = []
    validity_rules: list[ValidityRule] = []
    interpretation_rules: list[InterpretationRule] = []
    playbooks: list[Playbook] = []
    tools: list[Tool] = []
    dq_checks: list[DqCheck] = []
    privacy: Privacy = Privacy()
    festivals: list[Festival] = []
    value_aliases: dict[str, str] = Field({}, description="What people say -> what data says "
                                          "(lower-case): bombay -> mumbai (B9 concept 14)")
    sources_cited: list[Citation] = []
    changelog: list[ChangelogEntry] = []

    @model_validator(mode="after")
    def _ids(self):
        pid = self.pack.id
        if not _ID.match(pid):
            raise ValueError(f"pack id {pid!r} is not snake_case")
        for t in self.tools:
            if not t.id.startswith(pid + "."):
                raise ValueError(f"tool {t.id} must be namespaced {pid}.<name>")
        for kind, items in (("concept", self.vocabulary), ("fork", self.forks),
                            ("template", self.metric_templates), ("rule", self.validity_rules),
                            ("tool", self.tools), ("source", self.sources)):
            ids = [i.id for i in items]
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate {kind} ids in {pid}")
        return self
