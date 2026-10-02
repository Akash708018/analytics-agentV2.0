"""B10: the frontend's api-request issues (#23, #17, #16, #21; #20 is in test_keywords)."""
from __future__ import annotations

import pytest

from backend.packs.loader import PackError, load_all, merge


# --- #23: optional params are declared ----------------------------------------------------------

def test_optional_params_are_declared_in_the_tool_spec(tmp_path):
    from fastapi.testclient import TestClient
    from backend.api.app import create_app
    c = TestClient(create_app(state_dir=tmp_path))
    tools = {t["id"]: t for t in c.get("/packs/logistics").json()["pack"]["tools"]}
    focus = tools["logistics.sla_drivers"]["params_optional"]
    assert [p["name"] for p in focus] == ["focus"] and focus[0]["default"] is None
    days = tools["logistics.stuck_shipments"]["params_optional"][0]
    assert days["name"] == "days" and days["default"] == "3"


def test_a_step_reading_an_undeclared_param_fails_pack_load():
    from backend.packs.models import Pack, Step, Tool
    packs = dict(load_all())
    lg = packs["logistics"]
    t = lg.tools[0]
    bad = Tool(**{**t.model_dump(), "params_optional": [],
                  "steps": [Step(analysis="group_compare",
                                 params={"dimension": "@param?:secret", "measure": "x"})]})
    packs["logistics"] = Pack(**{**lg.model_dump(), "tools": [bad.model_dump()]})
    with pytest.raises(PackError, match="optional param 'secret'"):
        merge(packs, ["logistics"])


def test_every_real_pack_declares_what_its_steps_read():
    from backend.packs.loader import step_params
    for pid in ("marketing", "logistics"):
        m = merge(load_all(), [pid])
        for t in m.tools.values():
            declared = set(t.params_required) | {p.name for p in t.params_optional}
            assert step_params(t) <= declared, t.id
