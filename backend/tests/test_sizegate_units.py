"""The size gates in the units a person's own machine reports (Cleanup Step 12, RF-O2).

The retail run's refuse file was 2,595,199,347 bytes: 2.60 GB as macOS Finder and `ls` report it,
2.42 GiB. The gates were built from 1024-powers and printed as "GB", so the file was warned about as
"2.4 GB ... Loading will work" under a limit the messages called "2.5 GB". Sparse files make the
sizes here without writing the bytes; the gate reads stat() and nothing else before its verdict.
"""

from __future__ import annotations

from backend.engine.config import SIZE_GATES, human_bytes
from backend.engine.ingest import sizegate


def sparse(tmp_path, name: str, size: int):
    p = tmp_path / name
    with open(p, "wb") as f:
        f.truncate(size)
    return p


def test_bytes_are_printed_in_decimal_units():
    assert human_bytes(2_595_199_347) == "2.6 GB"
    assert human_bytes(250_000_000) == "250.0 MB"
    assert human_bytes(999) == "999 B"


def test_the_gates_are_whole_decimal_units():
    assert SIZE_GATES.csv_refuse_bytes % 10**8 == 0
    assert SIZE_GATES.csv_warn_bytes % 10**6 == 0


def test_a_file_over_the_limit_is_refused_naming_the_limit(tmp_path):
    p = sparse(tmp_path, "big.csv", SIZE_GATES.csv_refuse_bytes + 1)
    r = sizegate.check_file(p, "csv")
    assert r.verdict is sizegate.Verdict.REFUSE
    assert human_bytes(SIZE_GATES.csv_refuse_bytes) in r.message


def test_a_warning_names_the_threshold_it_crossed(tmp_path):
    p = sparse(tmp_path, "warn.csv", SIZE_GATES.csv_warn_bytes + 1)
    r = sizegate.check_file(p, "csv")
    assert r.verdict is sizegate.Verdict.WARN
    assert f"over the {human_bytes(SIZE_GATES.csv_warn_bytes)} warning threshold" in r.message
