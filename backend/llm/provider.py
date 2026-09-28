"""The LLM seam. The agent sees one method: complete(system, user) -> text.

Providers: `ScriptedLLM` (tests, benches: replies in order, or computed from the prompt) and
`EngineLLM` (v1's providers with failover and waits, engine/webapp/llm.py). Tokens are
estimated at ~4 characters each, and labelled as estimates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol


def tokens(text: str) -> int:
    return max(1, round(len(text) / 4))


class LLM(Protocol):
    name: str

    def complete(self, system: str, user: str) -> str: ...


@dataclass
class Usage:
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    per_call: list[dict] = field(default_factory=list)

    def add(self, purpose: str, system: str, user: str, reply: str) -> None:
        i, o = tokens(system) + tokens(user), tokens(reply)
        self.calls += 1
        self.tokens_in += i
        self.tokens_out += o
        self.per_call.append({"purpose": purpose, "tokens_in": i, "tokens_out": o})


@dataclass
class ScriptedLLM:
    """Replies from a list (in order) or a function of (system, user). Records every prompt."""
    replies: list[str] | Callable[[str, str], str]
    name: str = "scripted"
    seen: list[tuple[str, str]] = field(default_factory=list)

    def complete(self, system: str, user: str) -> str:
        self.seen.append((system, user))
        if callable(self.replies):
            return self.replies(system, user)
        if not self.replies:
            raise RuntimeError("scripted LLM ran out of replies")
        return self.replies.pop(0)


class EngineLLM:
    """v1's provider chain (engine/webapp/llm.py: Gemini / Groq / OpenRouter, in ANALYTICS_LLM
    order): text in, text out, failing over to the next provider on a ProviderError. A failover
    never re-runs a tool -- the agent calls tools, not the provider."""
    name = "engine"

    def __init__(self, on_failover: Callable[[str, str], None] | None = None,
                 max_wait: float | None = None):
        from backend.engine.webapp import llm as v1
        self._v1 = v1
        self._on_failover = on_failover
        self._max_wait = max_wait if max_wait is not None else v1.QUICK_WAIT_S

    def available(self) -> bool:
        return bool(self._v1.configured())

    def complete(self, system: str, user: str) -> str:
        failures = []
        for provider in self._v1.configured():
            try:
                text, model = provider.complete(system, user, max_wait=self._max_wait)
                self.name = f"{provider.name}/{model}"
                return text
            except self._v1.ProviderError as exc:
                failures.append(exc.summary)
                if self._on_failover:
                    self._on_failover(provider.name, exc.summary)
        raise RuntimeError("no model answered: " + ("; ".join(failures) or "none configured"))
