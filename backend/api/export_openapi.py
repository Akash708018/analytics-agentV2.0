"""Write docs/api/openapi.yaml from the declared contract. The drift test compares the two."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import yaml

from backend.api.app import create_app

OUT = Path(__file__).resolve().parents[2] / "docs" / "api" / "openapi.yaml"


def render() -> str:
    with tempfile.TemporaryDirectory() as d:
        schema = create_app(state_dir=d).openapi()
    return yaml.safe_dump(schema, sort_keys=False, allow_unicode=True, width=100)


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(), encoding="utf-8")
    print(f"wrote {OUT} ({len(OUT.read_text().splitlines())} lines)", file=sys.stderr)
