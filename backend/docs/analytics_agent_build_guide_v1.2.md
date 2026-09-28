# **Automated Analytics Agent — Full Build Guide**

**Owner:** Akash **Machine:** MacBook Air M1 (Apple Silicon, arm64) **Version:** 1.2 **Status:** Plan locked, Phase 0 not started

---

## **Changelog**

**v1.2 — implementation-trap pass.** Five issues reviewed; three verified by running them on this stack (openpyxl 3.1.5, DuckDB 1.5.5).

| \# | Change | Verified? | Sections |
| ----- | ----- | ----- | ----- |
| 1 | `merged_cells` is unavailable in openpyxl read-only mode — raises `AttributeError`. Replaced with direct XML parse. | ✅ Tested | 6.6, Phase 3 |
| 2 | Merge forward-fill must be **bounded by actual merge ranges**. Unbounded fill corrupts column names. | ✅ Tested | 6.3, Phase 3 |
| 3 | DuckDB `read_csv` has no multi-row header concept. Naive `skip` \+ `header=true` makes every column VARCHAR and turns the real header into a data row. | ✅ Tested | 4.2, Phase 2 |
| 4 | Claude Desktop keeps the stdio subprocess alive across new chats. Startup-generated `SESSION_ID` leaks state between unrelated tasks. | Accepted | 6.7, Phase 1–2 |
| 5 | Cleaning must use `CREATE OR REPLACE TABLE ... AS SELECT`, never in-place `UPDATE`. | Accepted | Phase 6 |
| 6 | DuckDB extension install needs internet on first run. | Accepted | Phase 0 |
| 7 | New tool `reset_workspace`. FMR extended to F15. | — | 8, 16 |
| 8 | Phase 15 added, 22/09/2026: cleaning in the web app and helper columns (`DERIVE_COLUMN`, a fixed menu). Optional extras renumbered to Phase 16. | Planned | 9, 11, 12 |

**v1.1 — hardening pass.** Streaming ingest and size gate; `header_rows` as list; no bare file paths; instructional gate refusals; session-scoped DuckDB; pipe-delimited returns; Failure Mode Register added.

Timeline: 31–39 focused days.

---

## **READ THIS FIRST — How to use this document in a new chat**

Paste this into a fresh AI chat window and say:

> "Here is my build guide for the Automated Analytics Agent. I have completed through Phase **N**. Start me on Phase **N+1**. Give me full files, not diffs. One step at a time."

For that to work, **keep Section 12 (Progress Ledger) updated.** Tick a box the moment a phase passes its Done-When test. A stale ledger means the new window builds on code that does not exist.

Three rules carried over from your previous projects:

1. Full file re-deliveries, never diffs.  
2. One step at a time, no unexplained jargon.  
3. Lock the plan before touching the keyboard. This document is that lock.

**A note on the verified sections.** Sections marked ✅ contain code that was actually executed against openpyxl 3.1.5 and DuckDB 1.5.5, not reasoned about. Trust those over any AI's recollection, including mine — but re-run them if your versions differ materially.

---

## **1\. What you are building, in one paragraph**

A local program that any AI chat client can talk to. You point it at a Postgres database or hand it an Excel/CSV file. It figures out the structure with your confirmation, profiles the data, proposes cleaning steps with exact row counts, applies only what you approve, validates the result, runs a full battery of analyses, and writes a detailed report that includes a complete ledger of everything it changed. You ask questions in plain English; it answers with numbers it can defend.

**What makes it different from the hundreds of "chat with your CSV" projects:** the cleaning ledger, the approval gate, the grain declaration, and the eval harness.

---

## **2\. Vocabulary**

| Term | Plain meaning |
| ----- | ----- |
| **MCP** | Model Context Protocol. An open standard for letting an AI call functions you wrote. |
| **MCP server** | Your Python program. Publishes a list of functions and waits to be called. |
| **stdio transport** | The AI launches your program as a subprocess and talks over standard input/output. |
| **Streamable HTTP** | Your program runs as a real web service. Needed only for Track B. |
| **Tool** | One function your server exposes, e.g. `profile_dataset`. |
| **FastMCP** | Python library that turns a normal function into an MCP tool with a decorator. |
| **DuckDB** | A database that runs inside your Python process. "SQLite for analytics". |
| **Out-of-core** | Processing data larger than RAM by streaming from disk. DuckDB does this; pandas does not. |
| **CTAS** | `CREATE TABLE AS SELECT`. Builds a new table from a query instead of editing one in place. |
| **MCP Inspector** | Debugging web app that connects to your server so you can click tools without an AI. |
| **Grain** | What one row represents. Getting this wrong invalidates everything downstream. |
| **Ingest Spec** | JSON describing how to read a messy file: header rows, columns, types. |
| **Cleaning ledger** | Append-only record of every change made, with row counts. |
| **Workspace** | One isolated DuckDB file plus outputs. Reset explicitly, not automatically. |

---

## **3\. The two problems this design solves**

### **3.1 The messy-Excel problem**

The fix is not "add a UI panel". The fix is a **data contract** — the Ingest Spec — with two front doors filling the same object:

* **Track A (chat only):** the tool returns the raw grid as text plus a draft spec. You correct it in chat.  
* **Track B (Streamlit):** the same grid renders as a clickable table. Same spec comes out.

The consuming code is byte-for-byte identical. If you build it as a UI feature instead, you write the ingest logic twice and they drift.

**The rule:** the server never guesses silently. It shows the grid, states its guess, and waits.

### **3.2 The trust problem**

An agent that can modify data is an agent nobody trusts. The answer is a hard split, **enforced in code, not in a prompt**:

* `propose_cleaning_plan` computes what *would* change and how many rows. Writes nothing.  
* `apply_cleaning_plan` accepts only action IDs that came from a proposal.  
* Every applied action appends to the ledger, and the ledger ships inside the report.

---

## **4\. Architecture**

                   ┌──────────────────────────────┐

                    │  AI CLIENT                   │

                    │  Track A: Claude Desktop     │

                    │  Track B: Streamlit \+ free   │

                    │           LLM API            │

                    └──────────────┬───────────────┘

                                   │  MCP protocol

                    ┌──────────────▼───────────────┐

                    │  YOUR MCP SERVER (Python)    │

                    │  L1 Ingest    L6 Validation  │

                    │  L2 Structure L7 Analysis    │

                    │  L3 Contract  L8 Charts      │

                    │  L4 Profile   L9 Report      │

                    │  L5 Cleaning                 │

                    └──────────────┬───────────────┘

                    ┌──────────────▼───────────────┐

                    │  DUCKDB — one file per       │

                    │  WORKSPACE, in workspace/    │

                    └──────┬───────────────┬───────┘

                  ┌────────▼─────┐  ┌──────▼────────┐

                  │  Postgres    │  │ Excel / CSV   │

                  │  (READ\_ONLY) │  │ (streamed)    │

                  └──────────────┘  └───────────────┘

### **4.1 Why DuckDB in the middle**

Everything lands in DuckDB, so every layer above is written **once** against one dialect. Otherwise you write each analysis twice, once in Postgres SQL and once in pandas.

**On the apparent contradiction with Olist:** there you chose Postgres over DuckDB for resume signalling on a warehouse project. That still stands. Here DuckDB is an internal engine, not the showcase, and Postgres remains a first-class source connector. In an interview that is a strong answer, not a walk-back.

### **4.2 How files are read — ✅ VERIFIED**

Pandas is not on the ingest path. It loads entire files into memory; DuckDB streams. On an 8–16GB M1 a 1GB CSV would trigger an OOM kill with no useful error.

| Input | Preview path | Full load path |
| ----- | ----- | ----- |
| **CSV / TSV** | First 200 lines via plain file iteration | DuckDB native `read_csv`, streaming |
| **Excel (.xlsx)** | `load_workbook(read_only=True)`, first 20 rows only | openpyxl read-only iterator, batched 50k rows into DuckDB |
| **Postgres** | `LIMIT 20` | `ATTACH ... READ_ONLY` — no copy |

**CSV goes to DuckDB but Excel does not** because the version-drift concern applies to DuckDB's spreadsheet extension, not to CSV reading, which is core and stable.

#### **4.2.1 The multi-row header trap in `read_csv` — ✅ VERIFIED**

DuckDB's `read_csv` has `skip` and `header`, but **no concept of multi-row header concatenation**. Handing it a two-row header naively corrupts everything. Measured on DuckDB 1.5.5 with a file whose real header spans rows 3–4 and data starts at row 5:

**Naive — `read_csv(path, skip=2, header=true)`:**

columns: \['Order', 'column1', 'Q3 2024', 'Q3 2024\_1', 'column4'\]

types  : \[VARCHAR, VARCHAR, VARCHAR, VARCHAR, VARCHAR\]

row 1  : ('ID', 'Region', 'Units', 'Amt', 'Channel')   ← the real header, as DATA

Every column typed VARCHAR, and the second header row silently became the first data row. Nothing errors. The analysis engine would then refuse to sum anything because no column is numeric, and you would spend an evening debugging the wrong layer.

**Correct — assemble names in Python, then `header=false` with explicit `skip`:**

names \= assemble\_header\_names(spec)          \# see 6.3

con.sql(f"""

    SELECT \* FROM read\_csv('{path}',

        header \= false,

        skip   \= {spec.data\_start\_row \- 1},

        names  \= {names})

""")

columns: \['Order ID', 'Region', 'Q3 2024 Units', 'Q3 2024 Amt', 'Units', 'Channel'\]

types  : \[VARCHAR, VARCHAR, BIGINT, BIGINT, BIGINT, VARCHAR\]

Type inference now works. `skip = data_start_row - 1` because `skip` is a count of rows, while `data_start_row` is a 1-indexed position.

**Apply this whenever `len(spec.header_rows) > 1`.** For a single-row header, plain `header=true` is fine.

---

## **5\. Requirements — MacBook Air M1**

### **5.1 Software checklist**

| Thing | Version | Why | Cost |
| ----- | ----- | ----- | ----- |
| Homebrew | Latest, at `/opt/homebrew` | Package manager | Free |
| Python | **3.12** | 3.13 has occasional wheel gaps | Free |
| uv | Latest | Environment manager | Free |
| DuckDB (Python) | 1.5+ | Analysis engine | Free |
| openpyxl | 3.1+ | Excel reading | Free |
| PostgreSQL | 16 or 17 | Source connector \+ Olist | Free |
| Node.js | 20 LTS | MCP Inspector only | Free |
| Claude Desktop | Latest | Track A client | Free |
| VS Code, Git, GitHub | — | — | Free |

**Total: ₹0.**

### **5.2 M1-specific gotchas**

1. **Homebrew must be at `/opt/homebrew`.** `/usr/local` is the Intel path.  
2. **Claude Desktop launches your server with a minimal PATH.** Short names like `npx` often fail even when they work in your terminal; use full paths. This bites Apple Silicon hardest because `/opt/homebrew/bin` is rarely on that PATH.  
3. **Absolute paths everywhere in config.** No `~`, no relative paths.  
4. **Do not use Rosetta.** Every package here has native arm64 wheels.  
5. **Never `pip install` into `/usr/bin/python3`.**  
6. **Know your RAM:** `sysctl hw.memsize`. On 8GB, halve the Section 4.2 size-gate thresholds.

---

## **6\. The Ingest Spec**

{

  "source\_type": "excel",

  "path": "/Users/akash/data/sales\_2024.xlsx",

  "sheet\_name": "Q3 Data",

  "header\_rows": \[4, 5\],

  "header\_join": "space",

  "header\_merge\_fill": "bounded",

  "data\_start\_row": 6,

  "data\_end\_row": null,

  "footer\_skip\_rows": 3,

  "na\_values": \["", "NA", "N/A", "-", "--", "null"\],

  "thousands\_sep": ",",

  "decimal\_sep": ".",

  "encoding": "utf-8",

  "columns": \[

    {

      "source\_index": 0,

      "source\_name": "Order ID",

      "target\_name": "order\_id",

      "dtype": "string",

      "role": "identifier",

      "unit": null,

      "notes": "Leading zeros must be preserved"

    }

  \],

  "skip\_columns": \[7, 8\]

}

### **6.1 Field rules**

* `header_rows` is a **list**, 1-indexed to match what Excel shows you. Single-row header is `[4]`.  
* `header_join`: `space` | `underscore` | `bottom_only` | `top_only`.  
* `header_merge_fill`: **`bounded`** | `none`. See 6.3 — `unbounded` was removed in v1.2 because it is always wrong.  
* `footer_skip_rows` handles totals rows and notes at the bottom.  
* `dtype`: `string`, `integer`, `decimal`, `date`, `datetime`, `boolean`, `category`.  
* `role`: `identifier`, `dimension`, `measure`, `date`, `flag`, `free_text`, `ignore`.

### **6.2 Why `role` exists**

`dtype` says how to store a value. `role` says what it is *for*. Integers could be a customer ID (never sum), a quantity (sum), or a rating (average, and watch for a J-shaped distribution — you saw this on Olist reviews). Without `role` the agent will average your order IDs.

### **6.3 Merge fill must be BOUNDED — ✅ VERIFIED**

This is the subtlest bug in the whole ingest layer, and it was found by running the code, not reading it.

A spanning header like `Q3 2024` merged across C:D stores its value **only in C4**. D4 is empty. The obvious fix is to forward-fill the header row left to right. **That is wrong**, because it does not stop at the end of the merge range — it keeps filling into unrelated columns.

Measured, on a row where only `C4:D4` is actually merged:

UNBOUNDED (buggy):

  \['Order ID', 'Order Region', 'Q3 2024 Units', 'Q3 2024 Amt',

   'Q3 2024 Units', 'Q3 2024 Channel'\]

BOUNDED (correct):

  \['Order ID', 'Region', 'Q3 2024 Units', 'Q3 2024 Amt',

   'Units', 'Channel'\]

`Order` leaked into `Region`, and `Q3 2024` leaked onto `Channel`, which was never under any merge. Both names are plausible enough that you would not notice until a number came out wrong.

**Verified implementation:**

from openpyxl.utils import range\_boundaries

def fill\_bounded(row\_values, merge\_refs, row\_num\_1idx):

    """Fill ONLY across real merge ranges on this row. No guessing."""

    out \= list(row\_values)

    for ref in merge\_refs:

        c1, r1, c2, r2 \= range\_boundaries(ref)

        if r1 \<= row\_num\_1idx \<= r2:

            v \= out\[c1 \- 1\]

            for c in range(c1, c2 \+ 1):

                out\[c \- 1\] \= v

    return out

**For CSV there is no merge metadata at all.** A blank cell in a CSV header row could be a merge artifact or could be genuinely blank, and nothing in the file distinguishes them. So: **never auto-fill CSV headers.** Show the raw header rows, state that blanks are ambiguous, and ask. Guessing here is how you silently mislabel a column.

### **6.4 Pivot-dump detection**

If the preview finds a mostly-null column with occasional labels, plus a "Grand Total" string in the first column, flag it as a probable pivot dump and warn that manual flattening may be needed. **Do not auto-unpivot** — that is a rabbit hole and the guess will be wrong.

### **6.5 The confirmation loop**

User: analyse this file

  ↓  connect\_source(path)   \[size gate first — may refuse here\]

Server returns: streamed grid preview, merged-cell report,

                draft Ingest Spec, plain-English assumptions

  ↓  user confirms or corrects

confirm\_ingest\_spec(spec)  →  data streamed into DuckDB

**Nothing loads until `confirm_ingest_spec`.** Hard gate.

### **6.6 openpyxl read-only mode cannot see merged cells — ✅ VERIFIED**

Read-only mode uses a streaming XML parser that skips sheet metadata to save memory. Merge information lives in that skipped metadata. Measured on openpyxl 3.1.5:

\=== NORMAL MODE \===

has attr: True

ranges  : \[\<MergedCellRange E4:F4\>, \<MergedCellRange C4:D4\>\]

\=== READ\_ONLY MODE \===

class   : ReadOnlyWorksheet

has attr: False

ERROR   : AttributeError: 'ReadOnlyWorksheet' object has no attribute 'merged\_cells'

Note it raises rather than returning empty — which is the good outcome, because a crash in development is better than a silent zero in production. Do not "fix" this with a `try/except` that swallows it.

**The bind:** you need read-only mode for memory safety on large files, and you need merge data for correct headers. You cannot have both from the same API.

**Verified fix — parse the `<mergeCells>` block straight from the sheet XML.** Constant memory, no workbook load:

import zipfile

from xml.etree import ElementTree as ET

NS \= "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

def merged\_ranges(path, sheet\_name):

    with zipfile.ZipFile(path) as z:

        wb \= ET.fromstring(z.read("xl/workbook.xml"))

        idx \= None

        for i, s in enumerate(wb.find(f"{NS}sheets"), start=1):

            if s.get("name") \== sheet\_name:

                idx \= i

                break

        if idx is None:

            raise KeyError(sheet\_name)

        out \= \[\]

        with z.open(f"xl/worksheets/sheet{idx}.xml") as f:

            for \_, el in ET.iterparse(f, events=("end",)):

                if el.tag \== f"{NS}mergeCell":

                    out.append(el.get("ref"))

                elif el.tag \== f"{NS}sheetData":

                    el.clear()   \# drop row data as we stream past it

        return out

Tested against the normal-mode ground truth: both return `C4:D4` and `E4:F4`.

Two implementation notes. The `el.clear()` on `sheetData` is what keeps memory flat — without it, `iterparse` accumulates every row in the tree. And sheet index comes from `workbook.xml` order; do not assume `sheet1.xml` matches the first tab you see in Excel, because tab order and file order can diverge.

### **6.7 Workspace identity — the persistent-process trap**

Claude Desktop launches your MCP server once when the app starts and **keeps that subprocess alive across every conversation**. Clicking "New Chat" does not restart it.

The consequence: if you generate a workspace ID at module import (`WORKSPACE_ID = uuid4()`), every new chat silently reuses the previous chat's DuckDB catalog. You start a fresh conversation about a new file and the agent can still see `sales_2024` from two hours ago, with its old contract and its old ledger.

**There is no fix that detects chat boundaries** — MCP gives the server no conversation identifier. So do not try to infer it. Instead:

1. **Partition all state by `dataset_name`,** never by an implicit "current dataset". Every tool takes `dataset_name` explicitly. There is no ambient selection to leak.  
2. **Provide an explicit `reset_workspace()` tool** that drops all tables, clears ledgers, and starts clean. Document it in the README as the way to begin a new task.  
3. **Make `get_workflow_state` list every loaded dataset with its load timestamp,** so leftovers are visible rather than surprising.  
4. **Track B is different** — there, a real per-user session ID exists and *should* key the DuckDB file. The Phase 2 code takes a workspace ID as a parameter for exactly this reason: Track A passes a stable one, Track B passes the session's.

---

## **7\. The Dataset Contract**

After ingest, before analysis, the agent must obtain a contract:

{

  "dataset\_name": "sales\_2024",

  "grain": "one row \= one order line item",

  "primary\_key": \["order\_id", "line\_number"\],

  "date\_column": "order\_date",

  "analysis\_window": {"start": "2024-01-01", "end": "2024-12-31"},

  "measures": \[

    {"name": "amount\_inr", "agg": "sum", "definition": "net of tax, excludes cancelled"}

  \],

  "dimensions": \["region", "category", "channel"\],

  "known\_exclusions": \[

    {"rule": "status \= 'cancelled'", "reason": "not real revenue", "row\_count": 412}

  \],

  "caveats": \["November 2024 has no data — source feed outage"\]

}

This generalises the eight decisions you locked on Olist so they work on any dataset. It is the difference between an agent that invents a revenue definition and one that uses yours, and it makes every number in the report traceable to something you signed off.

---

## **8\. Full tool surface**

Nineteen tools.

### **Group A — Ingest (Phase 2–3)**

| Tool | Returns |
| ----- | ----- |
| `list_sources` | Configured DSNs and recent file paths |
| `connect_source` | Size-gate result, streamed grid preview, merged-cell report, draft spec, assumptions |
| `confirm_ingest_spec` | Rows read, rows loaded, per-column coercion failures |
| `describe_dataset` | Tables, columns, dtypes, roles, row counts |

### **Group B — Contract (Phase 4\)**

| Tool | Returns |
| ----- | ----- |
| `propose_dataset_contract` | Draft contract with evidence per guess |
| `confirm_dataset_contract` | Stored contract; unlocks analysis |

### **Group C — Profile (Phase 5\)**

| Tool | Returns |
| ----- | ----- |
| `profile_dataset` | Per column: nulls, distinct, min/max, mean/median/std, top 10, type mismatches, outliers; plus duplicate row count |
| `profile_column` | Distribution, histogram, string-length stats, date gaps |

### **Group D — Clean (Phase 6\)**

| Tool | Returns |
| ----- | ----- |
| `propose_cleaning_plan` | Numbered actions with exact row counts. Writes nothing. |
| `apply_cleaning_plan` | Before/after counts per action; ledger entries |
| `get_cleaning_ledger` | Full append-only history |

### **Group E — Validate (Phase 7\)**

| Tool | Returns |
| ----- | ----- |
| `validate_dataset` | Pass/fail across PK uniqueness, nulls, ranges, referential integrity, date sanity, grain |

### **Group F — Analyse (Phase 8–10)**

| Tool | Returns |
| ----- | ----- |
| `run_analysis` | Result table \+ method note \+ caveats |
| `run_sql` | Read-only, row-capped result |

### **Group G — Output (Phase 11–12)**

| Tool | Returns |
| ----- | ----- |
| `render_chart` | PNG path **plus text description of what was plotted and key values** |
| `build_report` | Report path \+ table of contents \+ key findings inline |

### **Group H — Orientation (Phase 4–5)**

| Tool | Returns |
| ----- | ----- |
| `get_workflow_state` | Every loaded dataset with load time and progress; what is blocked; exact next call |
| `read_result_file` | A page of rows from a result file |
| `reset_workspace` | Drops all tables and ledgers. Requires `confirm=True`. |

### **8.1 Return contract**

**Rule 1 — every tool returns a formatted string, never a nested object.** One MCP build writeup describes a tool returning a raw Python dict that rendered fine in the Inspector but came back truncated inside Claude Desktop, fixed by wrapping the return as a typed text string.

**Rule 2 — pipe-delimited markdown, not space-padded ASCII.** Pipes give every cell an unambiguous boundary.

**Rule 3 — cap rows AND columns; raw dumps are never a primary return.** 50 × 50 maximum inline. An analysis tool's job is to return *an answer*, not a grid. If a tool's main output is raw records, it is under-designed.

**Rule 4 — no tool ever returns a bare file path.** Handing an LLM a path it cannot open invites a confident report about data it never saw. Every disk-write returns path \+ shape \+ summary \+ first 20 rows \+ a paging hint pointing at `read_result_file`.

### **8.2 Gate refusals must be instructional**

A bare error makes the agent flail — retry the same wrong call, apologise, retry. The refusal must carry the fix:

BLOCKED: run\_analysis requires a confirmed Dataset Contract for

'sales\_2024'. None exists.

NEXT STEP: call propose\_dataset\_contract(dataset\_name="sales\_2024"),

show the draft to the user, then call confirm\_dataset\_contract once

they approve.

Current state: loaded (51,290 rows), profiled, not cleaned, no contract.

What was blocked, the exact next call with arguments, current state. `get_workflow_state` returns the same on demand.

Write tool docstrings as **instructions**, not descriptions — FastMCP uses them as what the model reads. "Call this only after `confirm_ingest_spec`", not "Loads a dataset."

---

## **9\. The analysis engine**

### **Tier 1 — Descriptive (Phase 8\)**

`summary_stats` · `distribution` · `frequency` · `cross_tab` · `top_n`

### **Tier 2 — Comparative (Phase 8\)**

`group_compare` · `pareto` · `concentration` · `ranking_shift`

### **Tier 3 — Temporal (Phase 9\)**

`calendar_coverage` · `trend` · `seasonality` · `period_compare` · `growth_decomposition`

**`calendar_coverage` runs mandatorily before any other temporal analysis.** On Olist, November 2016 was absent and required explicit month-series generation. That is the norm, not a quirk.

### **Tier 4 — Relational (Phase 9\)**

`correlation` · `bivariate` · `driver_analysis` · `mix_shift`

`mix_shift` answers "did average order value fall because customers spent less, or because the cheap category grew?" Almost nobody builds it.

### **Tier 5 — Anomaly (Phase 9\)**

`outlier_detection` (IQR, z-score, MAD side by side) · `correlated_shift` · `changepoint`

**`correlated_shift` is your signature move.** It surfaced the Tamil Nadu statewide data-feed event on Mandi — 252 blocks across 89 markets, roughly 36–115× too low, missed by twelve earlier checks. Port and generalise it.

### **Tier 6 — Inferential (Phase 10\)**

`hypothesis_test` · `confidence_interval` · `effect_size` · `sample_adequacy`

Every result states the test used, the assumption checked, and a plain-English interpretation.

### **Tier 7 — Cohort (Phase 10\)**

`cohort_retention` · `repeat_behaviour`

If repeat rate \< 5%, `cohort_retention` warns and recommends `repeat_behaviour` — the reframe you made on Olist at \~3%.

### **Tier 8 — Forecasting (Phase 16, optional)**

Port SARIMAX/Prophet from Mandi. Out of scope for the main build.

---

## **10\. Repository layout**

analytics-agent/

├── README.md                  ← include the Failure Mode Register here

├── pyproject.toml

├── .gitignore

├── docs/

│   ├── analytics\_agent\_build\_guide.md    ← this file

│   ├── decisions.md

│   └── demo.md

├── src/analytics\_agent/

│   ├── server.py              ← tool registration ONLY, no logic

│   ├── config.py              ← paths, size gates, limits, DSN registry

│   ├── workspace.py           ← workspace ID, paths, reset

│   ├── state.py               ← per-dataset state \+ gate messages

│   ├── ingest/

│   │   ├── spec.py            ← IngestSpec model \+ validation

│   │   ├── preview.py         ← streaming preview

│   │   ├── merges.py          ← XML mergeCells parser (6.6) \+ bounded fill (6.3)

│   │   ├── headers.py         ← multi-row assembly, dedup

│   │   ├── sizegate.py

│   │   ├── excel.py           ← openpyxl read-only streaming

│   │   ├── csv\_loader.py      ← DuckDB read\_csv with names/skip (4.2.1)

│   │   └── postgres.py        ← ATTACH READ\_ONLY

│   ├── contract/dataset\_contract.py

│   ├── profile/{table\_profile,column\_profile}.py

│   ├── clean/{proposals,apply,ledger}.py

│   ├── validate/rules.py

│   ├── analysis/{registry,descriptive,comparative,temporal,

│   │             relational,anomaly,inferential,cohort}.py

│   ├── charts/render.py

│   ├── report/{assemble.py,templates/}

│   └── util/{db,sql\_guard,results,formatting}.py

├── tests/

│   ├── fixtures/

│   │   ├── clean\_sales.csv

│   │   ├── messy\_headers.xlsx        ← title in A1, header row 4, footer notes

│   │   ├── merged\_multiheader.xlsx   ← 2-row header, merged spanning cells

│   │   ├── multiheader.csv           ← 2-row header, NO merge metadata

│   │   ├── mixed\_types.xlsx          ← numbers as text, dates as strings

│   │   ├── gaps\_and\_dupes.csv

│   │   └── big\_synthetic.csv         ← \~1.5 GB, script-generated, gitignored

│   └── test\_\*.py

├── eval/{gold\_questions.yaml,run\_eval.py}

└── workspace/                 ← gitignored

    └── \<workspace\_id\>/        ← duckdb file, ledgers, outputs, charts

**Rule:** `server.py` contains zero business logic. This keeps Track B a transport swap, not a rewrite.

---

## **11\. Phases**

---

### **Phase 0 — Environment (half a day)**

1. `brew --prefix` must print `/opt/homebrew`.  
2. `brew install python@3.12 postgresql@17 node git uv`  
3. Install Claude Desktop, sign in. Install VS Code.  
4. `brew services start postgresql@17`; confirm Olist is reachable.  
5. Note RAM: `sysctl hw.memsize`.

**Pre-fetch the DuckDB Postgres extension while you have internet:**  
 python3 \-c "import duckdb; duckdb.connect().sql('INSTALL postgres')"

6. `INSTALL` downloads a binary from DuckDB's extension repository on first use. Behind a restrictive network or offline it hangs rather than failing fast, and it will do this in the middle of Phase 2 when you are debugging something else. Get it cached now. Extensions live in `~/.duckdb/extensions/`.

**Done-When:** `python3.12 -c "import platform; print(platform.machine())"` prints `arm64`; `psql -l` works; `node --version` ≥ 20; Claude Desktop Settings → Developer visible; the extension pre-fetch completed.

**Trap:** `x86_64` means you are under Rosetta. Fix before continuing.

---

### **Phase 1 — Skeleton and first connection (1 day)**

1. `mkdir analytics-agent && cd analytics-agent && git init`  
2. `uv init --python 3.12`  
3. `uv add fastmcp duckdb openpyxl psycopg[binary] pydantic pyyaml` (No pandas. It is not on the ingest path.)  
4. **Check current FastMCP docs before writing FastMCP code.** It moved 2.x → 3.x in early 2026 and the registration surface changed. Do not trust remembered syntax, including mine.  
5. Create the folder tree.  
6. `util/formatting.py` — `format_table()`, pipe-delimited, 50×50 caps.  
7. `workspace.py` — workspace ID **taken as a parameter, not generated at import** (see 6.7). Track A passes a stable ID from config; Track B will pass a session ID.  
8. `server.py` with two tools: `ping()` and `reset_workspace(confirm: bool)`.  
9. Inspector: `npx @modelcontextprotocol/inspector uv run python -m analytics_agent.server`  
10. Claude Desktop: Settings → Developer → Edit Config opens `~/Library/Application Support/Claude/claude_desktop_config.json`.

{

  "mcpServers": {

    "analytics-agent": {

      "command": "/opt/homebrew/bin/uv",

      "args": \["--directory",

               "/Users/\<YOUR\_USERNAME\>/code/analytics-agent",

               "run", "python", "-m", "analytics\_agent.server"\]

    }

  }

}

11. **Fully quit Claude Desktop (Cmd+Q)** and reopen.

**Done-When:** "ping the analytics agent" returns your formatted string, and `reset_workspace` works.

**Trap:** fails for boring reasons — trailing comma, relative path, non-absolute `uv`. Logs: `~/Library/Logs/Claude/mcp.log`.

---

### **Phase 2 — Ingest connectors and workspace isolation (2–3 days)**

**Files:** `util/db.py`, `workspace.py`, `ingest/csv_loader.py`, `ingest/excel.py`, `ingest/postgres.py`, `ingest/sizegate.py`, `config.py`

**Workspace isolation — build now, not in Phase 14\.** DuckDB takes a file lock per process; two processes on one file give `IO Error: Could not set lock on file`. Track A never hits this; Track B hits it with the second user, who would also see the first user's data. Retrofitting means touching every module.

**Loaders:**

* Postgres: `INSTALL postgres; LOAD postgres; ATTACH '<dsn>' AS pg (TYPE POSTGRES, READ_ONLY);`  
* CSV single-row header: `read_csv(path, header=true, skip=N)`  
* **CSV multi-row header: assemble names in Python, then `header=false, skip=data_start_row-1, names=[...]`** per 4.2.1. Verified.  
* Excel: `load_workbook(read_only=True)`, iterate, batch 50k rows.  
* Size gate runs before all of the above.

**Done-When:** load an Olist table, `clean_sales.csv`, `multiheader.csv`, and a clean xlsx; `describe_dataset` correct for all four; **`multiheader.csv` produces BIGINT columns, not VARCHAR** (this is the 4.2.1 regression test); two server processes with different workspace IDs run concurrently without lock errors.

**Trap:** always `READ_ONLY` on Postgres. The agent must be structurally incapable of writing to your source.

---

### **Phase 3 — Ingest Spec, preview, merges (4–5 days)**

Longest ingest phase. This is where the messy-file problem is actually solved.

**Files:** `ingest/spec.py`, `preview.py`, `merges.py`, `headers.py`

1. `IngestSpec` Pydantic model per Section 6\. Validate `data_start_row > max(header_rows)`, no duplicate `target_name`, valid enums.  
2. **Streaming preview.** CSV: 200 lines by file iteration. Excel: `read_only=True`, first 20 rows. Never load a whole file for a preview.  
3. **`merges.py` — the XML parser from 6.6.** Do **not** call `ws.merged_cells` on a read-only worksheet; it raises `AttributeError`. Verified.  
4. **Bounded fill from 6.3.** Never unbounded. Add a unit test asserting `Channel` does not become `Q3 2024 Channel`.  
5. **CSV headers are never auto-filled** — no merge metadata exists to bound the fill. Show raw rows and ask.  
6. **Multi-row assembly** per `header_join`; dedupe with numeric suffixes and report it.  
7. **Header-row guesser:** first row where values are mostly non-null strings, mostly distinct, non-numeric, and where the row below has a different type profile. Report confidence honestly — "I think", not "the header is".

**Merged-cell reporting** — always state what was filled:  
 NOTE: Rows 4–5 contain 2 merged ranges (C4:D4, E4:F4). I filledthem to build column names. Row 4 alone showed 4 of 6 columnsblank. Check the assembled names before confirming.

8.   
9. Pivot-dump detection per 6.4.  
10. Per-column type-coercion failure counts. Never silently coerce to null.

**Done-When:** `messy_headers.xlsx` and `merged_multiheader.xlsx` both load correctly through pure conversation; `multiheader.csv` prompts rather than guessing; the bounded-fill unit test passes; and the preview of `big_synthetic.csv` (1.5 GB) returns in under 5 seconds with flat memory.

**Trap:** the 1-indexed / 0-indexed boundary. The spec is 1-indexed to match Excel; DuckDB `skip` is a *count*, not an index; openpyxl rows are 1-indexed but its iterators are not always what you expect. Convert in exactly one place per loader and write a test that catches an off-by-one.

---

### **Phase 4 — Dataset Contract and workflow state (2 days)**

**Files:** `contract/dataset_contract.py`, `state.py`

1. Pydantic model per Section 7\.  
2. `propose_dataset_contract` guesses PK candidates (uniqueness on singles and pairs), date column, measures, dimensions — **with evidence**: "order\_id is unique across 99,441 of 99,441 rows".  
3. `confirm_dataset_contract` stores it.  
4. `state.py`: per-**dataset** progress tracking (never an ambient "current dataset" — see 6.7) and the instructional gate messages from 8.2.  
5. `require_contract()` used by all analysis tools.  
6. `get_workflow_state` tool, listing every loaded dataset **with load timestamps** so cross-chat leftovers are visible.

**Done-When:** `run_analysis` with no contract returns a refusal naming the exact next call; `get_workflow_state` shows all datasets with timestamps; and in a live Claude Desktop session the agent recovers on its first retry without an apology loop.

**Trap:** test recovery with the actual AI, not the Inspector. This gate exists to steer a non-deterministic agent, and the Inspector cannot tell you whether it worked.

---

### **Phase 5 — Profiling (2 days)**

**Files:** `profile/*.py`, `util/results.py`

Build `util/results.py` here — disk-write returning path \+ summary \+ preview \+ paging hint (Rule 4). Add `read_result_file`.

**Done-When:** runs on all fixtures plus an Olist table; correctly reports injected duplicates and nulls; a wide-table profile returns a path *with* a usable summary, never bare.

---

### **Phase 6 — Cleaning with the approval gate (3 days)**

**Files:** `clean/proposals.py`, `apply.py`, `ledger.py`

**Detection rules:** exact duplicate rows; near-duplicate keys; whitespace/case inconsistency; numbers stored as text; dates stored as text; impossible values; nulls above threshold; outliers; inconsistent units; encoding artifacts.

**Each proposal returns:**

ID:          C003

Action:      Trim whitespace and normalise case in \`region\`

Rows:        1,842 of 51,290 (3.6%)

Before:      "Maharashtra ", "maharashtra", "MAHARASHTRA" (3 variants)

After:       "Maharashtra" (1 variant)

Severity:    low

Reversible:  yes

**Use CTAS, never in-place `UPDATE`.** DuckDB enforces static column types. A cleaning action that turns `"$1,200"` into a decimal cannot be applied with `UPDATE` to a column already locked as `VARCHAR` — the type is fixed at table creation. Every applied action rebuilds the table:

CREATE OR REPLACE TABLE sales\_v3 AS

SELECT \* EXCLUDE (amount),

       CAST(REPLACE(REPLACE(amount,'$',''),',','') AS DECIMAL(18,2)) AS amount

FROM sales\_v2;

This also solves a problem the type issue obscures: **versioned tables give you true before/after for free**, which is exactly what the ledger needs. Keep `_v1`, `_v2`, `_v3` and record the version pair in each ledger entry. Cheap on DuckDB, and it makes the cleaning history genuinely auditable rather than merely described.

**Hard requirements:**

* The proposal path runs on a read-only connection so it *cannot* write even if buggy.  
* `apply_cleaning_plan` takes `approved_action_ids` and nothing else. Empty list is a no-op.  
* Ledger is append-only JSONL: timestamp, action ID, description, rows before, rows after, source version, target version, SQL executed.

**Done-When:** run on `mixed_types.xlsx`, approve 3 of 6, ledger shows exactly those 3 with correct counts, the other 3 provably did not run, and a text→decimal conversion succeeds (which it would not under `UPDATE`).

---

### **Phase 7 — Validation (1–2 days)**

PK uniqueness; grain check; null rules; range rules; referential integrity; date sanity; category domains; row count vs expectation.

**Done-When:** clean pass/fail table; correct failure on a deliberately broken fixture.

---

### **Phase 8 — Analysis Tiers 1–2 (3 days)**

Registry first — `analysis_type` → function, uniform signature. Every analysis returns `{result_table, method_note, caveats, n}`.

**Done-When:** all nine run on Olist and a fixture; unknown type returns the valid list.

**Trap:** an identifier must never be summed. The protection is real and the mechanism is not the one this line named until 20/09/2026: `role` is a proposal-time heuristic in `evidence.py` and never reaches a confirmed contract, so nothing can enforce `role=identifier` at analysis time. What does the work is narrower and stronger -- every analysis reads only the contract's declared measures, so a column nobody declared is out of scope whether or not anyone called it an identifier. 6.2's payoff is at proposal time, where `role` shapes what a person is offered to declare.

---

### **Phase 9 — Analysis Tiers 3–5 (4–5 days)**

Order: `calendar_coverage` → rest of Tier 3 → Tier 4 → Tier 5, `correlated_shift` last.

**Done-When:** `calendar_coverage` finds the missing month in your Olist window; `correlated_shift` fires on a deliberately injected synthetic shift.

---

### **Phase 10 — Analysis Tiers 6–7 (2–3 days)**

`uv add scipy statsmodels`

**Done-When:** `hypothesis_test` auto-selects and *names* the test for three input shapes; `cohort_retention` redirects to `repeat_behaviour` on a low-repeat dataset.

---

### **Phase 11 — Charts (1–2 days)**

`uv add matplotlib`. PNG into the workspace. Line, bar, grouped bar, scatter, histogram, box, heatmap, waterfall (for `mix_shift`).

Per Rule 4, return the path **plus a text description and key values** — the agent cannot see the PNG.

**Trap:** set the matplotlib backend to `Agg` explicitly. A GUI backend can hang your server on macOS.

---

### **Phase 12 — Report assembly (2 days)**

**Mandatory sections:** question asked; dataset and grain; data quality summary; **cleaning ledger**; validation results; findings with charts; method notes; caveats and exclusions; reproduction appendix listing exact tool calls.

Markdown first.

**Done-When:** one end-to-end run on `merged_multiheader.xlsx` produces a complete document.

---

### **Phase 13 — Eval harness (2–3 days) — DO NOT SKIP**

30–40 gold questions across the fixtures with known answers. Include cases where **the correct behaviour is to refuse or caveat** — a trend request on a dataset with a missing quarter must produce a gap warning, not a clean line.

**Three suites:**

* **Correctness** — does the number match?  
* **Behavioural** — does the agent recover from a gate refusal in one retry? Does it call `confirm_ingest_spec` without being told twice?  
* **Regression** — the specific traps: bounded fill, multi-header CSV typing, read-only merge parsing, CTAS type conversion. One test each, mapped to the FMR ID.

**Why this is the highest-value phase:** it turns "I built an AI thing" into "I measured whether it was right, and here is the number."

---

### **Phase 14 — Track B (3–4 days)**

Only after Phase 13 passes.

1. Switch transport to Streamable HTTP in `server.py`. One file, because there is no logic there.  
2. Pass the real per-user session ID as the workspace ID (the parameter from Phase 1 step 7). Verify with two concurrent browser sessions before anything else.  
3. Agent loop calling a free LLM API with your tool definitions. Google Gemini's free tier is the best default — as of August 2026, current Flash models are free with no card required. Groq is a good failover for speed; build a provider switch.  
4. Streamlit UI. Key screen is the **Ingest Spec editor** — grid preview as a clickable table with header-row multi-select and per-column role dropdowns. Same spec object, different front door.  
5. Host: Streamlit Community Cloud or Hugging Face Spaces, both free.  
6. Hosted Postgres: Neon — permanent free tier, no card, and unlike Supabase it does not pause after a week of inactivity, which would break your demo exactly when a recruiter opens it. Free tier is 0.5 GB per project; Aiven's 5 GB free plan is the alternative.  
7. Upload size cap in the UI matching the 4.2 gate. A public demo gets handed a garbage file within a day.

**Caution on free LLM tiers:** free no-card tiers are generally funded by your prompts being used for training. Fine for public demo data. Do not reuse that key for anything private; note it in the README.

---

### **Phase 15 — Cleaning in the web app, and helper columns (3–4 days)**

Only after Phase 14's UI steps. Decided 22/09/2026, from the graded bunty_babli runs (Cleanup Steps
8–11): every web answer had to carry "the figures include the 4 copied rows", because the browser
cannot clean (P14-O2), and questions like "category by month" or "order-size band by channel" had
no answer, because an analysis groups only by declared columns and month is not one.

**Part A — a Clean screen.** The engine side exists and is tested (Phase 6): proposal with exact
counts, approval by id, all-or-nothing apply, `_agent_history_<name>_vN` before-tables, the ledger,
staleness refusal. This part is a front door.

1. Backend: `propose_cleaning` and `apply_cleaning` on the web backend protocol, the real backend and
   the fake one.
2. A **Clean** screen between Upload & read and Contract: each action with its counts, its SQL and
   its sample; a lossy action (one that discards rows or values not declared missing) marked as such;
   a required order stated before the click, not only refused after it (convert after normalising).
3. After an apply: rows before and after, the ledger, and -- when a contract exists -- a pointer to
   the Contract screen to confirm a new version, because the row count moved. That is where a key
   the duplicates blocked (`primary_key=["order_id"]`) becomes statable.
4. The assistant stays read-only. It learns the screen exists (its screen list, the
   `SCREEN_FOR` notes) and can point to an action by id; it never applies one. A person approves
   every action -- no auto-approved "safe" subset.

**Part B — helper columns, as an approved derivation.**

5. A new cleaning action, `DERIVE_COLUMN`, proposed by a person (never detected, like
   `EXCLUDE_COLUMN`) and applied through the same gate: a CTAS rebuild, a history table, a ledger
   row carrying its SQL. The proposal shows the new column's definition in words, its SQL, how many
   rows get a value, how many come out null, and a sample.
6. **A fixed menu, not free SQL.** P8-D7 measured caller text reading `/etc/passwd` through
   `read_csv`, and `util/sql_guard.py` checks boolean predicates only. The menu, each kind writing
   its own definition sentence:
   - date part of the date column: month, quarter, year, weekday;
   - bands of a measure at stated edges (`order_value` at 1,000 and 5,000);
   - arithmetic on two numeric columns: product, ratio, difference (a ratio's zero denominator is
     null, and counted);
   - a flag: a column compared with a stated value;
   - a combination of two dimensions.
   Free-form expressions are for a later phase, Claude Desktop only, and only once `sql_guard` can
   check a value expression.
7. A new column is ADDITIVE drift, so the contract in force keeps working. The column is declared on
   the Contract screen as a dimension or a measure with a one-line definition, and only then can an
   analysis group by it or sum it -- the rule that every number traces to something a person signed.
8. Not duplicated: a month for a trend (`grain`), one month's top orders (`period`), bins of a measure
   (`distribution`, `bivariate`), outlier flags (`outlier_detection`) and weekday patterns
   (`seasonality`) already exist. A helper column is for grouping and measuring with them.

**Watch:** every clean and every derivation keeps a before-table, and abandoned web workspaces never
expire (P14-O1). Measure workspace sizes before public hosting.

**Done-When:** in the browser only, on `test_orders.csv`: drop the 4 exact copies (604 → 600),
confirm v2 with `primary_key=["order_id"]`, derive `order_month` from `order_date`, declare it, and
ask for order_value by category and month -- every figure matching SQL on the deduplicated rows, and
November 246,412.22 with no copies caveat. The ledger holds both actions with their SQL.

---

### **Phase 16 — Optional extras**

Forecasting, multi-dataset joins, scheduled runs. Out of scope until 0–15 are done.

---

## **12\. Progress Ledger — KEEP THIS UPDATED**

| Phase | Name | Status | Date | Notes |
| ----- | ----- | ----- | ----- | ----- |
| 0 | Environment | ☑ Done  |  27/08/2026|☑ Done  |
| 1 | Skeleton \+ connection | ☑ Done  | 27/08/2026 |  ☑ Done|
| 2 | Ingest \+ workspace isolation |  ☑ Done | 29/08/2026 |Steps 1–9 plus Step 0.5 (olist rebuilt into Homebrew PG17). 29/20 acceptance assertions pass. |
| 3 | Ingest Spec \+ merges |  ☑ Done | 30/08/2026 | Steps 1-8. 197 unit assertions, 31 acceptance assertions (35 with big_synthetic), Phase 2's 20 still passing. Live run recorded in phase3_step7.md. |
| 4 | Contract \+ workflow state | ☑ Done |  |  |
| 5 | Profiling |☑ Done| 01/09/2026| Steps 1-7, 9. 747 unit assertions, 42 acceptance assertions. Phase 2's 20, Phase 3's 35 and Phase 4's 50 still passing.|
| 6 | Cleaning gate | ☑ Done | 06/09/2026  | Step 1. 15 library-fact tests pinned; 762 unit assertions. Phase 2's 20, Phase 3's 35, Phase 4's 50 and Phase 5's 50 still passing. |
| 7 | Validation | ☑ Done| 08/09/2026  |  |
| 8 | Analysis T1–2 | ☑ Done | 13/09/2026 | Steps 1–10. Nine analyses across Tiers 1–2, the tool layer (compute_analysis), the MCP surface, and acceptance through a real DatasetContract and a real require_contract (closes P8-O10). 1,304 unit assertions; acceptance 99 passed, 0 failed, 2 skipped as of 21/09/2026, from 54/0/5. The Olist half closed then (P10-O3): all nine run against order_payments joined to orders and customers under a real contract. Column paging closed with it, though the skip itself survived until 21/09/2026 and the closure described work nothing performed (C80): the clause proved width, not paging. It now reads past the twelfth column -- measured, the result is 29 columns wide and start_col=13 returns columns 13 to 24 -- where no fixture pairing exceeded seven. Still skipped, each for a reason that holds: ANALYSIS_RESULT_UNSOUND (P10-D36 made every analysis report what it drops, so no real one loses rows silently -- C75 records the Phase 10 audit predicting otherwise), the rendered MCP schema (FastMCP builds it from the signature and no script reads what a client displays), The role trap is no longer among them: P10-O13 corrected this guide on 21/09/2026 and the clause became two checks against Olist, asserting that summary_stats reports the declared measure and names an undeclared identifier as not summarised rather than summarising it. |
| 9 | Analysis T3–5 | ☑ Done | 18/09/2026 | Steps 1–7. Twelve analyses across Tiers 3–5: calendar_coverage, trend, seasonality, period_compare, growth_decomposition; correlation, bivariate, driver_analysis, mix_shift; outlier_detection, changepoint, correlated_shift. Temporal ground facts pinned in tests/test_duckdb_temporal_facts.py, 21 tests, suite 1304 -> 1346; phase closes at 1,510, re-measured 18/09/2026. tests/test_phase9.py 19 passed, 0 failed, 0 skipped as of 21/09/2026, from 16/0/0; the three added are clause five, which is P9-O2. The Tier 5 skip is gone. Acceptance clause four runs correlated_shift over 99,441 Olist rows across a 26-month calendar: both injected series break after 2017-09, the coincidence rate is reported, 2016-11 narrows the comparison, and a control shift six months away is not reported as coincident. P9-D1 to P9-D66 logged (D35-D37 unassigned) with C36 to C60. P9-O1 closed. Open after the cleanup of 21/09/2026: P9-O4's feature half (scoped) and nothing else. O2, O3, O5, O6, O7, O8, O9, O10, O11 and O12 are closed. O4 (compute_analysis cannot reach an attached source, so server.py's in-place path serves inspection only) and O10 (the analysis count stated in two places, edited every step) grow with Phase 10. Steps 4a and 4b have no decisions.md section; P9-O7 was never assigned. |
| 10 | Analysis T6–7 | ☑ Done | 19/09/2026 | Steps 1–9. Ground facts pinned in five files; P10-D1 to D64 logged with C61 to C67. Tier 6: hypothesis_test over four measured branches (Welch, one-way ANOVA, chi-square, Mann-Whitney, with Kruskal-Wallis past two groups), confidence_interval (t for a mean, Wilson for a share), effect_size (Hedges' g, eta squared, Cramer's V), sample_adequacy (the detectable effect, never observed power). Tier 7: cohort_retention and repeat_behaviour, both refusing a key distinct per fact row. 27 analyses registered; none materialises a column. Unit suite 1,510 -> 1,661. Done-When proven on local Postgres: tests/test_phase10.py, 35 passed, 0 failed, 1 skipped -- three shapes each naming their test, the redirect firing at 3.119%, and repeat_behaviour reproducing Step 1 Part D's hand count of 96,096 people and 2,997 repeaters by an independent route. The skip is the non-finite screen, which needs a NaN written into the source database; it is covered in tests/test_nan_and_kruskal_facts.py. Closed: P10-O1, O2, O4, O5, O6, O8, O9, O10, O11, and P9-O10; then P10-O3 on 19/09/2026 and P10-O13 on 21/09/2026. Nothing recorded open against Phase 10. |
| 11 | Charts | ☑ Done | 21/09/2026 | Step 1: ground facts. matplotlib 3.11.2 added per line 943, with seven transitive packages. P11-D1 to D7 logged in tests/test_matplotlib_facts.py, 7 tests, suite 1665 -> 1672. Agg set before the pyplot import and get_backend() returns 'Agg' capitalised; all eight kinds draw from plain Python lists, so the chart layer owns no conversion and the numpy rule holds; a None is a gap in a line and a TypeError in a bar whose message names nothing, so render_chart screens nulls itself; figures accumulate until closed; two saves of identical data are byte-identical, so a chart is assertable by digest. Nothing in src/ changed -- this step measures and builds nothing. Step 2: charts/render.py, 443 lines, and tests/test_charts_render.py, 26 tests, suite 1672 -> 1698. P11-D8 to D15 with C82. All eight kinds draw from an Output; the parse back out of base.number()'s display strings lives in one function, because an Output holds strings and not floats (C82); nulls are screened here and reported, non-finite values refused; Chart has no accessor returning a bare path, per Rule 4; the figure closes in a finally and the refusal path is the one tested. Charts land in workspace/<id>/charts/. Step 3a: eight analyses were uncallable through compute_analysis -- six parameters undeclared and three declared but dropped (C83); the guard that should have caught it compared a hand-typed set with <=, and now derives the roster from REGISTRY (P11-D16). Step 3: render_chart registered, 30 parameters all forwarded; the gate/lookup/scope/run ladder extracted into _produce so both tools climb one copy; a chart refusal says the analysis ran and only the shape does not fit; matplotlib imported inside the function so the analysis package stays free of it. P11-D17 to D22 with C84. Suite 1699 -> 1712. Step 4: tests/test_phase11.py, the acceptance script -- all nine parameters C83 named through server.compute_analysis, all eight kinds through server.render_chart, and Rule 4 on a real PNG. 26 passed, 0 failed, 0 skipped, on the CSV fixture so it needs no Postgres. P11-O1 closed. It failed on its first run and found C85: growth_decomposition compared its reconciliation with exact equality, which refused every DOUBLE measure with a message printing both sides rounded so they read as identical; the tolerance now lives in base.py and mix_shift reads it too (P11-D23). P11-D1 to D26 with C82, C83, C84, C85. Suite 1712 -> 1714. |
| 12 | Report | ☑ Done | 21/09/2026 | Step 1: what each of the nine mandatory sections can be built from, measured rather than assumed. P12-D1 to D4. Five sections read records that already exist per dataset -- the cleaning ledger (which stores the SQL it ran), validation runs, profile runs and the stored contract -- so they are a formatting job. The question asked is stored nowhere and is an argument. A written result keeps its table and loses everything else: write_result puts headers and rows on disk and the summary only in the returned object, and no parameters at all, so nothing says which call produced a result file. ensure_table appears in five modules and the analysis tier is the one that records nothing. P12-O1 open: the reproduction appendix cannot be written from what exists, and recording analysis and chart runs the way the other five tiers record theirs is the answer that does not require trusting the agent's account of what it ran. Nothing in src/ changed. Step 2: analysis/runs.py, the sixth module to keep its own runs, recording what each analysis and chart was called with so call() renders an invocation rather than inferring one from a filename. One table with nullable chart columns, which is also the only thing linking a PNG to the analysis that drew it. Recorded after success, so a refused call records nothing. P12-D5 to D10, P12-O1 closed. Suite 1714 -> 1732. Step 3: report/assemble.py, the nine mandatory sections in Markdown, each from a record another phase wrote and none of them recomputed. A section with no record still appears and says why, and which were empty is carried on the Report rather than left to be counted. P12-D11 to D15. Suite 1732 -> 1748. Step 4: build_report registered, refusing only a dataset that is not loaded -- one with no contract is reported rather than refused, because 'no grain was agreed' is the finding (P12-D16). P12-D16 to D19. Suite 1748 -> 1759. Step 5: the Done-When. tests/test_phase12.py runs the whole pipeline on merged_multiheader.xlsx in one process -- ingest spec across a two-row merged header, profile, the one cleaning action the data needed (order_date is VARCHAR and the contract cannot be confirmed without it), contract, validation, seven analyses, a chart and the report. 36 passed, 0 failed, 0 skipped; the document is 8,748 bytes with all nine sections present and none empty. Everything below the load goes through server.py, because C83. P12-D20 to D23. |
| 13 | Eval harness | ☑ Done | 21/09/2026 | Step 1: the harness and the mechanism of all three suites, with 14 gold questions proving the format carries them -- filling to the guide's 30-40 is Step 2. SCORE 27/28 (96%): correctness 5/5, behavioural 18/19, regression 4/4 with one check per named Failure Mode Register id (F11, F12, F14, F15), all passing first run. P13-D1 to D4. The eval reports a score and exits zero on a wrong answer, because a suite that must be 100% cannot carry a number. Behavioural is measured without an agent, by executing the refusal's own NEXT STEP. Step 2 closed both: P13-O1, the prose moved from ten refusals into `detail` and the roster reads 26 of 37 (70%), the eleven remaining being the `(..., extra=...)` form and placeholders the caller must fill, both correct; P13-O2, the scripts point store.EXPORT_DIR at their own workspace rather than restoring docs/contracts by hand, which is shorter than the shared helper the item asked for. SCORE 31/31 (100%). P13-D5 with C87. Step 3 filled the set to 32 questions across three tables and both loaders -- 14 correctness, 12 behavioural, 6 regression over six FMR ids (F7, F9, F11, F12, F14, F15) -- including the guide's own caveat case, a month removed so calendar_coverage has a gap to name. SCORE 67/67 (100%). P13-D6, D7 with C88 and C89: a gold question accused the product of a defect it does not have, and a refusal named a recovery that was itself refused. Step 4: regression for the rest of the Failure Mode Register -- fourteen of fifteen ids, F2 left out because it is Track B with two or more users and not built. Every new check falsified before it was trusted (P13-D9), which caught one that could not fail (C91). 40 questions, SCORE 75/75 (100%). |
| 14 | Track B | ◐ In progress | 22/09/2026 | Step 1: ground facts, tests/test_track_b_facts.py, 6 tests over 7 facts. Same-process handles share a file; a read-only attach beside an open handle raises; a second process is locked out until the first closes; two workspaces on two threads are isolated; a second writer to one table is refused at its write and only it aborts (prediction was the commit -- wrong); each HTTP client has its own stable session id. Decides: serialise per workspace, one process, Track B ids never 'local'. P14-D1 to D8. Suite 1781 -> 1787. Step 2: the Streamlit UI on the fake backend -- sunset theme, the Journey written in ink on parchment, Upload & read, Contract, Ask, Files; 25 UI tests in ui/tests. DOMPurify drops a whole style block holding a tag-like '<' (C97: I first called it a race). P14-D9 to D13. Step 3: webapp/real_backend.py, the UI on the engine in-process under a per-workspace lock; unresolved contract fields reach the form blank (the engine holds a guessed grain); web exports stay in the workspace; the workspace id lives in the URL so a reload keeps it; looking creates nothing. P14-D17 to D21; P14-O1 open (abandoned workspaces never expire). Suite 1787 -> 1803. Step 4: the agent loop -- Gemini with Groq failover, no SDKs, models chosen from each provider's own list; twelve allowlisted read/analysis tools, workspace injected and never the model's; the suite now refuses network access after a leaked test key reached Gemini. Live run done: Gemini answered through four tool calls and every figure matched SQL; it surfaced C98 (the (all) roll-up drawn as a bar), fixed, plus model discovery, User-Agent, schema-dialect and rate-limit fixes. P14-D22 to D28, C98. Suite 1803 -> 1832. Step 5 (24/09/2026): P14-O1 closed -- idle ws_ workspaces expire after ANALYTICS_WORKSPACE_TTL_HOURS (72), use recorded by a .last_used marker because reads touch no file; P14-O2 closed -- a Clean screen on propose_cleaning/apply_cleaning, lossless steps pre-ticked, losses shown with samples. P14-D36, D37. Suite 1839 -> 1850, UI 43. Step 6: scripts/stress_matrix.py, 40 generated datasets through every tool, 1,634 calls -- 15 wrong, 2 crashes; 14 bugs in docs/stress/REPORT.md, registered as P14-O3 to O12 (P14-D38). Step 7: all 14 fixed -- CSV footers read by a seek, leading zeros counted as lost, Excel read data_only, statistics over finite values, empty sheets passed over, Latin-1 sniffed and paths redacted, text numbers and mixed dates converted by expression, pasted headers dropped; matrix 2 crashes / 15 wrong -> 0 / 9 load-time, each fixed by suggested cleaning. P14-D39 to D46, C100. Suite 1850 -> 1874. Step 8: the six residuals fixed (cp1252/UTF-16 via a UTF-8 copy, labelled total rows, Excel row numbers, two-reading numbers, quoted tails, finite ranges), then rounds 2-4 of new anomalies until a round found no bug: 16 more fixed, among them two-digit years silently read year-first, TIMESTAMPTZ crashing describe, 20-digit sums off by 34,650, mixed line endings, merged and error cells. 95 datasets, 0 crashes. P14-D47 to D64, C101. Suite 1874 -> 1896. Step 9: the first real-browser walk (headless Chromium, scripts/browser_check.py) -- no screen raised, but no analysis was reachable without a model key: an Explore screen now runs all 27 behind the same contract gate, plus sample workbook, readable buttons, DEMO.md. P14-D65 to D67, C102. Suite 1896 -> 1900, UI 49. Step 10: scripts/scenario_matrix.py -- 30 behaviour checks (lifecycles, hostile input, every MCP tool fuzzed, concurrency) found 5 bugs, all fixed: a reload showed a confirmed contract as blank, unknown params and non-workbooks crashed the web layer, report markup, unusable paths raised. 30/30. P14-D68 to D73, C103. Suite 1900 -> 1910, UI 50. Step 11: scripts/benchmark.py -- every MCP tool and all 27 analyses at 1k / 100k / 1M rows against ground truth computed independently: 1,932/1,932 right, deterministic, near-linear, the slowest analysis 0.41 s at 1M. 14 negatives written down, not fixed (docs/benchmark/BENCHMARK.md): a load_excel crash, Explore defaults outside the window, 2-9 s profiling, NEXT STEPs that ignore their WHY, display-text result files, contract exports outliving reset. P14-D74, D75, P14-O13 to O26 open, C104. Step 12: 12 of the 14 fixed and 2 closed by decision, nothing else moved: profiling 2.1 -> 0.44 s and the cleaning plan 3.4 -> 1.2 s at 100k (type reading per distinct value, thresholds decided on a sample first -- identical counts), NEXT STEPs that follow their WHY, heatmaps of one quantity with every cell in place, key findings that are findings, reset taking its exports. Benchmark 1,932/1,932 with 40 flags -> 15 (genuine chart choices); 4,336 stress records identical. P14-D76 to D80, C105, C106. Suite 1910 -> 1947. Step 13: scripts/domain_benchmark/ -- ten domains at 1k/100k/1M, stress to 10M, an oracle that never imports the engine: 6,159,473 checks, 0 incorrect once the oracle's own faults were fixed, 0 worker crashes; 16 product defects (D1-D16) found across it and Step 14, among them a concurrent result-name race (CRITICAL), an INT64 tie-term overflow at 2.2M ties, and a dedupe that made one total print two ways. Step 14: all 16 fixed, each with a test failing on the old code; profile_column 27x at 1M, 35x at 10M; scripts/targeted_regression/ 45/45 defect cases, 0 controls changed; H and J pass on the fixed code; an LLM benchmark (Gemini run, Groq blocked by the network policy). P14-D81 to D85, C107 to C111. Suite 1947 -> 1996. |
| 15 | Web cleaning \+ helper columns | ☐ Not started |  | Planned 22/09/2026 from the bunty_babli runs: a Clean screen on the Phase 6 engine (P14-O2 -- already closed by Phase 14 Step 5's Clean screen, found at the merge of 25/09/2026), then `DERIVE_COLUMN` from a fixed menu, declared in the contract. Section 11, Phase 15. |

`☐ Not started` / `◐ In progress` / `☑ Done`

---

## **13\. Locked decisions**

1. **DuckDB is the single analysis engine.** Postgres remains a first-class source connector.  
2. **CSV via DuckDB streaming reader; Excel via openpyxl read-only iterator. Pandas is not on the ingest path.**  
3. **Multi-row CSV headers: assemble names in Python, pass `names` \+ `skip` with `header=false`.** ✅ verified *(v1.2)*  
4. **Previews never load a whole file.**  
5. **A size gate runs before every load.** Refusing with guidance beats being OOM-killed.  
6. **The Ingest Spec is a contract** — chat in Track A, form in Track B, identical consuming code.  
7. **`header_rows` is a list.**  
8. **Merge fill is BOUNDED by real merge ranges. Unbounded fill is banned.** ✅ verified *(v1.2)*  
9. **Merged ranges are read by direct XML parse, never `ws.merged_cells` on a read-only worksheet.** ✅ verified *(v1.2)*  
10. **CSV headers are never auto-filled** — no merge metadata exists to bound the fill. *(v1.2)*  
11. **Nothing loads without `confirm_ingest_spec`.**  
12. **No analysis without a confirmed Dataset Contract.**  
13. **All state is partitioned by `dataset_name`. No ambient "current dataset".** *(v1.2)*  
14. **Workspace ID is a parameter, never generated at import.** Explicit `reset_workspace` tool. *(v1.2)*  
15. **Every gate refusal is instructional.**  
16. **Cleaning is a two-step gate**, proposal path read-only.  
17. **Cleaning uses CTAS with versioned tables, never in-place `UPDATE`.** *(v1.2)*  
18. **The ledger is append-only and ships inside every report.**  
19. **Pipe-delimited returns, capped 50 × 50\.**  
20. **No tool returns a bare file path.**  
21. **One DuckDB file per workspace**, with cleanup.  
22. **Postgres always `READ_ONLY`.**  
23. **Absolute paths in Claude Desktop config.**  
24. **Track A must pass Phase 13 before Track B starts.**  
25. **The eval harness is not optional.**

---

## **14\. Realistic timeline**

| Track | Phases | Focused days |
| ----- | ----- | ----- |
| Foundation | 0–4 | 10–13 |
| Core engine | 5–7 | 6–8 |
| Analysis | 8–10 | 9–11 |
| Output \+ proof | 11–13 | 6–7 |
| **Track A total** | **0–13** | **31–39** |
| Track B | 14 | 3–4 |

6–8 weeks evenings-and-weekends, 5–6 weeks focused. Track A alone is a complete portfolio project; Phase 13 is a legitimate stopping point.

---

## **15\. What to do right now**

1. Read this end to end once. Do not skim 4.2.1, 6.3, 6.6, or 6.7.  
2. Commit this file as `docs/analytics_agent_build_guide.md` **before writing any code**. That commit is your plan-lock.  
3. Start Phase 0\.  
4. When Phase 0 passes, tick the ledger, commit, and open a fresh chat: *"Here is my build guide. Phase 0 complete. Start me on Phase 1."*

---

## **16\. Failure Mode Register**

Put this table in the README. It is a strong interview artifact on its own — it shows you thought about how the system breaks, not just how it works.

| \# | Failure | Where it bites | Mitigation | Phase |
| ----- | ----- | ----- | ----- | ----- |
| F1 | Agent skips a gate, then loops on apologies | Any gated tool | Instructional refusals \+ `get_workflow_state` \+ ordering in docstrings | 4 |
| F2 | `IO Error: Could not set lock on file` | Track B, 2+ users | Workspace-scoped DuckDB from day one | 2 |
| F3 | Process OOM-killed mid-load | Large file on 8GB M1 | Streaming readers, size gate, explicit refusal | 2, 3 |
| F4 | Agent misreads a wide result table | Large inline returns | Pipe-delimited, 50×50 cap, answers not raw rows | 1, 8 |
| F5 | Preview loads whole file to show 15 rows | Every large file | Read-only iterators, 200-line / 20-row caps | 3 |
| F6 | Merged or multi-row headers break the loader | Corporate Excel | `header_rows` list, `header_join`, bounded fill, reported | 3 |
| F7 | Agent hallucinates contents of a file it got a path to | Any disk-write | No bare paths; summary \+ preview \+ `read_result_file` | 5 |
| F8 | Pivot dump misread as data | Corporate Excel | Detection \+ warning; no auto-unpivot | 3 |
| F9 | Silent type coercion hides bad data | Every load | Per-column coercion failure counts | 3 |
| F10 | Trend computed across missing periods | Temporal analysis | `calendar_coverage` mandatory before Tier 3 | 9 |
| **F11** | `AttributeError` on `ws.merged_cells` in read-only mode | Excel header preview | Parse `<mergeCells>` from sheet XML with `iterparse` ✅ | 3 |
| **F12** | Multi-row CSV header → all columns VARCHAR, header row becomes data | DuckDB `read_csv` | Assemble `names` in Python, `header=false` \+ `skip` ✅ | 2, 3 |
| **F13** | Stdio process persists across chats; state leaks between tasks | Claude Desktop | Per-dataset partitioning \+ `reset_workspace` \+ load timestamps | 1, 2, 4 |
| **F14** | Unbounded merge fill silently mislabels columns | Excel multi-row headers | Fill only within real merge ranges ✅ | 3 |
| **F15** | `UPDATE` fails on type change (VARCHAR → DECIMAL) | Cleaning apply | CTAS with versioned tables | 6 |

✅ \= mitigation verified by execution, not reasoning.

---

*End of guide.*

