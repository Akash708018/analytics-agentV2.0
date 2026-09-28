"""What a caller's SQL text is allowed to be before it reaches a connection.

Phase 8, Step 1 measured the reason this file exists. A contract carries
`known_exclusions[].rule` as free text -- "status = 'cancelled'" -- and an
analysis has to put that text inside a WHERE clause. Measured on DuckDB 1.5.5:

    con.execute("SELECT count(*) FROM ex WHERE NOT (1=1); DROP TABLE ex;")

dropped the table (P8-D7). A read-only handle refuses the DROP and then runs
the second statement anyway, and reads any file the process can open --
/etc/passwd came back with 142 rows through read_csv. Read-only guards the
catalog, not the filesystem, so it is the second line and this is the first.

Nothing here executes SQL. `check_predicate` parses; `bind_predicate` is the
only function that touches a connection, and it asks the binder a question
rather than running the text against the data.
"""

from __future__ import annotations

import json
import threading

import duckdb

__all__ = [
    "UnsafeSQL",
    "quote_identifier",
    "check_predicate",
    "bind_predicate",
    "negate",
]


class UnsafeSQL(ValueError):
    """Raised when caller-supplied SQL text may not be used.

    A `ValueError`, not a `Refusal`. util/ sits below state.py and importing
    it would invert the dependency; the caller that has a Reason to hand wraps
    this message in the instructional refusal 8.2 requires.
    """


def quote_identifier(name: str) -> str:
    """A column or table name, safe to interpolate.

    DuckDB 1.5.5 has no `quote_identifier` function -- only `json_quote` -- and
    `format` does not quote: format('SELECT "{}"', 'we"ird') returns
    SELECT "we"ird", which is three identifiers and a syntax error. Measured in
    Step 1, G11. So the doubling is done here, once.
    """
    if not isinstance(name, str) or not name:
        raise UnsafeSQL("an identifier cannot be empty.")
    if "\x00" in name:
        raise UnsafeSQL("an identifier cannot contain a null byte.")
    return '"' + name.replace('"', '""') + '"'


def _engine_message(exc: Exception) -> str:
    """The part of a DuckDB error a reader can act on.

    A BinderException carries the offending column, then a candidate binding,
    then the internal query text with a caret under it -- and that query is one
    this file wrote, not one the reader did. Showing it invites a correction to
    a line nobody typed. Everything up to the LINE marker is kept, which keeps
    "Candidate bindings: status" -- the sentence that names the typo.
    """
    lines = []
    for line in str(exc).splitlines():
        if line.startswith("LINE "):
            break
        if line.strip():
            lines.append(line.strip())
    return " ".join(lines) or str(exc).splitlines()[0]


def _wrapped(rule: str) -> str:
    """The rule as the smallest statement that has to parse for it to be used.

    Parenthesised on purpose. A rule ending in a line comment -- `x = 1 --` --
    swallows the closing paren, so the wrapper fails to parse and the rule is
    refused rather than silently truncating whatever it was appended to.
    """
    return f"SELECT 1 WHERE ({rule})"


# One parser connection per thread: a DuckDB connection is not safe to execute on from two
# threads at once, and ten threads sharing one took each other's results (IndexError on an
# empty fetch; Step 13 benchmark, H_concurrency).
_PARSER = threading.local()


def _ast(sql: str) -> dict:
    """The parse tree of `sql`, without running it.

    `json_serialize_sql` is a scalar function, so it needs a connection -- a
    private in-memory one, never the caller's. It has no catalog, which is the
    point: this reads the shape of the text, and nothing it returns depends on
    what is loaded.
    """
    con = getattr(_PARSER, "con", None)
    if con is None:
        con = _PARSER.con = duckdb.connect()
    raw = con.execute("SELECT json_serialize_sql(?)", [sql]).fetchall()[0][0]
    tree = json.loads(raw)
    if tree.get("error"):
        raise UnsafeSQL(
            f"this rule is not a usable SQL expression: "
            f"{tree.get('error_message', 'it did not parse')}"
        )
    return tree


def _classes(node):
    """Every `class` in a parse tree, depth first."""
    if isinstance(node, dict):
        if node.get("class"):
            yield node["class"]
        for value in node.values():
            yield from _classes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _classes(value)


def check_predicate(rule: str) -> str:
    """Return `rule` if it is one boolean expression, or raise.

    Three questions, all asked without the caller's connection and without
    executing anything:

    1. Does the rule stay one statement when wrapped? `duckdb.extract_statements`
       returned 2 for the injected string in Step 1 and 1 for a lone statement.
    2. Does the wrapped form parse at all?
    3. Does it contain a subquery?

    The third is not paranoia about a shape somebody might use. Measured: the
    rule

        1=1) OR (SELECT count(*) FROM read_csv('/etc/passwd')) > 0 AND (1=1

    is ONE statement, parses, binds against the table, and types as BOOLEAN --
    it passes every other check in this file and reads the password file. A
    rule says which of THIS table's rows are excluded; it has no business
    opening a second relation. The test is structural, on the parse tree,
    because `status = 'SUBQUERY'` contains the word and is a perfectly ordinary
    rule.

    What this still cannot see is whether the columns exist or whether the
    expression is boolean -- both need the table. `bind_predicate` asks those.
    """
    if not isinstance(rule, str) or not rule.strip():
        raise UnsafeSQL("an exclusion rule cannot be empty.")
    if "\x00" in rule:
        raise UnsafeSQL("an exclusion rule cannot contain a null byte.")

    wrapped = _wrapped(rule)
    try:
        statements = duckdb.extract_statements(wrapped)
    except Exception as exc:  # a parser error, and the message names the spot
        raise UnsafeSQL(
            f"this rule is not a usable SQL expression: {rule!r}. "
            f"DuckDB could not parse it -- {_engine_message(exc)}"
        ) from exc

    if len(statements) != 1:
        raise UnsafeSQL(
            f"this rule is {len(statements)} SQL statements, not an expression: "
            f"{rule!r}. A rule is the text after WHERE -- it cannot carry a "
            f"second statement, and one that does is not run."
        )

    if "SUBQUERY" in set(_classes(_ast(wrapped))):
        raise UnsafeSQL(
            f"this rule contains a subquery: {rule!r}. A rule says which rows "
            f"of this table are excluded, so it reads this table and nothing "
            f"else -- a subquery can open any relation the process can reach, "
            f"including a file on disk."
        )
    return rule


def bind_predicate(con, table: str, rule: str) -> str:
    """Return `rule` if it binds against `table` as a BOOLEAN, or raise.

    `check_predicate` first, so nothing that failed the parse reaches a
    connection. Then two things the parser cannot know:

    * every column named exists -- an unknown one raises BinderException here
      rather than halfway through an analysis;
    * the expression is BOOLEAN. `WHERE NOT (amt)` on an INTEGER column bound
      cleanly in Step 1 (G9) and returned zero rows -- a rule that filters
      everything and reports nothing wrong. typeof() is asked instead of
      trusting the WHERE clause to complain.

    LIMIT 0 -- the binder answers, the data is not read.
    """
    check_predicate(rule)
    ident = quote_identifier(table)
    try:
        rows = con.execute(
            f"SELECT typeof(({rule})) FROM {ident} LIMIT 1"
        ).fetchall()
    except Exception as exc:
        raise UnsafeSQL(
            f"this rule does not apply to {table}: {rule!r} -- "
            f"{_engine_message(exc)}"
        ) from exc

    if not rows:
        return rule  # empty table: nothing to type, and nothing to filter
    dtype = rows[0][0]
    if dtype != "BOOLEAN":
        raise UnsafeSQL(
            f"this rule has type {dtype}, not BOOLEAN: {rule!r}. A rule says which "
            f"rows are excluded, so it has to be true or false about a row -- "
            f"`status = 'cancelled'`, not `status`."
        )
    return rule


def negate(rule: str) -> str:
    """The rows a rule does NOT exclude, counting the ones it cannot judge.

    P8-D6, measured: on four rows with one cancelled and one NULL status,
    `WHERE NOT (status = 'cancelled')` kept two. NULL is not false, so the row
    the rule could not judge was dropped without being excluded by anybody.
    coalesce(..., false) keeps it, and the analysis reports it separately.
    """
    return f"NOT coalesce(({rule}), false)"
