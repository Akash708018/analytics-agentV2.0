"""The real backend app with a scripted model, for the e2e suite only (D-F11-1).

`backend.llm.provider.ScriptedLLM` is the seam the backend's own tests use: every route, the
engine, the contract gate and the figure checker are real; only the model's words are fixed.
The planner routes ROAS and waste questions to their playbooks; the explainer words the
figures it was given. Served by `servers.js`:

    uv run uvicorn --factory scripted_backend:make   (PYTHONPATH: the repo root and this folder)
"""

import json

from backend.api.app import create_app
from backend.llm.provider import ScriptedLLM


def reply(system: str, user: str) -> str:
    if system.startswith("You route"):
        q = user.lower()
        if "roas" in q:
            return json.dumps({"playbook": "why_roas_dropped",
                               "slots": {"period": "2026-09", "baseline": "2026-08"}})
        if "wast" in q:
            return json.dumps({"playbook": "where_is_spend_wasted", "slots": {}})
        return json.dumps({"playbook": None, "slots": {}})
    figures = [line[2:] for line in system.splitlines() if line.startswith("- ")][:3]
    return "Here is what the data shows: " + "; ".join(figures) + "."


def make():
    return create_app(llm=ScriptedLLM(reply))
