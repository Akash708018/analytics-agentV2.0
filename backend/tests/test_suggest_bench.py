"""The floor under scripts/suggest_bench.py: on its labelled datasets no strong suggestion is
wrong, every expected caveat is found, every caveat line recounts true, the clean table raises
nothing, shuffling changes nothing and renaming makes nothing wrong (25/09/2026).

    uv run pytest tests/test_suggest_bench.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "suggest_bench.py"


def test_the_bench_holds_its_floor(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location("suggest_bench", SCRIPT)
    bench = importlib.util.module_from_spec(spec)
    sys.modules["suggest_bench"] = bench  # its dataclasses look their module up by name
    spec.loader.exec_module(bench)
    code = bench.main(["--no-retail", "--json", str(tmp_path / "out.json")])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "strong precision 100.0%" in out
