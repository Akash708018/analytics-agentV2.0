"""The floor under scripts/marketing_bench_v2.py (25/09/2026): funnels, subscriptions, an
experiment, cross-platform claims, coupon fan-out with mixed currencies and search rankings --
no strong suggestion wrong, every expected caveat found and recounted true, nothing wrong that the
filter marks checked, every end-to-end figure equal to SQL, and the currency, split and
contamination caveats in the analysis replies.

    uv run pytest tests/test_marketing_bench_v2.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "marketing_bench_v2.py"


def test_the_second_marketing_bench_holds_its_floor(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location("marketing_bench_v2", SCRIPT)
    bench = importlib.util.module_from_spec(spec)
    sys.modules["marketing_bench_v2"] = bench
    spec.loader.exec_module(bench)
    code = bench.main(["--json", str(tmp_path / "out.json")])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "WRONG" not in out.replace("WRONG AND MARKED CHECKED", "")
    assert "contamination_caveat_in_reply: True" in out
