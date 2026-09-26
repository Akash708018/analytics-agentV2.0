"""The token budget before every provider call (step 2), with no network.

Measured 25/09/2026: Groq refused the retail question with HTTP 413 -- "Limit 8000, Requested
8917" -- after six tool replies, each carrying the table's whole caveat block. These tests hold the
budget's parts one by one, then reproduce that turn's shape through the real Groq session.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.engine.state import DECLARED, MEASURED  # noqa: E402
from backend.engine.webapp import agent, budget, llm  # noqa: E402
from backend.engine.webapp.llm import Call, ProviderError  # noqa: E402

#: Groq's own 413 of 25/09/2026, in the shape its API returns.
TOO_LARGE = json.dumps({"error": {
    "message": "Request too large for model `openai/gpt-oss-120b` in organization `org_x` service "
               "tier `on_demand` on tokens per minute (TPM): Limit 8000, Requested 8917, please "
               "reduce your message size and try again.",
    "type": "tokens", "code": "rate_limit_exceeded"}})


@pytest.fixture(autouse=True)
def nothing_learned():
    """A 413 teaches a limit for the rest of the process; no test may teach the next one."""
    budget.forget()
    yield
    budget.forget()


# --- the parts -----------------------------------------------------------------------------------

def test_tokens_are_characters_over_four_of_the_json():
    assert budget.CHARS_PER_TOKEN == 4
    assert budget.estimate("x" * 400) == 100
    assert budget.estimate("x" * 401) == 101                 # rounded up, never down
    body = {"messages": [{"role": "user", "content": "₹" * 10}]}
    assert budget.estimate(body) == -(-len(json.dumps(body, ensure_ascii=False)) // 4)


def test_limits_come_from_one_registry_with_a_safety_margin():
    assert budget.limit("groq", "openai/gpt-oss-120b") == 8_000
    assert budget.allowance("groq", "openai/gpt-oss-120b") == 6_000   # 8,000 x 0.75
    assert budget.limit("groq", "some-new-model") == budget.LIMITS["groq"]["*"]
    assert budget.limit("nobody", "m") is None and budget.allowance("nobody", "m") is None
    assert budget.SAFETY == 0.75


def test_a_413_teaches_the_limit_and_how_far_the_estimate_ran_low():
    budget.learn("groq", "m", limit_tokens=7_000, requested=8_917, estimated=7_430)
    assert budget.limit("groq", "m") == 7_000
    assert budget.allowance("groq", "m") == int(7_000 * 0.75 / (8_917 / 7_430))
    budget.learn("groq", "n", limit_tokens=None, requested=90_000, estimated=100)
    assert budget.allowance("groq", "n") == int(budget.LIMITS["groq"]["*"] * 0.75 / 2.0)  # bounded


def _reply(n: int, block: str = "") -> str:
    head = f"reply {n}: result file results/r{n}.csv\nWhat this shows:\n"
    table = "\n".join(f"| g{i} | {n * 1000 + i:,}.{n:02d} |" for i in range(20))
    return head + block + "filler line\n" * 150 + table


def test_a_shortened_reply_keeps_head_tail_note_and_its_caveat_lines():
    block = "".join(f"  - {MEASURED}rule {i} counted 1{i} rows\n" for i in range(5))
    block += f"  - {DECLARED}120 exact duplicate rows\n"
    text = "top line\n" + "a\n" * 400 + block + "b\n" * 400 + "| last | 42.5 |"
    cut = budget.shorten(text, 400)
    assert cut.startswith("top line") and cut.endswith("| last | 42.5 |")
    assert "read_result_file" in cut and "left out here" in cut
    for line in block.splitlines():
        assert line in cut, line
    assert len(cut) < len(text) / 2
    assert budget.shorten("short", 400) == "short"


def test_fit_shortens_the_oldest_replies_first_and_never_the_latest():
    messages = [{"content": _reply(i)} for i in range(4)]
    slots = [budget.Slot(m, "content", "reply") for m in messages]
    body = {"system": "S" * 800, "question": "Q?", "messages": messages}
    whole = [m["content"] for m in messages]
    allowed = budget.estimate(body) - 300
    assert budget.fit(slots, lambda: budget.estimate(body), allowed) >= 1
    assert budget.estimate(body) <= allowed
    assert messages[0]["content"] != whole[0], "the oldest is cut first"
    assert messages[-1]["content"] == whole[-1], "the latest reply is never cut"
    assert body["system"] == "S" * 800 and body["question"] == "Q?"
    # A second fit cuts from the whole text, not from a text that already carries a note.
    assert slots[0].original == whole[0]


def test_fit_touches_nothing_that_already_fits():
    messages = [{"content": _reply(1)}, {"content": _reply(2)}]
    slots = [budget.Slot(m, "content", "reply") for m in messages]
    before = [m["content"] for m in messages]
    assert budget.fit(slots, lambda: budget.estimate(messages), 10**6) == 0
    assert [m["content"] for m in messages] == before


def test_earlier_conversation_is_cut_only_after_the_replies():
    history = {"content": "an earlier answer " * 200}
    reply1, reply2 = {"content": _reply(1)}, {"content": _reply(2)}
    slots = [budget.Slot(history, "content", "history"),
             budget.Slot(reply1, "content", "reply"), budget.Slot(reply2, "content", "reply")]
    body = [history, reply1, reply2]
    original = history["content"]
    budget.fit(slots, lambda: budget.estimate(body), budget.estimate(body) - 150)
    assert history["content"] == original and reply1["content"] != _reply(1)
    budget.fit(slots, lambda: budget.estimate(body), budget.estimate(body) - 2_000)
    assert history["content"] != original, "history next, once the replies are at their floor"


# --- 413: shrink and retry once, then fail over --------------------------------------------------

def test_http_413_is_classified_with_the_limit_and_the_count():
    err = llm._classify("groq", 413, TOO_LARGE)
    assert err.kind == "too_large" and err.retryable
    assert (err.limit, err.requested) == (8_000, 8_917)
    assert "8,917 tokens against a limit of 8,000" in err.summary


def _session_with_replies(posts):
    q = llm.Groq()
    q.model = lambda: "openai/gpt-oss-120b"
    q.post = posts
    s = q.start("sys", [], "the question", [])
    s.add_results([(Call(f"c{i}", "t", {}), _reply(i) * 3) for i in range(5)])
    return s


def test_a_413_is_shrunk_and_sent_once_more():
    sizes = []

    def post(body):
        sizes.append(budget.estimate(body))
        if len(sizes) == 1:
            raise llm._classify("groq", 413, TOO_LARGE.replace("8000", "3000"))
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
    assert _session_with_replies(post).step().text == "ok"
    assert len(sizes) == 2 and sizes[1] < sizes[0]
    assert budget.limit("groq", "openai/gpt-oss-120b") == 3_000, "learnt from the 413"


def test_a_second_413_goes_to_the_loop_which_fails_over():
    calls = []

    def post(body):
        calls.append(1)
        raise llm._classify("groq", 413, TOO_LARGE.replace("8000", "3000"))
    with pytest.raises(ProviderError) as exc:
        _session_with_replies(post).step()
    assert exc.value.kind == "too_large" and exc.value.retryable and len(calls) == 2


# --- the failure's shape: Groq's 8,000, six large replies each carrying the caveat block ---------

CAVEAT_BLOCK = "".join(
    [f"  - {MEASURED}column_{i} is blank in {1000 + i * 37:,} row(s) ({i + 1}.{i}%), exactly "
     f"where channel = 'Store' -- the caveat's own words run long, as the engine's do.\n"
     for i in range(16)]
    + [f"  - {MEASURED}3 more caveat(s) are not listed here: the list stops at 16.\n"]
    + [f"  - {DECLARED}declared note {i}: 3,470 rows of something the person typed in\n"
       for i in range(13)])


def _analysis_reply(n: int) -> str:
    rows = "\n".join(f"| {r} | {n * 1_000_003 + i * 7_919:,}.{i:02d} | {40 + i} |"
                     for i, r in enumerate(["North", "South", "East", "West"]))
    return (f"Under contract v1 for retail: order_id + line_no.\n\n4 rows x 3 columns, written to\n"
            f"  results/top_n_{n}.csv\n\nWhat this shows:\n  - 30,123 of 30,123 row(s) analysed.\n"
            + CAVEAT_BLOCK
            + "  - Share is of the sum across all 4 group(s).\n" + "  - a method note\n" * 60
            + f"\n| value | total | rows |\n|---|---|---|\n{rows}")


def test_the_retail_turn_fits_groq_free_tier_and_answers(monkeypatch):
    """Six tool replies of ~5,700 characters, each with the same 30-line caveat block, through
    the real Groq session and the real tool specs. A scripted Groq refuses with 413 whatever
    estimates over its 8,000; every request stays at or under 6,000 and the turn answers."""
    assert 3_000 <= len(CAVEAT_BLOCK) <= 5_000
    replies = {f"c{i}": _analysis_reply(i) for i in range(1, 7)}
    monkeypatch.setattr(agent, "run_tool", lambda call, ws: replies[call.id])
    script = [{"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"c{i}", "type": "function", "function": {
            "name": "compute_analysis",
            "arguments": json.dumps({"dataset_name": "retail", "analysis_type": "top_n"})}}]}}]}
        for i in range(1, 7)]
    first = replies["c1"].splitlines()[-4].split("|")[2].strip()    # North, from the oldest reply
    script.append({"choices": [{"message": {"role": "assistant", "content": (
        f"North's total is {first}. The contract notes rows of something the person typed.")}}]})
    sent = []

    def post(body):
        size = budget.estimate(body)
        sent.append((size, json.loads(json.dumps(body))))
        if size > 8_000:
            raise llm._classify("groq", 413, TOO_LARGE)
        return script.pop(0)
    groq = llm.Groq()
    groq.model = lambda: "openai/gpt-oss-120b"
    groq.post = post
    turn = agent.answer("ws_000000000abc", [], "Revenue by region for 2025, and the issues?",
                        lock=contextlib.nullcontext, list_artifacts=list, providers=[groq])
    assert turn.error is None and turn.reply.startswith("North's total is")
    assert len(sent) == 7 and max(size for size, _ in sent) <= 6_000, [s for s, _ in sent]
    assert len(turn.tool_calls) == 6 and all(c.result == replies[f"c{i}"]
                                             for i, c in enumerate(turn.tool_calls, 1))
    # Unbudgeted, the six whole replies alone are ~35,000 characters (~8,900 tokens).
    assert budget.estimate("".join(replies.values())) > 8_000
    last = sent[-1][1]["messages"]
    assert last[0]["content"] == agent.SYSTEM and last[1]["content"].startswith("Revenue by")
    tool_texts = [m["content"] for m in last if m["role"] == "tool"]
    assert tool_texts[-1].endswith(replies["c6"].splitlines()[-1]), "latest reply kept whole"
    assert "Caveats: the same 30 as in an earlier reply this turn." in tool_texts[-1]
    assert sum(t.count(DECLARED) for t in tool_texts) == 13, "the block, once"
    # The figure from the oldest reply, which the model read shortened, is checked against the
    # whole reply: found.
    assert turn.verified and turn.verification.startswith("1 of 1 figure(s) match")
    assert any("shortened" in note for note in turn.notes)
