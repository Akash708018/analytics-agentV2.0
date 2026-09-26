"""The contract between the Track B user interface and this engine.

Phase 14, guide line 984: the Streamlit UI is "the same spec object, different front door". This
file is that door's frame, written before either side so neither can drift from it. The UI is
built against `Backend` with a fake behind it; the engine supplies the real one. Integrating the
two is swapping one object, and nothing in the UI changes.

Rules the shapes enforce:

- **The UI holds no logic.** Every decision -- what a header is, whether a contract may be
  confirmed, what a number means -- comes back from the backend. The UI renders it.
- **A refusal is data, not a string to parse.** Every call that can be refused returns its
  result object with `refusal` set. `Refusal.next_step` is the call that unblocks it; show it.
- **Plain types only.** str, int, float, bool, None, list, dict and the dataclasses below. No
  pandas, no numpy, no engine objects: the UI must be buildable without importing the engine.
- **Every call takes the workspace id first.** One per browser session, from
  `Backend.new_workspace_id()`, kept for the life of the session. It is a directory name on the
  server side and is validated there.

Standard library only, so importing this file costs the UI nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

#: How a measure may be combined. "none" means per-row only: summing a unit price means nothing.
AGGREGATIONS: tuple[str, ...] = (
    "sum", "mean", "median", "min", "max", "count", "count_distinct", "none",
)

#: How several header rows are joined into one column name.
HEADER_JOINS: tuple[str, ...] = ("space", "underscore", "bottom_only", "top_only")

#: Column types an Ingest Spec may force. None in a ColumnDraft means "let the loader decide".
DTYPES: tuple[str, ...] = ("VARCHAR", "BIGINT", "DOUBLE", "DATE", "TIMESTAMP", "BOOLEAN")

#: The role a column plays in a Dataset Contract.
ROLES: tuple[str, ...] = ("key", "date", "measure", "dimension", "ignore")

Verdict = Literal["OK", "WARN", "REFUSE"]
ArtifactKind = Literal["chart", "report", "result"]


@dataclass(frozen=True)
class Refusal:
    """Why something did not happen, and the one call that fixes it.

    `reason` is a stable code (NO_CONTRACT, DATASET_NOT_LOADED, ANALYSIS_NOT_POSSIBLE, ...).
    `text` is the whole refusal as the engine wrote it, for a details expander.
    """

    reason: str
    what: str
    why: str
    next_step: str
    text: str


@dataclass(frozen=True)
class Limits:
    """What this server will accept. The UI checks before sending; the backend checks again."""

    csv_max_bytes: int
    excel_max_bytes: int
    postgres_max_rows: int
    description: str  # the engine's own table of limits, Markdown


@dataclass(frozen=True)
class UploadResult:
    """A file saved into the workspace -- or not. `path` is what later calls take."""

    verdict: Verdict
    message: str
    path: str | None = None
    refusal: Refusal | None = None


@dataclass(frozen=True)
class GridPreview:
    """The top of a file exactly as it sits on the sheet, before anything is interpreted.

    rows[i] is sheet row `first_row_number + i` (1-indexed, as a person counts them). Cells are
    strings or None for empty; every row has the same length. merged_ranges are Excel ranges
    such as "B1:D1" -- draw them as spanning cells if you can, list them if you cannot.
    """

    rows: list[list[str | None]]
    first_row_number: int
    sheet: str | None
    sheet_names: list[str]
    merged_ranges: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ColumnDraft:
    """One column: its name on the sheet, the name it will have, and a forced type if any."""

    source_name: str
    target_name: str
    dtype: str | None = None


@dataclass(frozen=True)
class IngestDraft:
    """How the engine proposes to read a file, for the Ingest Spec editor.

    `spec` is the complete spec as a dict. The editor changes the fields a person can change
    (header_rows, data_start_row, footer_skip_rows, header_join, dataset_name, sheet, and each
    column's target_name and dtype), leaves every other key exactly as it came, and sends the
    dict back to `confirm_ingest`. The typed fields beside it are the same values, for display.

    While `unresolved` is non-empty the spec is PROVISIONAL and cannot be confirmed: show
    `questions`, and call `draft_ingest` again with the person's answers.
    """

    spec: dict
    grid: GridPreview
    dataset_name: str
    header_rows: list[int]
    data_start_row: int
    footer_skip_rows: int
    header_join: str
    columns: list[ColumnDraft]
    assumptions: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    message: str = ""
    refusal: Refusal | None = None


@dataclass(frozen=True)
class ActionResult:
    """The outcome of anything that changes state. `message` is Markdown to show as-is."""

    ok: bool
    message: str
    refusal: Refusal | None = None


@dataclass(frozen=True)
class ContractColumn:
    """A column as the contract form shows it, with what the data suggested about it."""

    name: str
    dtype: str
    suggested_role: str  # one of ROLES
    distinct_count: int | None = None
    null_count: int | None = None
    sample_values: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MeasureSuggestion:
    """How the engine reads one measure (contract/suggest.py): never applied by drafting.

    `strength` is strong, likely or unsure. Only a strong one may be filled into the form, and
    only by the person's click; a likely or unsure one comes with `question`. `ratio` is a ratio
    of sums to ADD beside the column, as {name, numerator, denominator, scale, definition}.
    """

    agg: str | None
    strength: str
    reason: str
    rule: str = ""
    per: list[str] = field(default_factory=list)
    question: str = ""
    ratio: dict | None = None


@dataclass(frozen=True)
class FieldSource:
    """Who decided one field of a drafted contract, and why (contract/llm_filter.py).

    `status`: agree (the model and the data), llm_only (the data cannot check it), overruled (the
    data replaced the model's choice, kept in `llm`), blocked, data (the data alone), or user.
    """

    status: str
    reason: str = ""
    llm: object = None
    confidence: float | None = None
    rule: str = ""


@dataclass(frozen=True)
class ContractDraft:
    """A Dataset Contract proposal, for the contract form. Nothing is stored by drafting.

    A person sets each column's role and, for measures, an aggregation from AGGREGATIONS and a
    one-line definition. The UI sends those choices back through `draft_contract` and shows what
    comes back; only a draft with `provisional` empty can go to `confirm_contract`.
    """

    dataset_name: str
    grain: str | None
    primary_key: list[str]
    date_column: str | None
    measures: list[str]
    dimensions: list[str]
    aggregations: dict[str, str]
    measure_definitions: dict[str, str]
    analysis_window_start: str | None  # ISO date
    analysis_window_end: str | None
    caveats: list[str]
    columns: list[ContractColumn]
    provisional: list[str] = field(default_factory=list)  # fields still needing an answer
    questions: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)  # "what the data showed"
    message: str = ""
    refusal: Refusal | None = None
    #: One per measure the engine could read; see MeasureSuggestion (25/09/2026).
    suggestions: dict[str, MeasureSuggestion] = field(default_factory=dict)
    #: Measures counted once per unit ({"rep_monthly_salary": ["rep_id"]}) and ratios of sums
    #: ({"margin_pct_of_sums": {"numerator": [...], "denominator": [...], "scale": 100}}).
    measure_per: dict[str, list[str]] = field(default_factory=dict)
    ratios: dict[str, dict] = field(default_factory=dict)
    #: Caveats the engine counted from the table; every result carries them.
    measured_caveats: list[str] = field(default_factory=list)
    #: Helper measures reading another column ({"is_late_rate": "is_late"}).
    measure_columns: dict[str, str] = field(default_factory=dict)
    #: Field path -> who decided it, when a model filled the form ("measures[x].agg", "grain").
    sources: dict[str, FieldSource] = field(default_factory=dict)
    #: "gemini/gemini-2.5-flash" when a model filled it; `fill_note` says how it went or why not.
    filled_by: str = ""
    fill_note: str = ""


@dataclass(frozen=True)
class CleaningStep:
    """One change the engine proposes for a loaded table, with the numbers that justify it.

    `lossy` means it destroys something not declared missing -- `values_lost` of `loss_unit`,
    with `sample` showing what. `suggested` means the engine itself would run it: lossless, and
    in conflict with no other suggested step. Pre-tick only those; the rest are the person's.
    """

    action_id: str
    kind: str  # CONVERT_TYPE, NORMALISE_MISSING, TRIM_WHITESPACE, NORMALISE_CASE, ...
    column: str | None
    intent: str
    sql: str
    rows_affected: int
    values_lost: int = 0
    loss_unit: str = "value"
    sample: list[str] = field(default_factory=list)
    lossy: bool = False
    suggested: bool = False


@dataclass(frozen=True)
class CleaningProposal:
    """Everything the engine would change in one dataset. Nothing is changed by proposing.

    Empty `steps` with no refusal means there is nothing to clean. `message` is the engine's own
    account, Markdown. Approve by action id with `apply_cleaning`, in the order wanted.
    """

    dataset_name: str
    row_count: int
    steps: list[CleaningStep]
    message: str = ""
    refusal: Refusal | None = None


@dataclass(frozen=True)
class AnalysisParam:
    """One argument an analysis takes, as a form field.

    `kind` says which widget: "measure" / "dimension" (pick from `options`, the contract's own
    declared columns), "choice" (pick from `options`), "number", or "text". `default` is the
    engine's suggestion for this dataset -- the last two months for a period, the window's
    halves for a before/after split -- and may be None.
    """

    name: str
    kind: str
    required: bool
    options: list[str] = field(default_factory=list)
    default: str | float | None = None
    help: str = ""


@dataclass(frozen=True)
class AnalysisSpec:
    """One analysis the engine can run on a dataset under its contract. `chart` is the chart
    kind that draws it, or None when a table is the answer."""

    name: str
    tier: int
    summary: str
    params: list[AnalysisParam]
    chart: str | None = None


@dataclass(frozen=True)
class AnalysisMenu:
    """Every analysis for one dataset, with its form. Refused while the dataset has no
    confirmed contract -- the gate is the engine's, not the screen's."""

    dataset_name: str
    analyses: list[AnalysisSpec]
    refusal: Refusal | None = None


@dataclass(frozen=True)
class AnalysisRun:
    """One analysis (or report) as the engine wrote it. `text` is Markdown: the contract it ran
    under, what it was computed over, the table. `artifacts` are the files it wrote -- the
    chart, the result table, the report."""

    text: str
    artifacts: list[Artifact] = field(default_factory=list)
    refusal: Refusal | None = None


@dataclass(frozen=True)
class DatasetSummary:
    """One dataset in the workspace and where it has got to.

    `notes` carry what must be seen -- for example "Loaded as a subset: 1,000 of 1,000,163
    rows..." -- so show every one, not only the first.
    """

    name: str
    rows: int
    columns: int
    source: str
    loaded: str  # "loaded 3 minutes ago"
    stage: str  # "loaded, no contract" | "contract v1, ready" | "contract v2, BLOCKED"
    next_step: str
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Artifact:
    """A file the engine wrote: a chart (PNG), a report (Markdown) or a result table (CSV).

    `description` is the engine's own account of it -- for a chart, what was plotted and the
    lowest, highest, first and last values. Show it beside the file.
    """

    kind: ArtifactKind
    path: str
    title: str
    created: str  # ISO timestamp
    description: str = ""


@dataclass(frozen=True)
class ToolCall:
    """One tool the assistant called while answering, for a "what I did" expander."""

    name: str
    arguments: dict
    result: str
    refused: bool = False


@dataclass(frozen=True)
class ChatTurn:
    """The assistant's answer to one message. `reply` is Markdown.

    `artifacts` are files this turn produced; render charts inline under the reply. `error` is
    set when the turn could not complete (the model provider failed, a timeout) -- show it and
    keep the conversation; the workspace is unchanged by a failed turn's missing steps.
    """

    reply: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    error: str | None = None
    #: The reply's figures checked against the tool replies (webapp/verify.py): one line for the
    #: person, and whether every figure was found. None where nothing was checked.
    verification: str | None = None
    verified: bool = True
    #: What the person should still read after the answer about how it was reached (step 2): a
    #: wait on a busy provider, a move to another provider, earlier replies shortened to fit a
    #: model's request limit. Empty when nothing of the kind happened.
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MetricProposal:
    """A metric the assistant proposed and the person has not yet approved (step 3): a yes/no per
    row from comparing two columns, e.g. recorded_delivery_minutes > promised_minutes. `measured`
    is what the engine counted before anyone approved: rows it can judge, and how many are true."""

    id: str
    dataset_name: str
    name: str
    formula: str
    definition: str
    measured: str


@dataclass(frozen=True)
class ChatEvent:
    """Something the person waiting on an answer should see while it is worked out (step 2).

    `kind` is "wait" (a busy provider asked for a pause of `seconds`), "shortened" (earlier tool
    replies cut to fit a model's request limit), "failover" (another provider or model takes over
    the turn), or "step" (a round or a tool starting -- shown while it happens, not kept on the
    turn). `who` is "provider (model)" where one is involved. Measured 25/09/2026: the screen said
    "Reading the data..." for seven minutes of waits nobody could see.
    """

    kind: str
    text: str
    who: str = ""
    seconds: float = 0.0


class Backend(Protocol):
    """Everything the UI may ask of the engine. Nothing else crosses the boundary."""

    def new_workspace_id(self) -> str:
        """A fresh id for one browser session. Call once and keep it in session state."""

    def limits(self) -> Limits:
        """Size limits, for the uploader and for display."""

    def save_upload(self, workspace_id: str, filename: str, data: bytes) -> UploadResult:
        """Save an uploaded file into the workspace, after the size gate."""

    def draft_ingest(
        self,
        workspace_id: str,
        path: str,
        *,
        dataset_name: str | None = None,
        sheet: str | None = None,
        header_rows: list[int] | None = None,
        header_join: str | None = None,
        authorised_fill: bool = False,
    ) -> IngestDraft:
        """Propose how to read a saved file. Re-call with a person's answers to re-draft."""

    def confirm_ingest(self, workspace_id: str, spec: dict) -> ActionResult:
        """Load the file as the (possibly edited) spec says. Refused while unresolved."""

    def list_datasets(self, workspace_id: str) -> list[DatasetSummary]:
        """Every dataset in the workspace, newest first, with its stage and next step."""

    def draft_contract(
        self,
        workspace_id: str,
        dataset_name: str,
        *,
        grain: str | None = None,
        primary_key: list[str] | None = None,
        date_column: str | None = None,
        measures: list[str] | None = None,
        dimensions: list[str] | None = None,
        aggregations: dict[str, str] | None = None,
        measure_definitions: dict[str, str] | None = None,
        analysis_window_start: str | None = None,
        analysis_window_end: str | None = None,
        caveats: list[str] | None = None,
        measure_per: dict[str, list[str]] | None = None,
        ratios: dict[str, dict] | None = None,
        measure_columns: dict[str, str] | None = None,
    ) -> ContractDraft:
        """Propose a contract. With no answers and no contract in force, a model fills the form
        and the data filters it; with answers, the person's are drafted and marked as theirs."""

    def refill_contract(self, workspace_id: str, dataset_name: str) -> None:
        """Forget the model's fill for this dataset, so the next draft asks it again."""

    def confirm_contract(self, workspace_id: str, draft: ContractDraft) -> ActionResult:
        """Store the contract. Refused while `draft.provisional` is non-empty."""

    def propose_cleaning(self, workspace_id: str, dataset_name: str) -> CleaningProposal:
        """What the engine would change in a loaded table, step by step. Changes nothing."""

    def apply_cleaning(self, workspace_id: str, dataset_name: str,
                       approved_action_ids: list[str]) -> ActionResult:
        """Run exactly the approved steps of the latest proposal, in the order given, or none.
        Refused if the table changed since the proposal, an id is unknown, or none is given."""

    def analysis_menu(self, workspace_id: str, dataset_name: str) -> AnalysisMenu:
        """Every analysis runnable on this dataset, each with its form. Refused without a
        confirmed contract."""

    def run_analysis(self, workspace_id: str, dataset_name: str, analysis_type: str,
                     params: dict, chart: str | None = None) -> AnalysisRun:
        """Run one analysis under the contract, and draw it when `chart` is given. The same
        gate and the same engine calls the assistant makes -- with no model in between."""

    def build_report(self, workspace_id: str, dataset_name: str, question: str) -> AnalysisRun:
        """The full report for a dataset: every section, every number traced to its call."""

    def chat(self, workspace_id: str, history: list[dict], message: str,
             progress: Callable[[ChatEvent], None] | None = None) -> ChatTurn:
        """Answer one message. history is [{"role": "user"|"assistant", "content": str}, ...],
        oldest first, not including `message`. `progress`, where given, hears each ChatEvent
        while the turn runs (step 2). Optional: a backend written before it (ui/fake_backend.py)
        takes three arguments, and the Ask screen calls it with three."""

    def pending_metrics(self, workspace_id: str) -> list[MetricProposal]:
        """Metrics the assistant proposed that wait for the person's Approve or Reject (step 3)."""

    def decide_metric(self, workspace_id: str, proposal_id: str, approve: bool) -> ActionResult:
        """The person's decision on a proposed metric. Approved, the assistant may compute with it;
        every result then says it is provisional and not in the contract."""

    def list_artifacts(self, workspace_id: str) -> list[Artifact]:
        """Charts, reports and result tables in the workspace, newest first."""

    def read_artifact(self, workspace_id: str, path: str) -> bytes:
        """The bytes of one artifact, for st.image / st.markdown / st.download_button.
        Raises ValueError for a path that is not one of this workspace's artifacts."""

    def reset_workspace(self, workspace_id: str) -> ActionResult:
        """Empty the workspace. The UI must ask the person to confirm first."""


__all__ = [
    "AGGREGATIONS", "DTYPES", "HEADER_JOINS", "ROLES",
    "ActionResult", "AnalysisMenu", "AnalysisParam", "AnalysisRun", "AnalysisSpec", "Artifact",
    "Backend", "ChatEvent", "ChatTurn", "MetricProposal", "CleaningProposal", "CleaningStep",
    "ColumnDraft", "ContractColumn",
    "ContractDraft", "DatasetSummary", "FieldSource", "GridPreview", "IngestDraft", "Limits",
    "MeasureSuggestion",
    "Refusal",
    "ToolCall", "UploadResult",
]
