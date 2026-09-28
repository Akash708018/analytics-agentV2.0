"""What one request to a model may carry, and how a conversation is made to fit it (step 2).

Measured 25/09/2026: Groq refused the retail question with HTTP 413 -- "tokens per minute (TPM):
Limit 8000, Requested 8917" -- once six tool replies were in the conversation, and the turn failed
after seven minutes. Every request is now measured before it is sent; one that would not fit has
its oldest tool replies shortened first, then the earlier messages of the conversation. The system
prompt, the question and the latest reply are never slots here, so never shortened.

Tokens are estimated, not counted: characters / CHARS_PER_TOKEN of the request's JSON (no tokenizer
is a dependency, and each provider's differs). The estimate runs low on text dense with numbers,
so a request is kept to SAFETY of its limit; a 413 that names the real count corrects the estimate
for that model for the rest of the process (`learn`).
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass

from backend.engine.state import DECLARED, MEASURED

CHARS_PER_TOKEN = 4
SAFETY = 0.75

#: Tokens one request may carry, by provider and model; "*" is the provider's other models. A
#: provider absent here is not budgeted. Only gpt-oss-120b's 8,000 is measured (the 413 of
#: 25/09/2026 named it); the other Groq figures are its free-tier tokens a minute as recalled,
#: not measured here (no key in the step-2 environment), and Gemini's and OpenRouter's are
#: assumptions set well above any turn this loop builds. A 413 that names a limit replaces a
#: wrong one for the rest of the process.
LIMITS: dict[str, dict[str, int]] = {
    "groq": {"openai/gpt-oss-120b": 8_000, "openai/gpt-oss-20b": 8_000,
             "llama-3.3-70b-versatile": 12_000, "llama-3.1-8b-instant": 6_000, "*": 6_000},
    "gemini": {"*": 250_000},
    "openrouter": {"*": 32_000},
}
#: Characters a shortened tool reply keeps (head and tail), and an earlier message. REPLY_MIN is a
#: last pass, after the earlier messages: measured on the retail-like turn (step 2, command 5), a
#: request with five replies stayed 56 tokens over its allowance with every older reply at
#: REPLY_KEEP or under it. 600 still holds a top_n reply's first lines (the result file's path)
#: and its table's last rows; the caveat lines are kept apart from either (PINNED).
REPLY_KEEP = 1_200
HISTORY_KEEP = 400
REPLY_MIN = 600
#: The line webapp/agent.py puts where a caveat block repeats within a turn.
SAME_CAVEATS = "Caveats: the same "
#: Lines a shortened text keeps wherever they sit: the caveats every result carries, and the line
#: standing for them. Cut from the first reply that carries them, every later "the same N" line
#: would point at nothing -- and they are what "list the data-quality issues" is answered from.
PINNED = (MEASURED, DECLARED, SAME_CAVEATS)

_learned: dict[tuple[str, str], int] = {}   # a limit a provider named in a 413
_ratio: dict[tuple[str, str], float] = {}   # its count / this module's estimate, from that 413


def estimate(body) -> int:
    """Tokens, estimated: characters / CHARS_PER_TOKEN of the JSON, non-ASCII kept as written."""
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def limit(provider: str, model: str) -> int | None:
    if (provider, model) in _learned:
        return _learned[(provider, model)]
    table = LIMITS.get(provider)
    if not table:
        return None
    return table.get(model, table.get("*"))


def allowance(provider: str, model: str) -> int | None:
    """The estimate a request may reach: SAFETY of the limit, lowered by what a 413 taught about
    how far the estimate runs below the provider's count."""
    lim = limit(provider, model)
    if lim is None:
        return None
    return int(lim * SAFETY / _ratio.get((provider, model), 1.0))


def learn(provider: str, model: str, *, limit_tokens: int | None, requested: int | None,
          estimated: int) -> None:
    """A 413 named the limit and what the request counted: remember both for this model. The
    ratio is bounded to [1, 2] -- a count that includes the reply's allowance is not a reason to
    halve every later request."""
    if limit_tokens:
        _learned[(provider, model)] = limit_tokens
    if requested and estimated:
        _ratio[(provider, model)] = min(max(requested / estimated, 1.0), 2.0)


def forget() -> None:
    """For tests: nothing learned."""
    _learned.clear()
    _ratio.clear()


@dataclass
class Slot:
    """Where one shortenable text sits in a provider's request: `container[key]`.

    kind "reply" is a tool reply (or one gathered from an earlier provider); "history" an earlier
    message of the conversation. `original` holds the text before the first shortening, so a
    second one cuts from the whole text, not from a text that already carries a note.
    """

    container: dict
    key: str
    kind: str
    original: str | None = None


def shorten(text: str, keep: int) -> str:
    """About `keep` characters of `text`: its head and its tail, cut at line ends, with a note
    between them, and the PINNED lines of the part left out. Both ends, because an analysis reply
    opens with the result file's path and the method note and closes with its table -- measured
    on the retail turn, the figures sit in its last few hundred characters."""
    if len(text) <= keep:
        return text
    head = text[:keep // 2]
    if "\n" in head[len(head) // 2:]:
        head = head[:head.rindex("\n")]
    tail = text[len(text) - (keep - len(head)):]
    if "\n" in tail[:len(tail) // 2]:
        tail = tail[tail.index("\n") + 1:]
    middle = text[len(head):len(text) - len(tail)]
    pinned = [line for line in middle.splitlines() if any(mark in line for mark in PINNED)]
    left = len(middle) - sum(len(line) + 1 for line in pinned)
    kept = "".join(f"\n{line}" for line in pinned)
    return (f"{head}\n[... {left:,} characters of this text left out here to fit the model's "
            f"request limit. A written result is paged with read_result_file.]{kept}\n{tail}")


def fit(slots: list[Slot], size: Callable[[], int], allowed: int) -> int:
    """Shorten texts until `size()` (an estimate of the whole request) is at most `allowed`: the
    tool replies oldest first to REPLY_KEEP, never the latest; then earlier messages, oldest
    first; then the replies again to REPLY_MIN. Returns how many texts were shortened -- 0 when
    it already fits or nothing more can be cut. A request that still does not fit is sent as it
    is: the allowance is SAFETY of the limit, and a 413 is answered once more."""
    over = size() - allowed
    if over <= 0:
        return 0
    replies = [s for s in slots if s.kind == "reply"][:-1]
    order = ([(s, REPLY_KEEP) for s in replies]
             + [(s, HISTORY_KEEP) for s in slots if s.kind == "history"]
             + [(s, REPLY_MIN) for s in replies])
    done: set[int] = set()
    for slot, floor in order:
        # Cut again while it helps: the estimate is of the JSON, whose escapes make a cut of n
        # characters of text save a little more or less than n.
        while over > 0:
            current = slot.container.get(slot.key)
            if not isinstance(current, str):
                break
            source = slot.original if slot.original is not None else current
            target = max(floor, len(current) - over * CHARS_PER_TOKEN - 200)
            cut = shorten(source, target)
            if len(cut) >= len(current):
                break
            slot.original = source
            slot.container[slot.key] = cut
            done.add(id(slot))
            over = size() - allowed
        if over <= 0:
            break
    return len(done)


__all__ = ["CHARS_PER_TOKEN", "LIMITS", "PINNED", "SAFETY", "SAME_CAVEATS", "Slot", "allowance",
           "estimate", "fit", "forget", "learn", "limit", "shorten"]
