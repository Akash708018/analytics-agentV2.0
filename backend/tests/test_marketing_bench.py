"""The floor under scripts/marketing_bench.py (25/09/2026): on its marketing tables no strong
suggestion is wrong, every expected caveat is found and recounts true, a model that sums
everything is left with nothing wrong that is marked checked, every per-channel CTR, ROAS and
spend the engine reports equals SQL written apart, a total of a ratio is refused, and the verifier
names a mean-of-rows ROAS quoted as a finding.

    uv run pytest tests/test_marketing_bench.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "marketing_bench.py"


def test_the_marketing_bench_holds_its_floor(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location("marketing_bench", SCRIPT)
    bench = importlib.util.module_from_spec(spec)
    sys.modules["marketing_bench"] = bench
    spec.loader.exec_module(bench)
    code = bench.main(["--json", str(tmp_path / "out.json")])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "strong precision 100.0%" in out and "NOT FOUND" not in out
    assert "a total of ctr_pct refused: True" in out
