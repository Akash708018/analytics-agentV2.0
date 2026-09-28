"""The agent loop (Phase 14 Step 4), with no network.

A scripted provider stands in for the model so the loop is tested exactly: which tools run, with
which workspace, what is refused, when it stops, when it fails over. The two real providers' wire
formats are tested by translating canned responses in the shapes their APIs document.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine import workspace  # noqa: E402
from backend.engine.webapp import agent, llm  # noqa: E402
from backend.engine.webapp.llm import Call, ProviderError, Reply  # noqa: E402
from backend.engine.webapp.real_backend import RealBackend  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ANSWERS = dict(
    grain="one row = one order", primary_key=["order_id"], date_column="order_date",
    measures=["units", "unit_price", "revenue"], dimensions=["region", "product", "channel"],
    aggregations={"units": "sum", "unit_price": "none", "revenue": "sum"},
    measure_definitions={"units": "items", "unit_price": "price of one", "revenue": "u x p"},
    analysis_window_start="2024-01-01", analysis_window_end="2024-12-31")


class Scripted:
    """A provider whose model replies are a list, consumed one per step."""

    name = "scripted"

    def __init__(self, replies, fail: ProviderError | None = None):
        self.replies, self.fail, self.seen = list(replies), fail, []

    def available(self):
        return True

    def start(self, system, history, message, tools):
        self.tools = tools
        outer = self

        outer.finals = []
        outer.gathered = []

        class S:
            def step(self_inner, final=False):
                outer.finals.append(final)
                if outer.fail:
                    raise outer.fail
                return outer.replies.pop(0)

            def add_results(self_inner, results):
                outer.seen.extend(results)

            def add_user(self_inner, text):
                outer.seen.append((None, text))

            def add_gathered(self_inner, note, results):
                outer.gathered.append((note, list(results)))
        return S()


@pytest.fixture()
def contracted():
    be = RealBackend()
    ws = be.new_workspace_id()
    path = be.save_upload(ws, "clean_sales.csv", (FIXTURES / "clean_sales.csv").read_bytes()).path
    assert be.confirm_ingest(ws, be.draft_ingest(ws, path).spec).ok
    assert be.confirm_contract(ws, be.draft_contract(ws, "clean_sales", **ANSWERS)).ok
    yield be, ws
    workspace.reset(ws)
    workspace.workspace_dir(ws).rmdir()


def _answer(be, ws, provider, message="how many orders by region?"):
    return agent.answer(ws, [], message, lock=lambda: be._workspace(ws),
                        list_artifacts=lambda: be.list_artifacts(ws), providers=[provider])


# --- schemas ---------------------------------------------------------------------------------

def _schema_keys(schema, out):
    """Keywords of the schema itself, not property NAMES (render_chart has a parameter 'title')."""
    if isinstance(schema, dict):
        for k, v in schema.items():
            out.add(k)
            if k == "properties":
                for sub in v.values():
                    _schema_keys(sub, out)
            elif isinstance(v, (dict, list)):
                _schema_keys(v, out)
    elif isinstance(schema, list):
        for v in schema:
            _schema_keys(v, out)
    return out


def test_every_allowlisted_tool_converts_to_the_openapi_subset():
    specs = agent.tool_specs()
    assert [s.name for s in specs] == list(agent.ALLOWED)
    for s in specs:
        keys = _schema_keys(s.parameters, set())
        assert not keys & {"anyOf", "default", "title", "additionalProperties", "$schema"}, s.name
        assert "workspace_id" not in s.parameters.get("properties", {}), s.name
    ca = next(s for s in specs if s.name == "compute_analysis").parameters
    assert ca["required"] == ["dataset_name", "analysis_type"]
    assert ca["properties"]["column"] == {"type": "string", "nullable": True}
    assert "title" in next(s for s in specs if s.name == "render_chart").parameters["properties"]


def test_no_path_sql_or_consent_tool_is_offered():
    for name in ("preview_file", "check_file", "propose_ingest_spec", "load_csv", "load_excel",
                 "query_source", "describe_source", "load_postgres_table", "confirm_ingest_spec",
                 "confirm_dataset_contract", "propose_dataset_contract", "apply_cleaning_plan",
                 "reset_workspace"):
        assert name not in agent.ALLOWED


def test_a_parameterless_tool_omits_parameters_for_gemini():
    specs = {s.name: s for s in agent.tool_specs()}
    assert "parameters" not in llm._declaration(specs["list_datasets"])
    assert "parameters" in llm._declaration(specs["compute_analysis"])


# --- the loop ----------------------------------------------------------------------------------

def test_a_tool_runs_in_this_workspace_and_the_answer_comes_back(contracted):
    be, ws = contracted
    p = Scripted([
        Reply(calls=[Call("1", "compute_analysis", {"dataset_name": "clean_sales",
                                                    "analysis_type": "frequency",
                                                    "column": "region"})]),
        Reply(text="North leads.")])
    turn = _answer(be, ws, p)
    assert turn.error is None and turn.reply == "North leads."
    [call] = turn.tool_calls
    assert not call.refused and "500 of 500 row(s) analysed" in call.result
    assert "workspace_id" not in call.arguments


def test_a_model_supplied_workspace_is_overwritten(contracted):
    be, ws = contracted
    p = Scripted([Reply(calls=[Call("1", "list_datasets", {"workspace_id": "local"})]),
                  Reply(text="ok")])
    turn = _answer(be, ws, p)
    # By the workspace's own name: "local" (Claude Desktop's) may hold a clean_sales too, so
    # finding the dataset would not prove which workspace answered.
    result = turn.tool_calls[0].result
    assert f"'{ws}'" in result and "'local'" not in result


def test_a_tool_outside_the_allowlist_is_refused_in_the_engines_shape(contracted):
    be, ws = contracted
    p = Scripted([Reply(calls=[Call("1", "reset_workspace", {"confirm": True})]),
                  Reply(text="I can't do that.")])
    turn = _answer(be, ws, p)
    assert turn.tool_calls[0].refused and "not available here" in turn.tool_calls[0].result
    assert be.list_datasets(ws), "nothing was reset"


def test_a_refused_analysis_is_marked_and_its_text_reaches_the_model(contracted):
    be, ws = contracted
    p = Scripted([Reply(calls=[Call("1", "compute_analysis", {"dataset_name": "nope",
                                                              "analysis_type": "frequency"})]),
                  Reply(text="That dataset is not loaded.")])
    turn = _answer(be, ws, p)
    assert turn.tool_calls[0].refused
    assert "NEXT STEP" in p.seen[0][1]


def test_the_loop_stops_after_the_round_limit(contracted):
    be, ws = contracted
    p = Scripted([Reply(calls=[Call(str(i), "list_datasets", {})])
                  for i in range(agent.MAX_ROUNDS + 2)])
    turn = _answer(be, ws, p)
    # Cleanup Step 11: the last round offers no tools, so at most MAX_ROUNDS - 1 run; a model
    # that still answers with calls and no text gets the stop message.
    assert len(turn.tool_calls) == agent.MAX_ROUNDS - 1
    assert f"stopped after {agent.MAX_ROUNDS} rounds" in turn.reply


def test_a_chart_drawn_this_turn_is_returned_as_an_artifact(contracted):
    be, ws = contracted
    p = Scripted([Reply(calls=[Call("1", "render_chart", {
        "dataset_name": "clean_sales", "analysis_type": "frequency", "chart": "bar",
        "column": "region", "y": "rows"})]), Reply(text="Here it is.")])
    turn = _answer(be, ws, p)
    assert [a.kind for a in turn.artifacts] == ["chart"]
    assert be.read_artifact(ws, turn.artifacts[0].path)[:4] == b"\x89PNG"


def test_a_long_tool_reply_is_trimmed_for_the_model_but_kept_whole_for_the_person():
    long = "x" * (agent.RESULT_CHARS + 500)
    trimmed = agent._trim(long)
    assert len(trimmed) < len(long) and "500 more characters" in trimmed


def test_a_retryable_failure_fails_over_to_the_next_provider(contracted):
    be, ws = contracted
    down = Scripted([], fail=ProviderError("gemini", "HTTP 429", retryable=True))
    up = Scripted([Reply(text="from the second provider")])
    turn = agent.answer(ws, [], "hi", lock=lambda: be._workspace(ws),
                        list_artifacts=lambda: be.list_artifacts(ws), providers=[down, up])
    assert turn.reply == "from the second provider" and turn.error is None


def test_a_fatal_failure_is_reported_not_retried(contracted):
    be, ws = contracted
    bad = Scripted([], fail=ProviderError("gemini", "HTTP 400: bad key", retryable=False))
    never = Scripted([Reply(text="should not run")])
    turn = agent.answer(ws, [], "hi", lock=lambda: be._workspace(ws),
                        list_artifacts=lambda: be.list_artifacts(ws), providers=[bad, never])
    assert turn.error and "bad key" in turn.error and never.replies


def test_no_configured_provider_says_how_to_configure_one():
    turn = agent.answer("ws_000000000abc", [], "hi", lock=contextlib.nullcontext,
                        list_artifacts=list, providers=[])
    assert "GEMINI_API_KEY" in turn.error and ".env" in turn.error


# --- wire formats, from canned responses ---------------------------------------------------------

def test_gemini_calls_are_read_and_the_models_content_echoed_verbatim(monkeypatch):
    g = llm.Gemini()
    monkeypatch.setattr(g, "model", lambda: "m")
    content = {"role": "model", "parts": [
        {"functionCall": {"name": "list_datasets", "args": {}}, "thoughtSignature": "sig=="},
        {"text": "checking"}]}
    replies = iter([{"candidates": [{"content": content}]},
                    {"candidates": [{"content": {"role": "model", "parts": [{"text": "done"}]}}]}])
    sent = []
    monkeypatch.setattr(g, "post", lambda body, model=None: (sent.append(body), next(replies))[1])
    s = g.start("sys", [{"role": "assistant", "content": "earlier"}], "q", [])
    r = s.step()
    assert r.calls[0].name == "list_datasets" and r.text == "checking"
    s.add_results([(r.calls[0], "result text")])
    assert s.step().text == "done"
    contents = sent[-1]["contents"]
    assert contents[0]["role"] == "model"                       # history mapped
    assert contents[2] == content                                # echoed verbatim, signature kept
    assert contents[3]["parts"][0]["functionResponse"]["response"] == {"result": "result text"}


def test_gemini_with_no_candidate_is_a_fatal_error(monkeypatch):
    g = llm.Gemini()
    monkeypatch.setattr(g, "model", lambda: "m")
    monkeypatch.setattr(g, "post",
                        lambda body, model=None: {"promptFeedback": {"blockReason": "SAFETY"}})
    with pytest.raises(ProviderError) as exc:
        g.start("s", [], "q", []).step()
    assert not exc.value.retryable and "SAFETY" in str(exc.value)


def test_groq_tool_calls_round_trip(monkeypatch):
    q = llm.Groq()
    monkeypatch.setattr(q, "model", lambda: "m")
    replies = iter([
        {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "run_analysis", "arguments": '{"dataset_name": "d"}'}}]}}]},
        {"choices": [{"message": {"role": "assistant", "content": "done"}}]}])
    sent = []
    monkeypatch.setattr(q, "post", lambda body: (sent.append(body), next(replies))[1])
    s = q.start("sys", [], "q", [llm.ToolSpec("run_analysis", "d", {"type": "object",
                                                                    "properties": {}})])
    r = s.step()
    assert r.calls == [Call("c1", "run_analysis", {"dataset_name": "d"})]
    s.add_results([(r.calls[0], "text")])
    assert s.step().text == "done"
    # The session appends to one list, so the last request's messages end with the model's
    # final reply; the tool result is the one before it.
    assert sent[-1]["messages"][-2] == {"role": "tool", "tool_call_id": "c1", "content": "text"}
    assert sent[0]["tools"][0]["function"]["name"] == "run_analysis"


def test_http_errors_are_classified_and_never_carry_the_key(monkeypatch):
    import io
    import urllib.error

    def raise_(code):
        def f(req, timeout):
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(b"{}"))
        return f
    monkeypatch.setenv("GEMINI_API_KEY", "SECRET-KEY-123")
    monkeypatch.setattr(llm, "_sleep", lambda s: None)
    for code, retry in ((429, True), (503, True), (400, False), (403, False)):
        monkeypatch.setattr(llm.urllib.request, "urlopen", raise_(code))
        with pytest.raises(ProviderError) as exc:
            llm._request("gemini", "https://example.invalid", {"x-goog-api-key": "SECRET-KEY-123"})
        assert exc.value.retryable is retry and "SECRET-KEY-123" not in str(exc.value)


def test_env_file_loads_names_without_overwriting(tmp_path, monkeypatch):
    """On a private copy of the environment. The first version used delenv(raising=False) on an
    absent variable, which registers nothing to undo, so the key load_env wrote outlived the test
    and a later test called Gemini for real with it (P14-D24)."""
    import os
    env = tmp_path / ".env"
    env.write_text('# comment\nexport GEMINI_API_KEY="abc"\nGROQ_API_KEY=already\n')
    private = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    private["GROQ_API_KEY"] = "kept"
    monkeypatch.setattr(os, "environ", private)
    assert llm.load_env(env) == ["GEMINI_API_KEY"]
    assert private["GEMINI_API_KEY"] == "abc" and private["GROQ_API_KEY"] == "kept"


def test_gemini_model_choice_prefers_the_alias_then_the_highest_number():
    """P14-D25: string sorting chose gemini-omni-1.1-flash, whose free tier refused at once."""
    listed = ["gemini-2.5-flash", "gemini-3.8-flash", "gemini-omni-1.1-flash", "gemini-3.5-flash",
              "gemini-3.1-flash-lite", "gemini-3-flash-preview", "gemini-flash-latest"]
    assert llm.choose_gemini_model(listed) == "gemini-flash-latest"
    no_alias = [n for n in listed if n != "gemini-flash-latest"]
    assert llm.choose_gemini_model(no_alias) == "gemini-3.8-flash"
    assert llm.choose_gemini_model(["gemini-3.8-flash", "gemini-3.10-flash"]) == "gemini-3.10-flash"


def test_every_request_names_its_user_agent(monkeypatch):
    """Groq's edge refuses Python's default agent with 403 / 1010 (P14-D25)."""
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def capture(req, timeout):
        seen.update({k.lower(): v for k, v in req.header_items()})
        return Resp()
    monkeypatch.setattr(llm.urllib.request, "urlopen", capture)
    llm._request("groq", "https://example.invalid", {"Authorization": "Bearer x"})
    assert seen["user-agent"] == llm.USER_AGENT and "urllib" not in seen["user-agent"].lower()


def test_a_fatal_failure_after_a_retryable_one_reports_both(contracted):
    be, ws = contracted
    limited = Scripted([], fail=ProviderError("gemini", "HTTP 429: quota", retryable=True))
    blocked = Scripted([], fail=ProviderError("groq", "HTTP 403: 1010", retryable=False))
    turn = agent.answer(ws, [], "hi", lock=lambda: be._workspace(ws),
                        list_artifacts=lambda: be.list_artifacts(ws), providers=[limited, blocked])
    assert "gemini: HTTP 429" in turn.error and "groq: HTTP 403" in turn.error


def _http_error(code, body=b"{}", headers=None):
    import io
    import urllib.error
    return urllib.error.HTTPError("https://x", code, "x", headers or {}, io.BytesIO(body))


def test_a_busy_provider_is_waited_on_as_it_asks_then_succeeds(monkeypatch):
    """P14-D26: Groq's own "try again in 3.9675s" is honoured, then the call goes through."""
    waits, calls = [], []

    class Ok:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def urlopen(req, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise _http_error(429, b'{"error":{"message":"Please try again in 3.9675s."}}')
        return Ok()
    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(llm, "_sleep", waits.append)
    assert llm._request("groq", "https://x", {}) == {"ok": True}
    assert len(calls) == 2 and abs(waits[0] - (3.9675 + 0.25)) < 1e-6


def test_a_long_wait_fails_over_instead_of_stalling(monkeypatch):
    """A spent daily quota asks for longer than MAX_WAIT_S: no sleep, straight to the next
    provider."""
    waits = []
    body = b'{"error":{"details":[{"retryDelay": "3600s"}]}}'

    def urlopen(req, timeout):
        raise _http_error(429, body)
    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(llm, "_sleep", waits.append)
    with pytest.raises(ProviderError) as exc:
        llm._request("gemini", "https://x", {})
    assert exc.value.retryable and waits == []


def test_a_503_is_retried_with_backoff_then_reported(monkeypatch):
    waits = []
    monkeypatch.setattr(llm.urllib.request, "urlopen",
                        lambda req, timeout: (_ for _ in ()).throw(_http_error(503)))
    monkeypatch.setattr(llm, "_sleep", waits.append)
    with pytest.raises(ProviderError) as exc:
        llm._request("gemini", "https://x", {})
    assert exc.value.retryable and len(waits) == llm.ATTEMPTS - 1


def test_groq_gets_json_schema_nullables_and_gemini_keeps_openapi():
    """P14-D27: Groq refused gpt-oss's `question: null` because `nullable` is not JSON Schema.
    run_analysis carried that argument and left the allowlist in Cleanup Step 10; compute_analysis's
    `dimension` is the same shape, a string that may be null."""
    ca = next(s for s in agent.tool_specs() if s.name == "compute_analysis")
    assert ca.parameters["properties"]["dimension"] == {"type": "string", "nullable": True}
    js = llm.to_json_schema(ca.parameters)
    assert js["properties"]["dimension"] == {"type": ["string", "null"]}
    assert js["properties"]["dataset_name"] == {"type": "string"}
    q = llm.Groq()
    session = q.start("s", [], "m", [ca])
    sent = session._tools[0]["function"]["parameters"]["properties"]["dimension"]
    assert sent == {"type": ["string", "null"]}


def test_a_chart_drawn_before_a_failover_stays_with_the_answer(contracted):
    """P14-D28 kept a chart drawn by an attempt that then failed over out of the answer: that
    attempt's replies were thrown away and every tool ran again. Since step 2 the next provider
    answers from those replies, so the chart is one the answer was written from -- and the chart
    is drawn once, not twice."""
    be, ws = contracted

    class DrawsThenFails(Scripted):
        def start(self, system, history, message, tools):
            session = super().start(system, history, message, tools)
            outer, step = self, session.step

            def failing_step(final=False):
                if not outer.replies:
                    raise ProviderError("gemini", "HTTP 429", retryable=True)
                return step(final)
            session.step = failing_step
            return session
    first = DrawsThenFails([Reply(calls=[Call("1", "render_chart", {
        "dataset_name": "clean_sales", "analysis_type": "frequency", "chart": "bar",
        "column": "region", "y": "rows"})])])
    second = Scripted([Reply(text="answered from the chart's reply")])
    turn = agent.answer(ws, [], "q", lock=lambda: be._workspace(ws),
                        list_artifacts=lambda: be.list_artifacts(ws), providers=[first, second])
    assert turn.reply == "answered from the chart's reply"
    assert [a.kind for a in turn.artifacts] == ["chart"]
    assert [a.kind for a in be.list_artifacts(ws)].count("chart") == 1, "drawn once"
    assert [c.name for c in turn.tool_calls] == ["render_chart"]
    assert second.tools == [] and len(second.gathered[0][1]) == 1


def test_the_rules_forbid_inventing_units():
    assert "no unit or currency" in agent.SYSTEM


DAILY = (b'{"error":{"code":429,"message":"You exceeded your current quota. Quota exceeded for '
         b'metric: generate_content_free_tier_requests, limit: 20, model: gemini-3.8-flash. '
         b'Please retry in 59.2s.","details":[{"violations":[{"quotaId":'
         b'"GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}')


def test_a_spent_daily_quota_is_never_waited_on(monkeypatch):
    """P14-D32: the 59 s "retry" on a daily quota was waited twice before failing over."""
    waits = []
    monkeypatch.setattr(llm.urllib.request, "urlopen",
                        lambda req, timeout: (_ for _ in ()).throw(_http_error(429, DAILY)))
    monkeypatch.setattr(llm, "_sleep", waits.append)
    with pytest.raises(ProviderError) as exc:
        llm._request("gemini", "https://x", {})
    assert exc.value.kind == "daily_quota" and exc.value.model == "gemini-3.8-flash"
    assert waits == [] and "daily quota for gemini-3.8-flash is used up" in exc.value.summary


def test_gemini_climbs_its_ladder_when_a_models_day_is_spent(monkeypatch):
    g = llm.Gemini()
    monkeypatch.setattr(g, "_discover", lambda: ["gemini-flash-latest", "gemini-3.8-flash",
                                                 "gemini-3.7-flash"])
    tried = []

    def request(provider, url, headers, body=None, **_):
        model = url.split("/models/")[1].split(":")[0]
        tried.append(model)
        if model in ("gemini-flash-latest", "gemini-3.8-flash"):
            raise llm._classify("gemini", 429, DAILY.decode())
        return {"candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}}]}
    monkeypatch.setattr(llm, "_request", request)
    with pytest.raises(ProviderError):
        g.start("s", [], "q", []).step()             # the alias is spent, and names 3.8
    assert g.model() == "gemini-3.7-flash"           # 3.8 skipped: the alias's error named it
    assert g.start("s", [], "q", []).step().text == "ok"
    assert tried == ["gemini-flash-latest", "gemini-3.7-flash"]


def test_the_loop_retries_gemini_on_its_next_model_before_groq(contracted):
    be, ws = contracted

    class Laddered(Scripted):
        name = "gemini"

        def __init__(self):
            super().__init__([Reply(text="from the second model")])
            self.left = 1

        def has_another_model(self):
            return True

        def start(self, *a):
            if self.left:
                self.left -= 1
                raise llm._classify("gemini", 429, DAILY.decode())
            return super().start(*a)
    groq = Scripted([Reply(text="should not be reached")])
    turn = agent.answer(ws, [], "q", lock=lambda: be._workspace(ws),
                        list_artifacts=lambda: be.list_artifacts(ws), providers=[Laddered(), groq])
    assert turn.reply == "from the second model" and groq.replies


def test_groq_recovers_when_its_model_calls_a_tool_it_was_not_given(monkeypatch):
    """P14-D33: gpt-oss copied a NEXT STEP into a call to propose_dataset_contract; Groq
    refused it with 400 tool_use_failed and the whole turn failed."""
    q = llm.Groq()
    monkeypatch.setattr(q, "model", lambda: "m")
    refusal = ('{"error":{"message":"Tool call validation failed: attempted to call tool '
               "'propose_dataset_contract' which was not in request.tools\","
               '"code":"tool_use_failed"}}')
    replies = iter([llm._classify("groq", 400, refusal),
                    {"choices": [{"message": {"role": "assistant",
                                              "content": "Confirm it on the Contract screen."}}]}])
    sent = []

    def post(body):
        sent.append([dict(m) for m in body["messages"]])
        r = next(replies)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(q, "post", post)
    reply = q.start("sys", [], "q", []).step()
    assert reply.text == "Confirm it on the Contract screen."
    note = sent[1][-1]
    assert note["role"] == "user" and "propose_dataset_contract is not one of your tools" in (
        note["content"])


def test_a_next_step_naming_a_screen_tool_is_annotated():
    text = 'NEXT STEP: call propose_dataset_contract(dataset_name="t")'
    out = agent._with_screen_notes(text)
    assert "propose_dataset_contract: not one of your tools" in out and "Contract screen" in out
    plain = 'NEXT STEP: call compute_analysis(dataset_name="t", analysis_type="trend")'
    assert agent._with_screen_notes(plain) == plain


def test_a_failed_turn_reads_as_sentences_not_json(contracted):
    be, ws = contracted
    daily = Scripted([], fail=llm._classify("gemini", 429, DAILY.decode()))
    turn = agent.answer(ws, [], "q", lock=lambda: be._workspace(ws),
                        list_artifacts=lambda: be.list_artifacts(ws), providers=[daily])
    assert turn.error.startswith("The assistant could not answer this time.")
    assert "daily quota for gemini-3.8-flash is used up" in turn.error and "{" not in turn.error


def test_the_screen_note_reaches_the_model_through_the_loop(contracted):
    """Wiring, not just the function: the first test of the note passed with it unwired."""
    be, ws = contracted
    p = Scripted([Reply(calls=[Call("1", "compute_analysis", {"dataset_name": "not_loaded",
                                                              "analysis_type": "frequency"})]),
                  Reply(text="Load it first.")])
    _answer(be, ws, p)
    to_model = p.seen[0][1]
    assert "[Note for the assistant]" in to_model and "Upload & read screen" in to_model


# --- the rules the graded answer of 22/09/2026 12:21 broke (Cleanup Step 9) -------------------

@pytest.mark.skip(reason="v2 B0 (D-B0-2): the v1 Streamlit app ui/app.py is not seeded; "
                         "v2 screens belong to the frontend")
def test_the_screens_the_rules_name_are_the_screens_the_app_has():
    """It sent the person to a Cleaning screen that does not exist."""
    import re
    from pathlib import Path
    app = (Path(__file__).resolve().parents[1] / "ui" / "app.py").read_text()
    assert list(agent.SCREENS) == re.findall(r'st\.Page\([^)]*title="([^"]+)"', app)
    assert all(name in agent.SYSTEM for name in agent.SCREENS)


def test_the_rules_forbid_handing_the_person_a_tool_call():
    assert "never give the person a tool call" in agent.SYSTEM.lower()


def test_the_rules_say_to_run_what_the_question_needs_rather_than_suggest_it():
    text = agent.SYSTEM.lower()
    assert "run it yourself" in text and "render_chart" in text and "period=" in text


def test_the_rules_forbid_a_cause_no_result_states():
    assert "cause" in agent.SYSTEM.lower()


def test_cleaning_is_sent_to_a_screen_that_exists():
    """Main's Cleanup Step 9 forbade 'approve cleaning' because cleaning had no screen and the rule
    named one anyway. Phase 14 Step 5 built the Clean screen; at the merge of 25/09/2026 the rule
    stands only because that screen is in the app -- and no rule may say cleaning has none."""
    assert "Clean" in agent.SCREENS
    assert "approve cleaning" in agent.SYSTEM and "the Clean screen" in agent.SYSTEM
    assert "cleaning and databases have no screen" not in agent.SYSTEM.lower()


# --- the request fits the providers (Cleanup Step 10, CL9-O1) ---------------------------------
#
# Groq refused every graded question with 413: 8,000 tokens a minute, and the twelve tool specs
# alone were 19,616 characters (~4,900 tokens) before a word of the conversation.

SPEC_BUDGET = 8_000


def _spec_chars() -> int:
    import json
    return sum(len(s.description) + len(json.dumps(s.parameters)) for s in agent.tool_specs())


def test_the_tool_specs_fit_their_budget():
    assert _spec_chars() <= SPEC_BUDGET, _spec_chars()


def test_the_analysis_roster_is_derived_from_the_registry():
    from backend.engine.analysis.registry import REGISTRY
    desc = next(s.description for s in agent.tool_specs() if s.name == "compute_analysis")
    for name in REGISTRY:
        assert f"{name}(" in desc, name
    assert "top_n(dimension, measure, n, period, grain)" in desc


def test_run_analysis_is_not_offered_to_the_model():
    """compute_analysis passes the same gate; run_analysis's reply was 7,431 characters of a
    catalogue the roster above already carries."""
    assert "run_analysis" not in agent.ALLOWED


def test_a_reply_naming_run_analysis_says_compute_analysis_does_the_same():
    text = agent._with_screen_notes('NEXT STEP: call run_analysis(dataset_name="x", ...)')
    assert "run_analysis: not one of your tools" in text and "compute_analysis" in text


# --- the live run of Cleanup Step 10: a malformed generation, and a false "nothing changed" ---

PARSE_FAILED = ('{"error":{"message":"Parsing failed. The model generated output that could not be '
                'parsed. Please adjust your prompt. See \'failed_generation\' for more details.",'
                '"type":"invalid_request_error","failed_generation":"{\\"name\\": ..."}}')


def test_a_generation_groq_could_not_parse_is_classified():
    assert llm._classify("groq", 400, PARSE_FAILED).kind == "generation_failed"


def test_groq_retries_a_generation_it_could_not_parse(monkeypatch):
    """Seen live: six tool calls made, a chart drawn, then 400 'Parsing failed' ended the turn."""
    q = llm.Groq()
    monkeypatch.setattr(q, "model", lambda: "m")
    replies = iter([llm._classify("groq", 400, PARSE_FAILED),
                    {"choices": [{"message": {"role": "assistant", "content": "November."}}]}])

    def post(body):
        r = next(replies)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(q, "post", post)
    assert q.start("sys", [], "q", []).step().text == "November."


def test_a_failed_turn_does_not_say_nothing_changed_when_a_file_was_written():
    """The same run wrote a chart before failing; the message said nothing had changed."""
    text = agent._failed(["groq: could not parse"], written=1)
    assert "Nothing in your workspace changed" not in text and "Files" in text
    assert "Nothing in your workspace changed" in agent._failed(["x"], written=0)


# --- the last round answers (Cleanup Step 11) ---------------------------------------------------
#
# Live, 15:03: seven rounds fetched every figure the answer needed, the eighth was spent on a
# refusal, and the turn returned "I stopped after 8 rounds" with none of it.

def test_the_last_round_is_asked_for_an_answer_and_its_text_is_kept(contracted):
    be, ws = contracted
    rounds = [Reply(calls=[Call(str(i), "list_datasets", {})]) for i in range(agent.MAX_ROUNDS - 1)]
    rounds.append(Reply(text="November, driven by one order.",
                        calls=[Call("x", "list_datasets", {})]))
    p = Scripted(rounds)
    turn = _answer(be, ws, p)
    assert p.finals == [False] * (agent.MAX_ROUNDS - 1) + [True]
    assert turn.reply == "November, driven by one order."
    assert len(turn.tool_calls) == agent.MAX_ROUNDS - 1, "the final round's calls are not run"


def test_gemini_is_told_to_call_nothing_on_the_final_round(monkeypatch):
    g = llm.Gemini()
    monkeypatch.setattr(g, "model", lambda: "m")
    bodies = []
    monkeypatch.setattr(g, "post", lambda body, model: bodies.append(
        __import__("copy").deepcopy(body)) or {"candidates": [{"content": {
            "role": "model", "parts": [{"text": "done"}]}}]})
    s = g.start("sys", [], "q", list(agent.tool_specs())[:1])
    s.step()
    s.step(final=True)
    assert bodies[0]["toolConfig"]["functionCallingConfig"]["mode"] == "AUTO"
    assert bodies[1]["toolConfig"]["functionCallingConfig"]["mode"] == "NONE"
    assert "last round" in str(bodies[1]["contents"][-1])


def test_groq_is_told_to_call_nothing_on_the_final_round(monkeypatch):
    q = llm.Groq()
    monkeypatch.setattr(q, "model", lambda: "m")
    bodies = []
    monkeypatch.setattr(q, "post", lambda body: bodies.append(
        __import__("copy").deepcopy(body)) or {
        "choices": [{"message": {"role": "assistant", "content": "done"}}]})
    q.start("sys", [], "q", list(agent.tool_specs())[:1]).step(final=True)
    assert bodies[0]["tool_choice"] == "none"
    assert "last round" in bodies[0]["messages"][-1]["content"]


def test_the_rules_say_not_to_re_check_a_ready_dataset():
    assert "profile, describe or validate only when" in agent.SYSTEM


# --- step 2: free-tier models finish multi-step answers (26/09/2026) ---------------------------

DECLARED_CAVEATS = ["120 exact duplicate rows; remove before summing",
                    "8 lines have units = -1 and negative revenue/cost",
                    "customer_state blank in 3,470 rows"]


@pytest.fixture()
def caveated():
    be = RealBackend()
    ws = be.new_workspace_id()
    path = be.save_upload(ws, "clean_sales.csv", (FIXTURES / "clean_sales.csv").read_bytes()).path
    assert be.confirm_ingest(ws, be.draft_ingest(ws, path).spec).ok
    assert be.confirm_contract(ws, be.draft_contract(ws, "clean_sales", caveats=DECLARED_CAVEATS,
                                                     **ANSWERS)).ok
    yield be, ws
    workspace.reset(ws)
    workspace.workspace_dir(ws).rmdir()


def _caveat_lines(text):
    from backend.engine.state import DECLARED, MEASURED
    return [line for line in text.splitlines() if DECLARED in line or MEASURED in line]


def test_a_repeated_caveat_block_is_sent_once_per_turn(caveated):
    """Every analysis prints its table's caveats; the model reads them once a turn. The tools'
    own text -- what Claude Desktop reads, and the person's record -- is unchanged."""
    be, ws = caveated
    freq = {"dataset_name": "clean_sales", "analysis_type": "frequency", "column": "region"}
    top = {"dataset_name": "clean_sales", "analysis_type": "top_n", "dimension": "region",
           "measure": "revenue", "n": 3}
    p = Scripted([Reply(calls=[Call("1", "compute_analysis", freq)]),
                  Reply(calls=[Call("2", "compute_analysis", top)]),
                  Reply(text="Done.")])
    turn = _answer(be, ws, p)
    first, second = (text for _, text in p.seen)
    block = _caveat_lines(first)
    assert len(block) >= len(DECLARED_CAVEATS) + 1       # the measured blank region, the declared
    assert _caveat_lines(second) == []
    assert f"Caveats: the same {len(block)} as in an earlier reply this turn." in second
    assert [len(_caveat_lines(c.result)) for c in turn.tool_calls] == [len(block)] * 2


def test_only_an_identical_block_is_replaced():
    from backend.engine.state import DECLARED
    got = agent.Gathered()
    a = f"head\n  - {DECLARED}one\n  - {DECLARED}two\n| t | 1 |"
    b = f"other\n  - {DECLARED}one\n  - {DECLARED}three\n| t | 2 |"
    assert got.add(Call("1", "x", {}), a) == a
    assert got.add(Call("2", "x", {}), b) == b, "a different block is sent whole"
    again = got.add(Call("3", "x", {}), a.replace("| t | 1 |", "| t | 9 |"))
    assert again == "head\n  - Caveats: the same 2 as in an earlier reply this turn.\n| t | 9 |"
    assert got.seen[-1] == a.replace("| t | 1 |", "| t | 9 |"), "the check reads the block"
    assert got.add(Call("4", "x", {}), "no caveats here") == "no caveats here"


FUNCTION_CALLS = {"role": "model", "parts": [
    {"functionCall": {"name": "compute_analysis", "args": {
        "dataset_name": "clean_sales", "analysis_type": "frequency", "column": "region"}},
     "thoughtSignature": "sig=="},
    {"functionCall": {"name": "list_datasets", "args": {}}}]}
HIGH_DEMAND = ('{"error":{"code":503,"message":"The model is overloaded due to high demand. '
               'Please try again later.","status":"UNAVAILABLE"}}')


def test_a_failover_after_tools_ran_answers_without_running_them_again(caveated, monkeypatch):
    """The failure's shape (25/09/2026): Gemini ran tools, then answered 503 "high demand", and
    the next provider ran every tool again. Now Groq is handed the replies already gathered, is
    offered no tool, and answers in one round."""
    be, ws = caveated
    ran = []
    real_run = agent.run_tool
    monkeypatch.setattr(agent, "run_tool", lambda call, w: (
        ran.append((call.name, json.dumps(call.args, sort_keys=True))), real_run(call, w))[1])
    gemini = llm.Gemini()
    monkeypatch.setattr(gemini, "model", lambda: "gemini-3.8-flash")
    monkeypatch.setattr(gemini, "_ladder", ["gemini-3.8-flash"])
    replies = iter([{"candidates": [{"content": FUNCTION_CALLS}]},
                    llm._classify("gemini", 503, HIGH_DEMAND)])

    def gemini_post(body, model=None):
        r = next(replies)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(gemini, "post", gemini_post)
    groq = llm.Groq()
    monkeypatch.setattr(groq, "model", lambda: "openai/gpt-oss-120b")
    sent = []

    def groq_post(body):
        sent.append(json.loads(json.dumps(body)))
        return {"choices": [{"message": {"role": "assistant", "content": "North leads."}}]}
    monkeypatch.setattr(groq, "post", groq_post)
    turn = agent.answer(ws, [], "how many orders by region?", lock=lambda: be._workspace(ws),
                        list_artifacts=lambda: be.list_artifacts(ws), providers=[gemini, groq])
    assert turn.error is None and turn.reply == "North leads."
    assert len(ran) == 2 and len(set(ran)) == 2, "each tool ran once"
    assert [c.name for c in turn.tool_calls] == ["compute_analysis", "list_datasets"]
    [body] = sent
    assert "tools" not in body and "tool_choice" not in body
    texts = [m["content"] for m in body["messages"]]
    assert texts[0] == agent.SYSTEM and texts[1] == "how many orders by region?"
    assert texts[2] == agent.HANDOVER.format(n=2)
    assert texts[3].startswith("[Tool reply 1 of 2, already run: compute_analysis(")
    assert texts[3].endswith(turn.tool_calls[0].result) and "last round" in texts[-1]
    assert any("high demand" in n and "groq takes over, answering from the 2 tool replies "
               "already gathered" in n for n in turn.notes), turn.notes


def test_a_gemini_session_answering_gathered_replies_is_offered_no_tools(monkeypatch):
    g = llm.Gemini()
    monkeypatch.setattr(g, "model", lambda: "m")
    bodies = []
    monkeypatch.setattr(g, "post", lambda body, model: bodies.append(
        __import__("copy").deepcopy(body)) or {"candidates": [{"content": {
            "role": "model", "parts": [{"text": "answered"}]}}]})
    s = g.start("sys", [], "the question", [])
    s.add_gathered("note", [(Call("c1", "list_datasets", {}), "reply one")])
    assert s.step(final=True).text == "answered"
    [body] = bodies
    assert "tools" not in body and "toolConfig" not in body
    texts = [p["text"] for p in body["contents"][-1]["parts"]]
    assert texts[0] == "the question" and texts[1] == "note"
    assert texts[2] == "[Tool reply 1 of 1, already run: list_datasets()]\nreply one"
    assert "last round" in texts[-1]


def test_a_failed_synthesis_moves_on_and_a_turn_with_none_left_reports_every_failure(
        caveated, monkeypatch):
    be, ws = caveated
    first = Scripted([Reply(calls=[Call("1", "list_datasets", {})])])
    step = first.start

    def start(*a):
        s = step(*a)
        inner = s.step

        def failing(final=False):
            if not first.replies:
                raise ProviderError("gemini", "HTTP 503", retryable=True)
            return inner(final)
        s.step = failing
        return s
    first.start = start
    down = Scripted([], fail=ProviderError("groq", "HTTP 413", retryable=True))
    turn = agent.answer(ws, [], "q", lock=lambda: be._workspace(ws),
                        list_artifacts=lambda: be.list_artifacts(ws), providers=[first, down])
    assert "gemini: HTTP 503" in turn.error and "groq: HTTP 413" in turn.error
    assert [c.name for c in turn.tool_calls] == ["list_datasets"], "the record is kept"


# --- OpenRouter, the third provider (scripted HTTP only: not verified live) ---------------------

def _private_env(monkeypatch, **keys):
    import os
    private = {k: v for k, v in os.environ.items()
               if not k.endswith("_API_KEY") and k not in ("ANALYTICS_LLM", "OPENROUTER_MODEL")}
    private.update(keys)
    monkeypatch.setattr(os, "environ", private)
    monkeypatch.setattr(llm, "load_env", lambda path=None: [])  # never the developer's .env
    monkeypatch.setattr(llm, "_INSTANCES", {})
    return private


def test_openrouter_is_third_in_the_default_order(monkeypatch):
    assert llm.DEFAULT_ORDER == "gemini,groq,openrouter"
    _private_env(monkeypatch, OPENROUTER_API_KEY="k")
    assert [p.name for p in llm.configured()] == ["openrouter"]
    _private_env(monkeypatch, OPENROUTER_API_KEY="k", GROQ_API_KEY="k", GEMINI_API_KEY="k")
    assert [p.name for p in llm.configured()] == ["gemini", "groq", "openrouter"]


def test_openrouter_chooses_a_free_model_that_calls_tools(monkeypatch):
    _private_env(monkeypatch, OPENROUTER_API_KEY="k")
    listed = {"data": [
        {"id": "anthropic/claude-x", "supported_parameters": ["tools"], "context_length": 9},
        {"id": "some/small:free", "supported_parameters": ["tools"], "context_length": 8_192},
        {"id": "some/long:free", "supported_parameters": ["tools"], "context_length": 131_072},
        {"id": "some/notools:free", "supported_parameters": [], "context_length": 10**6}]}
    asked = []
    monkeypatch.setattr(llm, "_request", lambda provider, url, headers, body=None, **_: (
        asked.append(url), listed)[1])
    assert llm.OpenRouter().model() == "some/long:free"
    assert asked == [f"{llm.OPENROUTER_URL}/models"]
    listed["data"].append({"id": "openai/gpt-oss-120b:free", "supported_parameters": ["tools"]})
    assert llm.OpenRouter().model() == "openai/gpt-oss-120b:free"      # the preference first
    listed["data"] = listed["data"][:1]
    with pytest.raises(ProviderError) as exc:
        llm.OpenRouter().model()
    assert not exc.value.retryable and "OPENROUTER_MODEL" in str(exc.value)
    _private_env(monkeypatch, OPENROUTER_API_KEY="k", OPENROUTER_MODEL="pinned/model")
    assert llm.OpenRouter().model() == "pinned/model"


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_openrouter_speaks_the_openai_shape_with_tools(monkeypatch):
    _private_env(monkeypatch, OPENROUTER_API_KEY="SECRET-OR-KEY", OPENROUTER_MODEL="m:free")
    seen = []
    answers = iter([
        {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "list_datasets", "arguments": "{}"}}]}}]},
        {"choices": [{"message": {"role": "assistant", "content": "two datasets"}}]}])

    def urlopen(req, timeout):
        seen.append((req.full_url, dict(req.header_items()), json.loads(req.data)))
        return _Resp(next(answers))
    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    spec = next(s for s in agent.tool_specs() if s.name == "list_datasets")
    s = llm.OpenRouter().start("sys", [], "what is loaded?", [spec])
    reply = s.step()
    assert reply.calls == [Call("c1", "list_datasets", {})]
    s.add_results([(reply.calls[0], "clean_sales")])
    assert s.step().text == "two datasets"
    url, headers, body = seen[-1]
    assert url == "https://openrouter.ai/api/v1/chat/completions"
    assert headers["Authorization"] == "Bearer SECRET-OR-KEY" and body["model"] == "m:free"
    assert body["tools"][0]["function"]["name"] == "list_datasets"
    assert body["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": "clean_sales"}


def test_openrouter_errors_are_classified_and_never_carry_the_key(monkeypatch):
    _private_env(monkeypatch, OPENROUTER_API_KEY="SECRET-OR-KEY", OPENROUTER_MODEL="m:free")
    monkeypatch.setattr(llm, "_sleep", lambda s: None)
    daily = {"error": {"code": 429, "message": "Rate limit exceeded: free-models-per-day. Add 10 "
                                              "credits to unlock 1000 free model requests per day"}}
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda req, timeout: _Resp(daily))
    with pytest.raises(ProviderError) as exc:
        llm.OpenRouter().post({"messages": []})        # an error in a 200 body
    assert exc.value.kind == "daily_quota" and "SECRET-OR-KEY" not in str(exc.value)
    upstream = {"error": {"code": 502, "message": "Provider returned error"}}
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda req, timeout: _Resp(upstream))
    with pytest.raises(ProviderError) as exc:
        llm.OpenRouter().post({"messages": []})
    assert exc.value.retryable and exc.value.kind == ""
    monkeypatch.setattr(llm.urllib.request, "urlopen",
                        lambda req, timeout: (_ for _ in ()).throw(_http_error(401, b'{"error":'
                                                                   b'{"message":"No auth"}}')))
    with pytest.raises(ProviderError) as exc:
        llm.OpenRouter().post({"messages": []})
    assert not exc.value.retryable and "SECRET-OR-KEY" not in str(exc.value)


def test_openrouter_answers_a_one_shot_json_request(monkeypatch):
    _private_env(monkeypatch, OPENROUTER_API_KEY="k", OPENROUTER_MODEL="m:free")
    sent = []

    def urlopen(req, timeout):
        sent.append(json.loads(req.data))
        return _Resp({"choices": [{"message": {"content": '{"a": 1}'}}]})
    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    value, who, failures = llm.complete_json([llm.OpenRouter()], "sys", "prompt")
    assert value == {"a": 1} and who == "openrouter/m:free" and failures == []
    assert sent[0]["response_format"] == {"type": "json_object"}


# --- the person sees what the turn waits on -------------------------------------------------------

def test_a_wait_is_heard_while_it_happens_and_kept_on_the_turn(monkeypatch):
    """25/09/2026: the Ask screen said "Reading the data..." through seven minutes of 60 s
    waits. Now each wait names the provider, the model and the seconds before the sleep."""
    events, slept = [], []
    answers = iter([_http_error(429, b'{"error":{"message":"Please try again in 3.9675s."}}'),
                    _Resp({"choices": [{"message": {"role": "assistant", "content": "done"}}]})])

    def urlopen(req, timeout):
        r = next(answers)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(llm, "_sleep", lambda s: slept.append((s, len(events))))
    groq = llm.Groq()
    monkeypatch.setattr(groq, "model", lambda: "openai/gpt-oss-120b")
    turn = agent.answer("ws_000000000abc", [], "hi", lock=contextlib.nullcontext,
                        list_artifacts=list, providers=[groq], progress=events.append)
    assert turn.reply == "done"
    [wait] = [e for e in events if e.kind == "wait"]
    assert wait.who == "groq (openai/gpt-oss-120b)" and abs(wait.seconds - 4.2175) < 1e-6
    assert "rate limit reached -- waiting 4 s" in wait.text
    assert slept[0][1] == events.index(wait) + 1, "heard before the sleep, not after"
    assert turn.notes == [wait.text], "a step is shown while it happens, not kept"
    assert any(e.kind == "step" for e in events)


def test_a_listener_that_fails_never_fails_the_turn(monkeypatch):
    def broken(event):
        raise RuntimeError("the screen went away")
    turn = agent.answer("ws_000000000abc", [], "hi", lock=contextlib.nullcontext,
                        list_artifacts=list, providers=[Scripted([Reply(text="fine")])],
                        progress=broken)
    assert turn.reply == "fine" and turn.error is None


def test_the_real_backend_passes_progress_through(monkeypatch):
    got = {}
    monkeypatch.setattr(agent, "answer", lambda *a, **kw: got.update(kw) or "turn")
    be = RealBackend()
    ws = be.new_workspace_id()
    try:
        assert be.chat(ws, [], "q", progress=print) == "turn" and got["progress"] is print
        assert be.chat(ws, [], "q") == "turn" and got["progress"] is None
    finally:
        with contextlib.suppress(FileNotFoundError):
            workspace.workspace_dir(ws).rmdir()


GONE = ('{"error":{"code":404,"message":"This model models/gemini-2.5-flash is no longer available '
        'to new users. Please update your code to use models/gemini-3.8-flash.",'
        '"status":"NOT_FOUND"}}')


def test_a_retired_pinned_gemini_model_fails_over_to_groq(monkeypatch):
    """Seen live 26/09/2026: GEMINI_MODEL=gemini-2.5-flash gave 404 and the turn ended there."""
    err = llm._classify("gemini", 404, GONE)
    assert err.kind == "model_gone" and err.retryable
    g = llm.Gemini()
    monkeypatch.setattr(g, "_ladder", ["gemini-2.5-flash"])
    monkeypatch.setattr(llm, "_request", lambda *a, **k: (_ for _ in ()).throw(
        llm._classify("gemini", 404, GONE)))
    groq = Scripted([Reply(text="from groq")])
    turn = agent.answer("ws_000000000abc", [], "hi", lock=contextlib.nullcontext,
                        list_artifacts=list, providers=[g, groq])
    assert turn.reply == "from groq" and turn.error is None
    assert not g.has_another_model()
