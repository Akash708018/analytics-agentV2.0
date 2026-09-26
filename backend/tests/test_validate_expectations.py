"""Rules every row must satisfy, declared in the contract and counted by validation.

Cleanup Step 13, RF-O7. The retail run's 8 negative quantities, 13 deliveries before the order and
37 sales after a rep's exit all passed as "all 6 check(s) passed": nothing could state them. Run
through server.py end to end -- load, propose with expectations, confirm, validate.
"""

from __future__ import annotations

import pytest

from backend.engine import server, workspace
from backend.engine.contract import store
from backend.engine.contract.refusals import Reason, reason_of

W = "expectations_test"
CSV = """line_id,units,shipped,ordered
L1,2,2024-01-05,2024-01-03
L2,-1,2024-01-06,2024-01-04
L3,,2024-01-07,2024-01-05
L4,3,2024-01-01,2024-01-06
L5,1,,2024-01-07
"""
BASE = dict(grain="one row = one line", primary_key=["line_id"], date_column="ordered",
            measures=["units"], aggregations={"units": "sum"},
            measure_definitions={"units": "items"},
            analysis_window_start="2024-01-01", analysis_window_end="2024-01-31")


@pytest.fixture()
def loaded(tmp_path, monkeypatch):
    workspace.reset(W)
    monkeypatch.setattr(store, "EXPORT_DIR", workspace.workspace_dir(W) / "contracts")
    p = tmp_path / "lines.csv"
    p.write_text(CSV)
    server.load_csv(path=str(p), dataset_name="lines", workspace_id=W)
    yield
    workspace.reset(W)


def propose(**kw):
    return server.propose_dataset_contract(dataset_name="lines", workspace_id=W, **BASE, **kw)


def confirm(text):
    body = text.rsplit("```", 2)[-2]
    body = body[4:] if body.startswith("json") else body
    return server.confirm_dataset_contract(contract_json=body, workspace_id=W)


RULES = [{"rule": "units > 0", "reason": "a line sells at least one item"},
         {"rule": "shipped >= ordered", "reason": "nothing ships before it is ordered"}]


def test_a_rule_counts_failures_and_leaves_nulls_unchecked(loaded):
    confirm(propose(expectations=RULES))
    report = server.validate_dataset(dataset_name="lines", workspace_id=W)
    units = next(ln for ln in report.splitlines() if ln.startswith("| Rows satisfy a rule")
                 and "units > 0" in ln)
    # 5 rows: L1, L4, L5 pass; L2 fails; L3's units is NULL -- not judged, not passed.
    assert "| 5 | 3 | 1 | 1 |" in units, units
    assert "L2" in report and "a line sells at least one item" in report


def test_a_second_rule_is_its_own_check(loaded):
    confirm(propose(expectations=RULES))
    report = server.validate_dataset(dataset_name="lines", workspace_id=W)
    ship = next(ln for ln in report.splitlines() if "shipped >= ordered" in ln
                and ln.startswith("| Rows satisfy a rule"))
    assert "| 5 | 3 | 1 | 1 |" in ship, ship     # L4 ships before it is ordered; L5 unshipped
    assert "L4" in report


def test_a_rule_naming_a_column_the_table_lacks_is_refused_at_proposal(loaded):
    text = propose(expectations=[{"rule": "qty > 0", "reason": "typo"}])
    assert reason_of(text) is Reason.CONTRACT_INVALID, text.splitlines()[:3]


def test_a_rule_that_opens_another_relation_is_refused(loaded):
    rule = "units > 0 OR (SELECT count(*) FROM read_csv('/etc/passwd')) > 0"
    text = propose(expectations=[{"rule": rule, "reason": "no"}])
    assert reason_of(text) is Reason.CONTRACT_INVALID


def test_the_rules_are_part_of_the_stored_contract(loaded):
    confirmed = confirm(propose(expectations=RULES))
    assert "Rules every row must satisfy:" in confirmed
    assert "units > 0 -- a line sells at least one item" in confirmed
