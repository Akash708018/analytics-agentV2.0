"""Phase 10 acceptance test. Run from the repo root:

    uv run python tests/test_phase10.py

Asserts the Done-When from the build guide:

    hypothesis_test auto-selects and NAMES the test for three input shapes;
    cohort_retention redirects to repeat_behaviour on a low-repeat dataset

and P10-O11: that the Tier 7 analyses reproduce, on the real database, the figures Step 1 Part D
measured by hand -- 96,096 people, 2,997 repeaters, 3.119%.

**Three tables, where Phase 9 used one.** Phase 9's helpers close over a module-level TABLE.
These take it as an argument, because Tier 6 needs a numeric measure and Tier 7 needs a person
key and a date that live in different tables:

  order_payments   payment_value is numeric; orders is three VARCHARs and five TIMESTAMPs
  orders x customers  the person is customers.customer_unique_id, the date is on orders

**The join is copied in, and P9-O4 is why.** Querying Olist in place returns DATASET_NOT_LOADED
because state._loadable_tables reads db.user_tables, so an attached catalog is not a loaded
dataset. Phase 9 copied one table for that reason; this copies two and joins them in the
workspace, which is still the path a person actually takes.

**installment_plan is derived, not injected.** The Welch branch needs a dimension with exactly
two values and Olist's payment table has none. payment_installments > 1 is a real distinction
computed from real rows. Phase 9 injected two wholly synthetic measures for its clause four and
recorded that as a finding; this is a smaller departure and is recorded the same way.

**It SKIPs rather than fails when the database is not there**, for the reason Phase 9 gives:
a test that reaches a live local database and errors everywhere else is worse than no test.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine import workspace  # noqa: E402
from backend.engine.analysis import tools as analysis_tools  # noqa: E402
from backend.engine.contract import store  # noqa: E402
from backend.engine.contract.dataset_contract import (  # noqa: E402
    Binding,
    DatasetContract,
    Measure,
)
from backend.engine.ingest.csv_loader import LoadRefused  # noqa: E402
from backend.engine.ingest.postgres import load_table  # noqa: E402
from backend.engine.util import db  # noqa: E402

WORKSPACE = "phase10_test"
SOURCE = "olist"
PAYMENTS = "payments"
JOINED = "customer_orders"

# Measured by hand in Phase 10 Step 1 Part D, 18/09/2026, against the same database:
#   99,441 orders, 99,441 distinct customer_id, 96,096 distinct customer_unique_id
#   keyed on the person: 2,997 repeaters, 3.119%
#   keyed on the order:  0 repeaters,     0.000%
PEOPLE = 96096
REPEATERS = 2997
RATE = "3.119%"
ORDERS_ROWS = 99441

PASSED = 0
FAILED = 0
SKIPPED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED += 1
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))


def skip(label: str, why: str) -> None:
    global SKIPPED
    SKIPPED += 1
    print(f"  SKIP  {label}  ({why})")


def heading(text: str) -> None:
    print()
    print(text)
    print("-" * len(text))


def confirm_contract(table: str, grain: str, primary_key: list[str],
                     date_column: str | None, measures: list, dimensions: list) -> object:
    """A real contract on a copied table, confirmed through the store.

    Phase 9's version closes over a module-level TABLE; this takes one, because Phase 10 needs
    three. Everything else is the same path: pairs and row count read from the workspace, a real
    Binding, and store.confirm.
    """
    con = db.connect(WORKSPACE)
    try:
        pairs = [
            (r[0], r[1])
            for r in con.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = ? ORDER BY ordinal_position",
                [table],
            ).fetchall()
        ]
        rows = con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        contract = DatasetContract(
            dataset_name=table,
            grain=grain,
            primary_key=primary_key,
            date_column=date_column,
            measures=measures,
            dimensions=dimensions,
            bound_to=Binding.from_pairs(pairs, rows),
        )
        return store.confirm(con, contract)
    finally:
        con.close()


def run(table: str, analysis_type: str, **params) -> str:
    """One analysis on a short-lived connection, through the real gate."""
    con = db.connect(WORKSPACE)
    try:
        return analysis_tools.compute_analysis(
            con, WORKSPACE, table, analysis_type, **params
        )
    finally:
        con.close()


def attempt(table: str, analysis_type: str, **params) -> str:
    """The rendered result, or the exception's text. Refusals are the subject of four clauses
    below, and a refusal arrives as a raise rather than as a string."""
    try:
        return run(table, analysis_type, **params)
    except Exception as exc:  # noqa: BLE001 - the message is what is under test
        return f"{type(exc).__name__}: {exc}"


def mount() -> str | None:
    """Copy in what the clauses need, or the reason there is nothing to test on.

    order_payments carries the numeric measure. orders and customers are joined into one table,
    because the person key and the date are on different sides of it.
    """
    con = db.connect(WORKSPACE)
    try:
        load_table(con, SOURCE, "order_payments")
        load_table(con, SOURCE, "orders")
        load_table(con, SOURCE, "customers")

        con.execute(
            f'CREATE OR REPLACE TABLE "{PAYMENTS}" AS SELECT *, '
            f"CASE WHEN payment_installments > 1 THEN 'instalments' ELSE 'single' END "
            f'AS installment_plan FROM "order_payments"'
        )
        con.execute(
            f'CREATE OR REPLACE TABLE "{JOINED}" AS '
            f"SELECT o.order_id, o.customer_id, c.customer_unique_id AS person, "
            f"       o.order_purchase_timestamp AS placed "
            f'FROM "orders" o JOIN "customers" c USING (customer_id)'
        )
        payments = con.execute(f'SELECT count(*) FROM "{PAYMENTS}"').fetchone()[0]
        joined = con.execute(f'SELECT count(*) FROM "{JOINED}"').fetchone()[0]
        if not payments:
            return f"{SOURCE}.public.order_payments is empty"
        if joined != ORDERS_ROWS:
            return (f"the join gave {joined:,} rows against {ORDERS_ROWS:,} orders, so "
                    f"customer_id is not one-to-one and the figures below do not apply")
        return None
    except LoadRefused as exc:
        return str(exc).splitlines()[0]
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    finally:
        con.close()


def bind_payments() -> None:
    confirm_contract(
        PAYMENTS,
        grain="one row = one payment on an order",
        primary_key=["order_id", "payment_sequential"],
        date_column=None,
        measures=[Measure(name="payment_value", agg="mean",
                          definition="what the payment came to")],
        dimensions=["payment_type", "installment_plan"],
    )


def bind_joined() -> None:
    confirm_contract(
        JOINED,
        grain="one row = one order",
        primary_key=["order_id"],
        date_column="placed",
        measures=[Measure(name="order_id", agg="count", definition="orders placed")],
        dimensions=["person", "customer_id"],
    )


# --------------------------------------------------------------------------
# Clause 1: the Done-When's first half -- three shapes, each test named
# --------------------------------------------------------------------------


def clause_one() -> None:
    heading("Clause 1: hypothesis_test names the test it ran, on three shapes")
    bind_payments()

    two = attempt(PAYMENTS, "hypothesis_test",
                  dimension="installment_plan", measure="payment_value")
    check("two groups run Welch and say so",
          "Welch's unequal-variance t-test" in two,
          "installment_plan has two values by construction")
    check("Welch is distinguished from Student by name",
          "not assumed to share a variance" in two)
    check("the df is reported beside the test",
          "df " in two and "p " in two)

    many = attempt(PAYMENTS, "hypothesis_test",
                   dimension="payment_type", measure="payment_value")
    check("more than two groups run one-way ANOVA and say so",
          "one-way ANOVA (F test)" in many)
    check("ANOVA reports both degrees of freedom",
          "df " in many and ", " in many.split("df ", 1)[-1][:12])
    check("ANOVA says what it spared the reader",
          "pairwise tests" in many)

    table = attempt(PAYMENTS, "hypothesis_test",
                    dimension="payment_type", second_dimension="installment_plan")
    check("two dimensions run chi-square and say so",
          "chi-square test of independence" in table)
    check("the continuity correction is stated either way",
          "continuity correction" in table)
    check("the expected-count screen is ours and is reported",
          "Smallest expected count" in table)

    check("every shape reported an assumption rather than a verdict",
          all("Assumption reported, not judged" in text for text in (two, many)),
          "P10-D14: skew, kurtosis and n, never a normality p-value")
    check("no shape printed a p-value of zero",
          not any(" p 0." == t[:4] for t in (two, many, table)),
          "P10-D13: a floor, never the string 0")


# --------------------------------------------------------------------------
# Clause 2: the rest of Tier 6, on real rows
# --------------------------------------------------------------------------


def clause_two() -> None:
    heading("Clause 2: confidence_interval, effect_size and sample_adequacy on real rows")

    interval = attempt(PAYMENTS, "confidence_interval",
                       dimension="payment_type", measure="payment_value")
    check("the interval around a mean is the t form",
          "Student's t on n - 1 degrees of freedom" in interval)
    check("it says what a 95% interval means",
          "across repeated samples" in interval)

    shares = attempt(PAYMENTS, "confidence_interval", dimension="payment_type")
    check("the interval around a share is Wilson",
          "Wilson" in shares and "Not Wald" in shares)

    size = attempt(PAYMENTS, "effect_size",
                   dimension="installment_plan", measure="payment_value")
    check("two groups get Hedges' g", "Hedges' g" in size)
    check("the band is called a convention",
          "vocabulary, not a verdict" in size)

    association = attempt(PAYMENTS, "effect_size",
                          dimension="payment_type", second_dimension="installment_plan")
    check("two dimensions get Cramer's V", "Cramer's V" in association)

    adequacy = attempt(PAYMENTS, "sample_adequacy",
                       dimension="installment_plan", measure="payment_value")
    check("sample_adequacy reports a detectable effect",
          "could reliably detect" in adequacy)
    check("and refuses to report observed power",
          "Observed power is not reported" in adequacy,
          "P10-D18: it is the p-value rearranged")


# --------------------------------------------------------------------------
# Clause 3: P10-O11 -- the hand-measured figures, reproduced by the modules
# --------------------------------------------------------------------------


def clause_three() -> None:
    heading("Clause 3: repeat_behaviour reproduces what Step 1 Part D measured by hand")
    bind_joined()

    text = attempt(JOINED, "repeat_behaviour", entity="person")
    check(f"{PEOPLE:,} distinct people", f"{PEOPLE:,} distinct person value(s)" in text,
          "Step 1 Part D counted customer_unique_id")
    check(f"{REPEATERS:,} came back", f"{REPEATERS:,} came back" in text)
    check(f"a repeat rate of {RATE}", RATE in text)
    check("the people column is not claimed to be a row count",
          "does not add to a row count" in text)


# --------------------------------------------------------------------------
# Clause 4: the Done-When's second half -- the redirect, on real low-repeat data
# --------------------------------------------------------------------------


def clause_four() -> None:
    heading("Clause 4: cohort_retention redirects to repeat_behaviour at 3.119%")

    text = attempt(JOINED, "cohort_retention", entity="person")
    check("a grid was drawn", "cohort(s) covering" in text)
    check("it warns that the shape stops saying anything",
          "stops saying anything" in text,
          f"{RATE} is below the guide's 5%")
    check("it names repeat_behaviour", "repeat_behaviour" in text)
    check("it says why a grid misleads here",
          "a curve that is not there" in text)
    check("the cells are people and it says so",
          "Cells are people, not percentages" in text)
    check(f"the cohorts hold {PEOPLE:,} people between them",
          f"{PEOPLE:,} distinct person value(s)" in text)


# --------------------------------------------------------------------------
# Clause 5: the trap -- a key that is distinct per row
# --------------------------------------------------------------------------


def clause_five() -> None:
    heading("Clause 5: both Tier 7 analyses refuse customer_id by name")

    for analysis in ("repeat_behaviour", "cohort_retention"):
        text = attempt(JOINED, analysis, entity="customer_id")
        check(f"{analysis} refuses customer_id",
              "one per row" in text,
              f"{ORDERS_ROWS:,} distinct across {ORDERS_ROWS:,} rows")
        check(f"{analysis} says what the column is, not what it cannot do",
              "describes events" in text or "fact about the column" in text)
        check(f"{analysis} did not return a figure",
              "0.000%" not in text,
              "Step 1 Part D: keyed on the order, the answer is a clean, confident zero")


# --------------------------------------------------------------------------
# Clause 6: what cannot be proved here
# --------------------------------------------------------------------------


def clause_six() -> None:
    heading("Clause 6: the non-finite screen")
    skip("a NaN in a real measure",
         "putting one in Olist means writing to the source database. The screen and what "
         "each aggregate does with a NaN are covered in tests/test_nan_and_kruskal_facts.py, "
         "measured rather than asserted")


def main() -> int:
    print("Phase 10 acceptance: Tiers 6 and 7 on a real warehouse table")

    workspace.reset(WORKSPACE)
    try:
        why = mount()
        if why:
            skip("all six clauses", why)
        else:
            clause_one()
            clause_two()
            clause_three()
            clause_four()
            clause_five()
            clause_six()
    finally:
        workspace.reset(WORKSPACE)

    print()
    print(f"{PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
    if SKIPPED:
        print("A skip is an outstanding clause, not a passing one.")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
