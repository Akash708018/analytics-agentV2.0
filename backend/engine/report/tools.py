"""The caller's side of the report: refusals in this codebase's shape, and the Rule 4 envelope.

Everything below `assemble` returns objects and raises; everything above it returns text a
caller reads. This is that boundary, the way analysis/tools.py is for analyses.

**It refuses only when the dataset is not loaded.** `validate_dataset` refuses a dataset with no
contract and gives a good reason -- a validation report with nothing to test against "would be a
page of NOT RUN". A report is the opposite case. P12-D11 decided every section appears and says
why it is empty, and a report of a dataset whose grain nobody agreed is still a report: it says
the grain was never agreed, which is the most important thing a reader could be told about the
numbers in it. Refusing would withhold exactly that.
"""

from __future__ import annotations

from datetime import datetime

from ..contract.refusals import Reason, Refusal
from ..util import db
from . import assemble


def _not_loaded(con, dataset_name: str) -> str:
    available = ", ".join(db.user_tables(con)) or "(none loaded)"
    return Refusal(
        reason=Reason.DATASET_NOT_LOADED,
        what=f"there is no dataset called '{dataset_name}' in this workspace.",
        why=(
            "a report describes a loaded table and the records kept against it -- the "
            "contract, the profile, the cleaning ledger, the validation runs and every "
            "analysis. With no table there is nothing any of those refer to."
        ),
        state=f"loaded: {available}",
        detail="list_datasets() shows what is already here.",
        next_call="list_datasets()",
    ).to_text()


def build_report(
    con,
    workspace_id: str,
    dataset_name: str,
    question: str,
    now: datetime | None = None,
) -> str:
    """Assemble the report and describe it. Returns the envelope, never a path."""
    if dataset_name not in db.user_tables(con):
        return _not_loaded(con, dataset_name)

    report = assemble.assemble(
        con, workspace_id, dataset_name=dataset_name, question=question, now=now
    )
    return report.to_text()


__all__ = ["build_report"]
