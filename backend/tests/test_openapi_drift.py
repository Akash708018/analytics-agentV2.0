"""The committed contract equals the one the code declares. On failure: regenerate with
`uv run python -m backend.api.export_openapi`, bump info.version, add a CHANGELOG entry."""
from __future__ import annotations

from pathlib import Path

import yaml

from backend.api import schemas
from backend.api.export_openapi import OUT, render

CHANGELOG = Path(OUT).parent / "CHANGELOG.md"


def test_committed_openapi_equals_generated():
    assert OUT.read_text(encoding="utf-8") == render()


def test_version_is_bumped_in_spec_and_changelog():
    spec = yaml.safe_load(OUT.read_text(encoding="utf-8"))
    assert spec["info"]["version"] == schemas.API_VERSION
    assert f"## {schemas.API_VERSION}" in CHANGELOG.read_text(encoding="utf-8")


def test_every_request_and_response_model_has_an_example():
    spec = yaml.safe_load(OUT.read_text(encoding="utf-8"))
    comps, refs = spec["components"]["schemas"], set()
    for ops in spec["paths"].values():
        for op in ops.values():
            bodies = [op.get("requestBody", {})] + list(op["responses"].values())
            for b in bodies:
                s = b.get("content", {}).get("application/json", {}).get("schema", {})
                for x in [s, *s.get("anyOf", [])]:
                    if "$ref" in x:
                        refs.add(x["$ref"].rsplit("/", 1)[-1])
    missing = sorted(n for n in refs if "examples" not in comps[n] and "Validation" not in n)
    assert refs and not missing, missing
