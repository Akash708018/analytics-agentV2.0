"""Central settings. Every limit lives here, not scattered in code.

Phase 2, Step 1. Supersedes the Phase 1 config.py. Every name the Phase 1
server.py and workspace.py imported is still exported, so nothing breaks.

See build guide Sections 4.2, 5.2, 6.7, 8.1, 8.2 and 10.

This module does NO I/O against user data. It answers three questions:
  1. Where does the agent put things?   -> Paths
  2. How big is too big?                -> Size gates
  3. What sources may it reach?         -> Source registry
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SERVER_NAME = "analytics-agent"
SERVER_VERSION = "0.2.0"

_GIB = 1024 ** 3
_MIB = 1024 ** 2
# File sizes are decimal, as macOS Finder and `ls -l` report them (Cleanup Step 12, RF-O2): the
# retail run's 2,595,199,347-byte file is 2.6 GB on the machine it sat on and was called "2.4 GB"
# here, under a refuse limit printed as "2.5 GB" that was 2.5 GiB. RAM stays in GiB above --
# memory is sold and reported in binary units.
_MB = 1000 ** 2
_GB = 1000 ** 3


# ---------------------------------------------------------------------------
# 1. Machine detection
# ---------------------------------------------------------------------------
# Guide 5.2 item 6: "On 8GB, halve the Section 4.2 size-gate thresholds."
# Section 4.2 never states what the thresholds are, so they are set here and
# scaled from detected RAM. Detecting rather than hardcoding also means Track B
# on a different host gets sane numbers with no code change.


def _detect_total_ram_bytes() -> int:
    """Total physical RAM in bytes. Falls back to 8 GiB, the cautious guess."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        pass
    try:
        out = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        return int(out.stdout.strip())
    except Exception:
        pass
    return 8 * _GIB


TOTAL_RAM_BYTES: int = _detect_total_ram_bytes()
TOTAL_RAM_GIB: float = round(TOTAL_RAM_BYTES / _GIB, 1)

# A 16 GB M1 reports ~16.0, an 8 GB reports ~8.0. The cut sits between them.
_SMALL_MACHINE_GIB = 12.0
IS_SMALL_MACHINE: bool = TOTAL_RAM_GIB < _SMALL_MACHINE_GIB


def _scaled(value: int) -> int:
    """Halve a threshold on a small machine."""
    return value // 2 if IS_SMALL_MACHINE else value


# ---------------------------------------------------------------------------
# 2. Paths
# ---------------------------------------------------------------------------
# Derived from __file__, never from the working directory: Claude Desktop
# launches this server with a minimal environment (guide 5.2 item 2).

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT: Path = PROJECT_ROOT / "workspace"

# The source registry lives OUTSIDE the repo so a DSN can never be committed by
# accident. Override with ANALYTICS_AGENT_SOURCES for Track B.
SOURCES_FILE: Path = Path(
    os.environ.get(
        "ANALYTICS_AGENT_SOURCES",
        Path.home() / ".analytics-agent" / "sources.yaml",
    )
).expanduser()


# ---------------------------------------------------------------------------
# 3. Workspace identity
# ---------------------------------------------------------------------------
# Guide 6.7 / locked decision 14: the workspace id is a PARAMETER, never
# generated at import. This constant is only the Track A DEFAULT. Every
# function that needs a workspace still takes it as an argument.

_WORKSPACE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

DEFAULT_WORKSPACE_ID: str = os.environ.get(
    "ANALYTICS_AGENT_WORKSPACE_ID", "local"
)


def validate_workspace_id(workspace_id: str) -> str:
    """Reject anything unsafe as a directory name.

    A workspace id becomes a path segment. In Track B it will arrive from a web
    session, so treat it as untrusted from day one: no separators, no dots, no
    traversal.
    """
    if not _WORKSPACE_ID_RE.match(workspace_id or ""):
        raise ValueError(
            f"Invalid workspace_id {workspace_id!r}. "
            "Allowed: letters, digits, hyphen, underscore; 1-64 characters. "
            "No slashes, dots or spaces."
        )
    return workspace_id


# ---------------------------------------------------------------------------
# 4. Size gates
# ---------------------------------------------------------------------------
# Locked decision 5: "A size gate runs before every load. Refusing with guidance
# beats being OOM-killed." Register entry F3.
#
# Two levels per source type:
#   WARN   -> the load proceeds, the tool says it will be slow
#   REFUSE -> the load does not start; the refusal carries the fix (guide 8.2)
#
# CSV limits are generous because DuckDB streams and spills to disk. Excel
# limits are tight because openpyxl parses XML row by row and cannot spill;
# that path is bounded by patience, not by memory.


@dataclass(frozen=True)
class SizeGates:
    csv_warn_bytes: int
    csv_refuse_bytes: int
    excel_warn_bytes: int
    excel_refuse_bytes: int
    excel_warn_rows: int
    excel_refuse_rows: int
    disk_headroom_multiplier: float
    disk_min_free_bytes: int


SIZE_GATES = SizeGates(
    csv_warn_bytes=_scaled(500 * _MB),
    csv_refuse_bytes=_scaled(5 * _GB),
    excel_warn_bytes=_scaled(40 * _MB),
    excel_refuse_bytes=_scaled(200 * _MB),
    excel_warn_rows=_scaled(250_000),
    excel_refuse_rows=_scaled(1_500_000),
    # DuckDB spills intermediates to disk; require room for the load plus
    # working space before starting one.
    disk_headroom_multiplier=3.0,
    disk_min_free_bytes=2 * _GB,
)

# Backward compatibility with the Phase 1 config.py. server.py imports these
# two names for its ping() output. They are now derived from SIZE_GATES rather
# than hand-set, so there is one source of truth.
MAX_EXCEL_MB: int = SIZE_GATES.excel_refuse_bytes // _MB
WARN_CSV_MB: int = SIZE_GATES.csv_warn_bytes // _MB


def human_bytes(n: float) -> str:
    """Format a byte count for a gate message, in decimal units (KB = 1,000 bytes), as the
    operating system's own file listings report it. Not for analysis output."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


def describe_size_gates() -> str:
    """Human-readable gate summary, for an instructional refusal (guide 8.2)
    and for get_workflow_state output."""
    g = SIZE_GATES
    scale_note = f"{TOTAL_RAM_GIB} GB RAM detected" + (
        " - thresholds halved for a small machine." if IS_SMALL_MACHINE else "."
    )
    return (
        f"Size gates ({scale_note})\n"
        f"| source | warn above | refuse above |\n"
        f"| --- | --- | --- |\n"
        f"| CSV / TSV | {human_bytes(g.csv_warn_bytes)} "
        f"| {human_bytes(g.csv_refuse_bytes)} |\n"
        f"| Excel (file) | {human_bytes(g.excel_warn_bytes)} "
        f"| {human_bytes(g.excel_refuse_bytes)} |\n"
        f"| Excel (rows) | {g.excel_warn_rows:,} | {g.excel_refuse_rows:,} |\n"
        f"| free disk | at least {g.disk_headroom_multiplier:g}x the file, "
        f"minimum {human_bytes(g.disk_min_free_bytes)} | - |"
    )


# ---------------------------------------------------------------------------
# 5. Read and return limits
# ---------------------------------------------------------------------------
# Guide 4.2 (preview paths), 8.1 Rule 3 (50 x 50 cap), F4, F5.

# Previews never load a whole file (locked decision 4).
CSV_PREVIEW_LINES: int = 200      # plain file iteration
EXCEL_PREVIEW_ROWS: int = 20      # openpyxl read_only iterator

# Excel -> DuckDB streaming batch size (guide 4.2).
EXCEL_BATCH_ROWS: int = 50_000

# Inline return caps live in util/formatting.py as MAX_ROWS and MAX_COLS,
# which has no module-level imports. config.py imports yaml and subprocess, so
# pointing formatting here would hand every analysis module that graph: P8-D40's
# objection, ruled in P8-D48. This module deliberately defines no inline caps.
MAX_CELL_CHARS: int = 200         # truncate a long free-text cell

# read_result_file paging lives in util/results.py as PAGE_ROWS, which is
# tied to formatting.MAX_ROWS so a page is exactly what the formatter was
# built to render. RESULT_PAGE_ROWS was defined here, said 100 where that
# says 50, and had no readers (P8-D48's shape).

# Default null tokens. An Ingest Spec may override these per dataset (guide 6).
DEFAULT_NA_VALUES: list[str] = [
    "", "NA", "N/A", "-", "--", "null", "NULL", "None",
]


# ---------------------------------------------------------------------------
# 6. Source registry
# ---------------------------------------------------------------------------
# Postgres DSNs are named aliases in a YAML file outside the repo. Tools take an
# alias, never a raw DSN, so a connection string cannot reach a chat transcript.
# Locked decision 22: Postgres is always READ_ONLY.
#
# Expected file (~/.analytics-agent/sources.yaml):
#
#   sources:
#     olist:
#       dsn: "host=localhost port=5432 dbname=olist"
#       description: "Olist Brazilian e-commerce warehouse (local)"
#
# Keep the password OUT of the DSN. Use ~/.pgpass or local trust auth. If one is
# present anyway, redact_dsn() hides it on the way out.

# Kept from the Phase 1 config. Seeded into the registry as alias "testdb" when
# no sources.yaml exists, so Step 7 is testable with no extra setup.
TEST_PG_DSN = "dbname=testdb"

_PASSWORD_RE = re.compile(r"(password\s*=\s*)(\S+)", re.IGNORECASE)
_URI_PASSWORD_RE = re.compile(r"(://[^:/@\s]+:)([^@\s]+)(@)")


def redact_dsn(dsn: str) -> str:
    """Mask any password in a libpq connection string or a postgres:// URI."""
    dsn = _PASSWORD_RE.sub(r"\1***", dsn)
    dsn = _URI_PASSWORD_RE.sub(r"\1***\3", dsn)
    return dsn


@dataclass(frozen=True)
class Source:
    alias: str
    dsn: str
    description: str = ""
    kind: str = "postgres"

    def redacted(self) -> str:
        return redact_dsn(self.dsn)


@dataclass
class SourceRegistry:
    sources: dict[str, Source] = field(default_factory=dict)
    load_error: str | None = None
    path: Path = SOURCES_FILE

    def get(self, alias: str) -> Source:
        if alias in self.sources:
            return self.sources[alias]
        known = ", ".join(sorted(self.sources)) or "(none configured)"
        raise KeyError(
            f"BLOCKED: no configured source named {alias!r}.\n"
            f"Known aliases: {known}\n"
            f"NEXT STEP: add it to {self.path} under 'sources:', then call "
            f"list_sources() to confirm it appears."
        )


def load_source_registry(path: Path | None = None) -> SourceRegistry:
    """Read the alias -> DSN registry.

    A missing file is normal, not an error: file-based ingest needs no registry.
    The built-in testdb alias is always present so Step 7 has something to hit.
    """
    path = path or SOURCES_FILE
    reg = SourceRegistry(path=path)
    reg.sources["testdb"] = Source(
        alias="testdb",
        dsn=TEST_PG_DSN,
        description="Local scratch database used for connector tests",
    )

    if not path.exists():
        return reg

    try:
        raw: Any = yaml.safe_load(path.read_text()) or {}
    except Exception as exc:
        reg.load_error = f"Could not parse {path}: {exc}"
        return reg

    entries = raw.get("sources") or {}
    if not isinstance(entries, dict):
        reg.load_error = f"{path}: 'sources' must be a mapping of alias -> settings."
        return reg

    for alias, cfg in entries.items():
        if not isinstance(cfg, dict) or not cfg.get("dsn"):
            reg.load_error = f"{path}: source {alias!r} is missing a 'dsn' key."
            continue
        reg.sources[str(alias)] = Source(
            alias=str(alias),
            dsn=str(cfg["dsn"]),
            description=str(cfg.get("description", "")),
            kind=str(cfg.get("kind", "postgres")),
        )
    return reg


if __name__ == "__main__":
    # Sanity check: uv run python -m backend.engine.config
    print(f"server         : {SERVER_NAME} {SERVER_VERSION}")
    print(f"project root   : {PROJECT_ROOT}")
    print(f"workspace root : {WORKSPACE_ROOT}")
    print(f"sources file   : {SOURCES_FILE} (exists: {SOURCES_FILE.exists()})")
    print(f"default ws id  : {DEFAULT_WORKSPACE_ID}")
    print(f"legacy aliases : MAX_EXCEL_MB={MAX_EXCEL_MB} WARN_CSV_MB={WARN_CSV_MB}")
    print()
    print(describe_size_gates())
    print()
    reg = load_source_registry()
    if reg.load_error:
        print(f"registry error : {reg.load_error}")
    for alias, src in sorted(reg.sources.items()):
        print(f"source {alias:<12} {src.redacted()}")