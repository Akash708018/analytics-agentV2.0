"""Drive the F1 client against a running backend (not Prism). Synthetic data only.

Run from the repository root with ANALYTICS_API_URL pointing at a backend started
with an empty state directory, for example:

    AA_NO_LLM=1 AA_STATE_DIR=/tmp/aa-state uv run uvicorn --factory backend.api.app:create_app --port 8765
    ANALYTICS_API_URL=http://127.0.0.1:8765 frontend/.venv/bin/python -m frontend.dev.live_smoke

It creates and deletes one session and uploads one synthetic CSV into a new
workspace. It prints status lines only: no request bodies beyond the synthetic
values written here.
"""

from __future__ import annotations

import time

from frontend.api_client import APIClient, APIError, VersionConflict

CSV = (
    b"date,campaign,channel,cost,clicks,impressions,conversions,revenue\n"
    b"2026-08-01,synthetic_a,search,100,50,1000,5,400\n"
    b"2026-08-02,synthetic_b,social,80,40,2000,2,150\n"
    b"2026-09-01,synthetic_a,search,120,55,1100,6,420\n"
    b"2026-09-02,synthetic_b,social,90,30,2100,1,90\n"
)


def expect_error(label: str, call) -> APIError:
    try:
        call()
    except APIError as error:
        current = getattr(error, "current", None)
        extra = f" current.version={current['version']}" if current else ""
        print(f"{label}: {type(error).__name__} {error.status_code} {error.code}{extra}")
        return error
    raise AssertionError(f"{label}: expected an APIError")


def main() -> None:
    with APIClient() as api:
        print("health:", api.health())
        print("version:", api.version())

        created = api.create_session()
        sid = created["sid"]
        session = api.get_session(sid)
        print("create_session: version", created["version"], "workspace", session["workspace_id"])

        saved = api.put_ui_state(sid, {"screen": "start"}, version=session["version"])
        print("put_ui_state: version", session["version"], "->", saved["version"])
        conflict = expect_error(
            "stale put_ui_state",
            lambda: api.put_ui_state(sid, {"screen": "ask"}, version=session["version"]),
        )
        assert isinstance(conflict, VersionConflict)
        assert conflict.current["ui_state"] == {"screen": "start"}
        expect_error(
            "ui_state with an email",
            lambda: api.put_ui_state(sid, {"note": "someone@example.com"}, version=saved["version"]),
        )

        dataset = api.upload_dataset(session["workspace_id"], "synthetic_ads.csv", CSV, "text/csv")
        dataset_id = dataset["dataset_id"]
        print("upload_dataset:", dataset_id, "rows", dataset.get("rows"))
        print("get_dataset: name", api.get_dataset(dataset_id).get("name"))
        detection = api.detect_domains(dataset_id)
        print("detect_domains:", [(c["domain"], c["evidence"].get("score")) for c in detection["domains"]],
              "confirmed", detection["confirmed"])
        tools = api.list_tools(dataset_id)["tools"]
        print("list_tools:", len(tools), "tools;", sorted({t["status"] for t in tools}))
        expect_error(
            "run_tool before domain",
            lambda: api.run_tool("marketing.channel_efficiency", dataset_id),
        )
        print("list_packs:", [(p["pack_id"], p["tools"]) for p in api.list_packs()["packs"]])

        turn_id = api.create_turn(sid, dataset_id, "Which channel had the best ROAS?")["turn_id"]
        deadline = time.monotonic() + 10
        turn = api.get_turn(turn_id)
        while turn["status"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.2)
            turn = api.get_turn(turn_id)
        print("turn:", turn["status"], [(e["type"], e["data"].get("code")) for e in turn["events"]])
        print("list_turns:", [t["turn_id"] == turn_id for t in api.list_turns(sid)["turns"]])
        expect_error("get_turn unknown", lambda: api.get_turn("t_does_not_exist"))

        print("delete_session:", api.delete_session(sid))
        expect_error("get_session after delete", lambda: api.get_session(sid))


if __name__ == "__main__":
    main()
