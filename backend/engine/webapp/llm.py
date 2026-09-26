"""Model providers for the Track B agent loop: Gemini, then Groq, then OpenRouter as failover
(guide Phase 14 item 3; OpenRouter added in step 2, 26/09/2026).

No SDKs. Each provider is one JSON POST over urllib, translated to and from one small internal
shape: a `Reply` holding text and/or tool calls. A `Session` keeps the provider's own message list,
so each wire format is spoken exactly -- Gemini's model content is echoed back verbatim, whatever
parts it holds, rather than rebuilt from what this module understood of it. Groq and OpenRouter
speak the same OpenAI chat-completions shape and share `OpenAISession`.

Every session keeps its request within the model's token budget (webapp/budget.py) before each
call, and a wait on a busy provider is reported to whoever listens (`listening`) -- the Ask screen
shows it rather than only "Reading the data...".

Keys come from the environment or a gitignored .env at the repository root (`load_env`), are sent
in headers, and never appear in an error message or a log line.
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from backend.engine.config import PROJECT_ROOT

from . import budget
from .contract import ChatEvent

TIMEOUT_S = 60
#: Sent on every request. Python's default "Python-urllib/3.x" is refused by Groq's edge with
#: HTTP 403 "error code: 1010" (Cloudflare's banned-signature code); measured: default 403, this 200.
USER_AGENT = "analytics-agent/0.1 (+https://github.com/Akash708018/analytics-agent)"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"
GROQ_URL = "https://api.groq.com/openai/v1"
OPENROUTER_URL = "https://openrouter.ai/api/v1"

#: Schema keys a function declaration keeps. Gemini accepts an OpenAPI subset and rejects the rest
#: (measured on the engine's own tools: anyOf x105, default x122, additionalProperties x36).
_KEEP = {"type", "description", "properties", "required", "items", "enum", "format", "nullable"}


class ProviderError(Exception):
    """A provider call failed. `retryable` -- rate limit, server error, timeout -- lets the loop
    fail over; anything else (a bad key, a rejected request) is reported, not retried.

    `kind` names the cases the loop treats specially: "daily_quota" (never waited on; Gemini
    moves to its next model), "tool_use_failed" (the provider refused the model's own tool call;
    the session tells the model and retries), "too_large" (HTTP 413: the session shrinks the
    request and sends it once more; `limit` and `requested` are the tokens the provider named).
    `summary` is the one sentence a person reads.
    """

    def __init__(self, provider: str, message: str, retryable: bool, *, kind: str = "",
                 summary: str = "", model: str | None = None, limit: int | None = None,
                 requested: int | None = None) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.retryable = retryable
        self.kind = kind
        self.model = model
        self.limit = limit
        self.requested = requested
        self.summary = f"{provider}: {summary or message}"


#: What a person waiting on an answer should see (step 2). Defined with the UI's contract, which
#: is standard library only, so the screen can read it without importing this module.
Event = ChatEvent

_listener: contextvars.ContextVar[Callable[[Event], None] | None] = contextvars.ContextVar(
    "analytics_llm_listener", default=None)


def notify(event: Event) -> None:
    """Tell the listener, if any. A listener that fails never fails the request it reports on."""
    listener = _listener.get()
    if listener is None:
        return
    try:
        listener(event)
    except Exception:  # noqa: BLE001 -- a screen that cannot show a line is not a provider error
        pass


@contextmanager
def listening(listener: Callable[[Event], None]) -> Iterator[None]:
    """Report every Event raised in this context (this thread) to `listener`."""
    token = _listener.set(listener)
    try:
        yield
    finally:
        _listener.reset(token)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict  # already converted by convert_schema


@dataclass(frozen=True)
class Call:
    id: str
    name: str
    args: dict


@dataclass
class Reply:
    text: str = ""
    calls: list[Call] = field(default_factory=list)


#: Sent with a session's final step (Cleanup Step 11): the loop keeps its round limit, and the last
#: round is an answer rather than one more call.
LAST_ROUND = ("[system] This is your last round: no tool can be called. Answer the person now, in "
              "words, from the tool replies above.")


class Session(Protocol):
    def step(self, final: bool = False) -> Reply: ...
    def add_results(self, results: list[tuple[Call, str]]) -> None: ...
    #: A message from the app, not the person: the figure check's correction (webapp/verify.py).
    def add_user(self, text: str) -> None: ...
    #: Tool replies another provider's model gathered this turn, as text after the question, each
    #: shortenable to the budget like a tool reply (step 2: a failover does not re-run tools).
    def add_gathered(self, note: str, results: list[tuple[Call, str]]) -> None: ...


def gathered_text(i: int, n: int, call: Call, text: str) -> str:
    """One gathered reply as the next provider reads it."""
    args = ", ".join(f"{k}={v!r}" for k, v in call.args.items() if v is not None)
    return f"[Tool reply {i} of {n}, already run: {call.name}({args})]\n{text}"


class Provider(Protocol):
    name: str

    def available(self) -> bool: ...
    def start(self, system: str, history: list[dict], message: str,
              tools: list[ToolSpec]) -> Session: ...


# --- environment -------------------------------------------------------------------------------

def load_env(path: Path | None = None) -> list[str]:
    """Read KEY=VALUE lines from the repository's .env into os.environ, never overwriting what is
    already set. Returns the NAMES loaded, never the values."""
    path = path or PROJECT_ROOT / ".env"
    loaded: list[str] = []
    if not path.is_file():
        return loaded
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


# --- schemas -----------------------------------------------------------------------------------

def convert_schema(schema: Any, *, drop: frozenset[str] = frozenset({"workspace_id"})) -> Any:
    """A FastMCP JSON schema as a function declaration's parameters.

    `anyOf: [X, {type: null}]` becomes X with nullable, keys outside the OpenAPI subset go, and
    the properties named in `drop` are removed -- the model never chooses a workspace.
    """
    if isinstance(schema, list):
        return [convert_schema(s, drop=drop) for s in schema]
    if not isinstance(schema, dict):
        return schema
    if "anyOf" in schema:
        branches = [b for b in schema["anyOf"] if b.get("type") != "null"]
        merged = dict(branches[0]) if len(branches) == 1 else {"type": "string"}
        if len(branches) < len(schema["anyOf"]):
            merged["nullable"] = True
        if "description" in schema:
            merged.setdefault("description", schema["description"])
        return convert_schema(merged, drop=drop)
    out: dict = {}
    for key, value in schema.items():
        if key not in _KEEP:
            continue
        if key == "properties":
            out[key] = {k: convert_schema(v, drop=drop) for k, v in value.items() if k not in drop}
        elif key == "required":
            out[key] = [k for k in value if k not in drop]
        else:
            out[key] = convert_schema(value, drop=drop)
    if out.get("type") == "object" and "properties" not in out:
        out["properties"] = {}
    if not out.get("required"):
        out.pop("required", None)
    return out


def to_json_schema(schema: Any) -> Any:
    """The converted (OpenAPI-subset) schema in standard JSON Schema, for OpenAI-compatible
    providers: `nullable: true` becomes a type list with "null".

    Groq validates a model's tool call against the schema it was given and does not know the
    OpenAPI keyword, so gpt-oss's `"question": null` for an optional field was refused with
    "expected string, but got null" (measured live, P14-D27). Gemini keeps `nullable`.
    """
    if isinstance(schema, list):
        return [to_json_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: (to_json_schema(v) if k != "properties"
               else {name: to_json_schema(sub) for name, sub in v.items()})
           for k, v in schema.items() if k != "nullable"}
    if schema.get("nullable") and isinstance(out.get("type"), str):
        out["type"] = [out["type"], "null"]
    return out


# --- HTTP --------------------------------------------------------------------------------------

#: Waiting on a busy provider. Measured on the free tiers, 22/09/2026: Gemini answered 503 "high
#: demand", and Groq 429 on tokens-per-minute with "Please try again in 3.9675s" -- each request
#: carries ~5,000 tokens of tool manuals against Groq's 8,000 a minute. A wait the provider asks
#: for is honoured up to MAX_WAIT_S; a longer one (a spent daily quota) fails over instead of
#: stalling the person (P14-D26).
ATTEMPTS = 3
#: One rate-limit window. Gemini's free tier allows 5 requests a minute on gemini-3.8-flash and
#: asked for "retry in 40.26s"; a 20 s cap failed over instead (measured, P14-D26). A per-minute
#: limit always clears within 60 s; a spent daily quota asks for hours and still fails over.
MAX_WAIT_S = 60.0
BACKOFF_S = (2.0, 5.0)
_WAIT_HINT = re.compile(r"(?:try again in|retry in)\s+([0-9.]+)\s*(ms|s)\b", re.I)
_RETRY_DELAY = re.compile(r'"retryDelay"\s*:\s*"([0-9.]+)s"')
_TOO_LARGE = re.compile(r"Limit\s+(\d+),\s*Requested\s+(\d+)", re.I)
_PER_DAY = re.compile(r"PerDay|per[- ]day", re.I)
_URL_MODEL = re.compile(r"/models/([^/:?]+):")
_sleep = time.sleep  # a test replaces it


def _who(provider: str, url: str, body: dict | None) -> str:
    """"provider (model)" for a person: the model from the body (OpenAI shape) or the URL
    (Gemini's)."""
    model = (body or {}).get("model")
    if not model:
        m = _URL_MODEL.search(url)
        model = m.group(1) if m else None
    return f"{provider} ({model})" if model else provider


def _wait_for(exc: urllib.error.HTTPError, detail: str, attempt: int) -> float:
    """Seconds the provider asked us to wait: Retry-After, Groq's "try again in Xs", Gemini's
    retryDelay -- else a short backoff."""
    header = exc.headers.get("Retry-After") if exc.headers else None
    if header and header.replace(".", "", 1).isdigit():
        return float(header)
    m = _WAIT_HINT.search(detail)
    if m:
        return float(m.group(1)) / (1000 if m.group(2).lower() == "ms" else 1)
    m = _RETRY_DELAY.search(detail)
    if m:
        return float(m.group(1))
    return BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]


def _classify(provider: str, code: int, body: str) -> ProviderError:
    """An HTTP error as a ProviderError, with the kind and one readable sentence.

    Read from the whole body, before any truncation: the quota id sits deep in Gemini's
    details. Measured 22/09/2026: a spent free tier answers 429 with quotaId
    "GenerateRequestsPerDayPerProjectPerModel-FreeTier" and "retry in 59s" -- a wait that would
    not help for hours; other models kept their own quota (P14-D32).
    """
    message = body
    try:
        err = json.loads(body).get("error", {})
        message = err.get("message", body) if isinstance(err, dict) else body
    except (json.JSONDecodeError, AttributeError):
        pass
    first = message.strip().split("\n")[0][:240]
    if code == 413:
        # Groq, 25/09/2026: "Request too large for model `openai/gpt-oss-120b` ... tokens per
        # minute (TPM): Limit 8000, Requested 8917". Retryable: the session shrinks and sends it
        # once more, and a second 413 fails over (step 2).
        m = _TOO_LARGE.search(message)
        lim, asked = (int(m.group(1)), int(m.group(2))) if m else (None, None)
        return ProviderError(provider, f"HTTP 413: {body[:500]}", True, kind="too_large",
                             limit=lim, requested=asked,
                             summary="the request was larger than the model accepts" + (
                                 f" ({asked:,} tokens against a limit of {lim:,})" if m else ""))
    # Gemini's quota id says PerDay; OpenRouter's "free-models-per-day"; Groq's "tokens per day
    # (TPD)" -- a wait of minutes to hours, never worth sleeping on.
    if code == 429 and _PER_DAY.search(body):
        m = re.search(r"model:\s*([\w.\-]+)", message)
        # A model name can hold dots (3.8) but not end in one: "model: gemini-3.8-flash." in a
        # sentence must not name a model that does not exist.
        model = m.group(1).rstrip(".") if m else None
        return ProviderError(provider, f"HTTP 429: {body[:500]}", True, kind="daily_quota",
                             model=model, summary=f"the free daily quota"
                             f"{' for ' + model if model else ''} is used up")
    if code == 404 and provider == "gemini":
        # "This model models/gemini-2.5-flash is no longer available to new users" -- seen live
        # 26/09/2026 with GEMINI_MODEL pinned to it. Not the turn's fault: the next model, or
        # the next provider, can answer (step 2).
        return ProviderError(provider, f"HTTP 404: {body[:500]}", True, kind="model_gone",
                             summary=f"the model is not available ({first})")
    if code == 400 and "failed_generation" in body:
        # Groq could not parse the model's own output -- a malformed tool call. A fresh sample
        # usually parses; the session retries (Cleanup Step 10, seen live).
        return ProviderError(provider, f"HTTP 400: {body[:500]}", False,
                             kind="generation_failed",
                             summary="the model's reply could not be parsed")
    if code == 400 and "tool_use_failed" in body:
        return ProviderError(provider, f"HTTP 400: {body[:500]}", False, kind="tool_use_failed",
                             summary=f"refused the model's tool call ({first})")
    if code == 429:
        return ProviderError(provider, f"HTTP 429: {body[:500]}", True,
                             summary="rate limit reached; try again in a minute")
    if code >= 500:
        return ProviderError(provider, f"HTTP {code}: {body[:500]}", True,
                             summary=f"the service is unavailable right now ({first})")
    return ProviderError(provider, f"HTTP {code}: {body[:500]}", False,
                         summary=f"HTTP {code}: {first}")


def _wait(who: str, why: str, seconds: float, attempt: int) -> None:
    """Sleep on a busy provider, having said so: the Ask screen once showed "Reading the data..."
    for seven minutes of these (25/09/2026)."""
    notify(Event("wait", f"{who}: {why} -- waiting {max(1, round(seconds))} s, then trying again "
                         f"(attempt {attempt + 2} of {ATTEMPTS})", who, seconds=seconds))
    _sleep(seconds)


def _request(provider: str, url: str, headers: dict, body: dict | None = None, *,
             max_wait: float = MAX_WAIT_S) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    for attempt in range(ATTEMPTS):
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json", "User-Agent": USER_AGENT, **headers})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            error = _classify(provider, exc.code, detail)
            busy = exc.code in (429, 500, 502, 503, 504) and error.kind != "daily_quota"
            if busy and attempt + 1 < ATTEMPTS:
                wait = _wait_for(exc, detail, attempt)
                if wait <= max_wait:
                    why = ("rate limit reached" if exc.code == 429
                           else f"busy (HTTP {exc.code})")
                    _wait(_who(provider, url, body), why, wait + 0.25, attempt)
                    continue
            raise error from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt + 1 < ATTEMPTS:
                _wait(_who(provider, url, body), "no response",
                      BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)], attempt)
                continue
            raise ProviderError(provider, f"no response ({type(exc).__name__})", True) from None
    raise AssertionError("unreachable")


# --- the budget, shared by every session (step 2) ----------------------------------------------

class _Budgeted:
    """A session whose every request is kept to its model's token budget (webapp/budget.py).

    `_slots` lists where the shortenable texts sit, oldest first: the earlier messages, then each
    tool reply as it arrives. A 413 is answered once: the limit and count the provider named are
    learnt, the request shrunk again, and sent again; a second 413 goes to the loop, which fails
    over.
    """

    _slots: list[budget.Slot]

    def _names(self) -> tuple[str, str]:
        raise NotImplementedError

    def _fit(self, body: dict) -> int:
        provider, model = self._names()
        allowed = budget.allowance(provider, model)
        if allowed is None:
            return 0
        cut = budget.fit(self._slots, lambda: budget.estimate(body), allowed)
        if cut:
            notify(Event("shortened", (
                f"{provider} ({model}): {cut} earlier tool repl{'y' if cut == 1 else 'ies'} "
                f"shortened for the model to fit its request limit of "
                f"{budget.limit(provider, model):,} tokens; every reply is whole under What I "
                f"did"), f"{provider} ({model})"))
        return cut

    def _send(self, body: dict, post: Callable[[], dict]) -> dict:
        self._fit(body)
        try:
            return post()
        except ProviderError as exc:
            if exc.kind != "too_large":
                raise
            provider, model = self._names()
            budget.learn(provider, model, limit_tokens=exc.limit, requested=exc.requested,
                         estimated=budget.estimate(body))
            if not self._fit(body):
                raise
            return post()


# --- Gemini ------------------------------------------------------------------------------------

def _declaration(t: ToolSpec) -> dict:
    """A Gemini function declaration. A tool left with no parameters once workspace_id is gone
    (list_datasets, get_workflow_state) omits `parameters`: an OBJECT with empty properties is
    one of the shapes Gemini rejects."""
    out = {"name": t.name, "description": t.description}
    if t.parameters.get("properties"):
        out["parameters"] = t.parameters
    return out


class GeminiSession(_Budgeted):
    def __init__(self, provider: "Gemini", system: str, history: list[dict], message: str,
                 tools: list[ToolSpec]) -> None:
        self._p = provider
        self._model = provider.model()  # pinned: signature parts belong to the model that wrote them
        earlier = [{"role": "model" if m["role"] == "assistant" else "user",
                    "parts": [{"text": m["content"]}]} for m in history if m.get("content")]
        self._slots = [budget.Slot(c["parts"][0], "text", "history") for c in earlier]
        self._body: dict = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": earlier + [{"role": "user", "parts": [{"text": message}]}],
        }
        # No tools (step 2: answering from replies another provider gathered) sends no
        # declarations -- an empty list is not a shape to test Gemini with.
        if tools:
            self._body["tools"] = [{"functionDeclarations": [_declaration(t) for t in tools]}]
            self._body["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}

    def _names(self) -> tuple[str, str]:
        return "gemini", self._model

    def step(self, final: bool = False) -> Reply:
        if final:
            if "tools" in self._body:
                self._body["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}
            last = self._body["contents"][-1]
            if last.get("role") == "user":
                last["parts"].append({"text": LAST_ROUND})
            else:
                self._body["contents"].append({"role": "user", "parts": [{"text": LAST_ROUND}]})
        data = self._send(self._body, lambda: self._p.post(self._body, self._model))
        candidates = data.get("candidates") or []
        if not candidates or "content" not in candidates[0]:
            reason = (candidates[0].get("finishReason") if candidates
                      else data.get("promptFeedback", {}).get("blockReason", "no candidate"))
            raise ProviderError("gemini", f"no content returned ({reason})", retryable=False)
        content = candidates[0]["content"]
        self._body["contents"].append(content)  # verbatim: keeps any signature parts
        reply = Reply()
        for i, part in enumerate(content.get("parts", [])):
            if "functionCall" in part:
                fc = part["functionCall"]
                reply.calls.append(Call(fc.get("id") or f"call_{i}", fc["name"],
                                        dict(fc.get("args") or {})))
            elif "text" in part and not part.get("thought"):
                reply.text += part["text"]
        return reply

    def add_results(self, results: list[tuple[Call, str]]) -> None:
        parts = []
        for call, text in results:
            response: dict = {"name": call.name, "response": {"result": text}}
            if not call.id.startswith("call_"):
                response["id"] = call.id
            parts.append({"functionResponse": response})
            self._slots.append(budget.Slot(response["response"], "result", "reply"))
        self._body["contents"].append({"role": "user", "parts": parts})

    def add_user(self, text: str) -> None:
        self._body["contents"].append({"role": "user", "parts": [{"text": text}]})

    def add_gathered(self, note: str, results: list[tuple[Call, str]]) -> None:
        """Text parts on the question's own content: no function call is replayed, so no thought
        signature is needed for one this model did not write."""
        parts = [{"text": gathered_text(i, len(results), call, text)}
                 for i, (call, text) in enumerate(results, 1)]
        last = self._body["contents"][-1]
        if last.get("role") != "user":
            last = {"role": "user", "parts": []}
            self._body["contents"].append(last)
        last["parts"].extend([{"text": note}] + parts)
        self._slots.extend(budget.Slot(p, "text", "reply") for p in parts)


#: gemini-<major>[.<minor>]-flash exactly: no lite, image, tts, preview or other variant.
_NUMBERED_FLASH = re.compile(r"^gemini-(\d+)(?:\.(\d+))?-flash$")


def gemini_ladder(names: list[str]) -> list[str]:
    """Every model worth trying, best first: the alias, the numbered flash models high to low,
    then the lite alias. The free quota is per model per day, so the next rung has its own."""
    first = choose_gemini_model(names)
    numbered = sorted(((int(m.group(1)), int(m.group(2) or 0), n)
                       for n in names if (m := _NUMBERED_FLASH.match(n))), reverse=True)
    ladder = [first] + [n for *_, n in numbered]
    if "gemini-flash-lite-latest" in names:
        ladder.append("gemini-flash-lite-latest")
    return list(dict.fromkeys(ladder))


def choose_gemini_model(names: list[str]) -> str:
    """Google's own alias for the current Flash model if it is offered, else the highest-NUMBERED
    plain flash model.

    The first version sorted names as strings, took the last, and got "gemini-omni-1.1-flash" --
    "omni" sorts after "3.8" -- whose free tier returned 429 on the first request. Measured on a
    free key, 22/09/2026: gemini-flash-latest 200 (serving gemini-3.8-flash); gemini-2.5-flash 404
    "no longer available to new users" (P14-D25).
    """
    if "gemini-flash-latest" in names:
        return "gemini-flash-latest"
    numbered = [(int(m.group(1)), int(m.group(2) or 0), n)
                for n in names if (m := _NUMBERED_FLASH.match(n))]
    if numbered:
        return max(numbered)[2]
    flash = [n for n in names if "flash" in n]
    if flash or names:
        return (flash or names)[0]
    raise ProviderError("gemini", "the model list offers nothing that generates", False)


class Gemini:
    name = "gemini"

    def __init__(self) -> None:
        self._ladder: list[str] | None = None
        self._spent: dict[str, str] = {}  # model -> the day its free quota ran out

    def _key(self) -> str:
        return os.environ.get("GEMINI_API_KEY", "")

    def available(self) -> bool:
        return bool(self._key())

    def ladder(self) -> list[str]:
        """GEMINI_MODEL alone if set; otherwise the provider's own list, best first -- measured,
        not recalled."""
        if self._ladder is None:
            pinned = os.environ.get("GEMINI_MODEL")
            self._ladder = [pinned] if pinned else self._discover()
        return self._ladder

    def _discover(self) -> list[str]:
        data = _request("gemini", f"{GEMINI_URL}/models?pageSize=1000",
                        {"x-goog-api-key": self._key()})
        names = [m["name"].removeprefix("models/") for m in data.get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
        return gemini_ladder(names)

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    def model(self) -> str:
        """The best model whose free quota has not run out today."""
        for name in self.ladder():
            if self._spent.get(name) != self._today():
                return name
        raise ProviderError("gemini", "every model's free daily quota is used up", True,
                            kind="daily_quota", summary="every model's free daily quota is used "
                            "up; it resets tomorrow")

    def has_another_model(self) -> bool:
        return any(self._spent.get(n) != self._today() for n in self.ladder())

    def post(self, body: dict, model: str | None = None, *, max_wait: float = MAX_WAIT_S) -> dict:
        model = model or self.model()
        try:
            return _request("gemini", f"{GEMINI_URL}/models/{model}:generateContent",
                            {"x-goog-api-key": self._key()}, body, max_wait=max_wait)
        except ProviderError as exc:
            if exc.kind in ("daily_quota", "model_gone"):
                # The alias has no quota of its own; the error names the model serving it, and
                # both are spent for today.
                for spent in {model, exc.model} - {None}:
                    self._spent[spent] = self._today()
            raise

    def complete(self, system: str, prompt: str, *, max_wait: float) -> tuple[str, str]:
        """One request, no tools, JSON out: (text, model)."""
        model = self.model()
        data = self.post({
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
        }, model, max_wait=max_wait)
        parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts if not p.get("thought")), model

    def start(self, system, history, message, tools) -> GeminiSession:
        return GeminiSession(self, system, history, message, tools)


# --- Groq and OpenRouter (OpenAI-compatible) --------------------------------------------------------

class OpenAISession(_Budgeted):
    """One conversation in the OpenAI chat-completions shape, which Groq and OpenRouter share.
    Each message is a slot for the budget: the earlier conversation as history, every tool reply
    (or reply gathered by another provider) as a reply."""

    def __init__(self, provider: "Groq | OpenRouter", system: str, history: list[dict],
                 message: str, tools: list[ToolSpec]) -> None:
        self._p = provider
        earlier = [{"role": m["role"], "content": m["content"]} for m in history
                   if m.get("content")]
        self._messages: list[dict] = (
            [{"role": "system", "content": system}] + earlier
            + [{"role": "user", "content": message}])
        self._slots = [budget.Slot(m, "content", "history") for m in earlier]
        self._tools = [{"type": "function", "function": {
            "name": t.name, "description": t.description,
            "parameters": to_json_schema(t.parameters)}} for t in tools]

    def _names(self) -> tuple[str, str]:
        return self._p.name, self._p.model()

    def _body(self, final: bool) -> dict:
        body: dict = {"messages": self._messages}
        # No tools (step 2: answering from replies another provider gathered) sends neither key:
        # tool_choice without tools is not a request the API defines.
        if self._tools:
            body["tools"] = self._tools
            body["tool_choice"] = "none" if final else "auto"
        return body

    def step(self, final: bool = False) -> Reply:
        if final:
            self._messages.append({"role": "user", "content": LAST_ROUND})
        for _ in range(3):
            try:
                body = self._body(final)
                data = self._send(body, lambda: self._p.post(body))
                break
            except ProviderError as exc:
                if exc.kind == "generation_failed":
                    self._messages.append({"role": "user", "content": (
                        "[system] Your last reply could not be parsed. Answer the person in "
                        "plain words, or call one tool with valid JSON arguments.")})
                    continue
                if exc.kind != "tool_use_failed":
                    raise
                # Groq refuses a call to a tool it was not given and returns no message; the
                # model copied an engine NEXT STEP naming one (seen live: propose_dataset_contract).
                # Tell it, and let it answer instead (P14-D33).
                m = re.search(r"tool '([\w]+)'", str(exc))
                self._messages.append({"role": "user", "content": (
                    f"[system] {m.group(1) if m else 'That tool'} is not one of your tools. Do not "
                    f"call it. Answer the person in words; if a step is theirs to do, say which "
                    f"screen does it.")})
        else:
            raise ProviderError(self._p.name, "three replies in a row were refused", False,
                                summary="three replies in a row were refused (a tool it does "
                                        "not have, or output that could not be parsed)")
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError(self._p.name, "no message returned", retryable=False) from None
        self._messages.append({k: v for k, v in message.items()
                               if k in ("role", "content", "tool_calls")})
        reply = Reply(text=message.get("content") or "")
        for tc in message.get("tool_calls") or []:
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            reply.calls.append(Call(tc["id"], tc["function"]["name"], args))
        return reply

    def add_results(self, results: list[tuple[Call, str]]) -> None:
        for call, text in results:
            message = {"role": "tool", "tool_call_id": call.id, "content": text}
            self._messages.append(message)
            self._slots.append(budget.Slot(message, "content", "reply"))

    def add_user(self, text: str) -> None:
        self._messages.append({"role": "user", "content": text})

    def add_gathered(self, note: str, results: list[tuple[Call, str]]) -> None:
        """The note, then each gathered reply as its own user message: no tool call is replayed,
        and each reply is a slot the budget can shorten apart from the others."""
        self._messages.append({"role": "user", "content": note})
        for i, (call, text) in enumerate(results, 1):
            message = {"role": "user", "content": gathered_text(i, len(results), call, text)}
            self._messages.append(message)
            self._slots.append(budget.Slot(message, "content", "reply"))


#: The name the Groq session had before OpenRouter shared it (step 2).
GroqSession = OpenAISession


class Groq:
    name = "groq"
    #: Tried in order when GROQ_MODEL is unset; the first the account's model list offers wins.
    PREFERENCE = ("llama-3.3-70b-versatile", "openai/gpt-oss-120b", "openai/gpt-oss-20b",
                  "llama-3.1-8b-instant")

    def __init__(self) -> None:
        self._model: str | None = None

    def _key(self) -> str:
        return os.environ.get("GROQ_API_KEY", "")

    def available(self) -> bool:
        return bool(self._key())

    def model(self) -> str:
        if self._model is None:
            self._model = os.environ.get("GROQ_MODEL") or self._discover()
        return self._model

    def _discover(self) -> str:
        data = _request("groq", f"{GROQ_URL}/models", {"Authorization": f"Bearer {self._key()}"})
        offered = {m.get("id") for m in data.get("data", [])}
        for name in self.PREFERENCE:
            if name in offered:
                return name
        raise ProviderError("groq", f"none of {', '.join(self.PREFERENCE)} is offered; set "
                            "GROQ_MODEL to a tool-calling model", retryable=False)

    def post(self, body: dict, *, max_wait: float = MAX_WAIT_S) -> dict:
        return _request("groq", f"{GROQ_URL}/chat/completions",
                        {"Authorization": f"Bearer {self._key()}"},
                        {"model": self.model(), **body}, max_wait=max_wait)

    def complete(self, system: str, prompt: str, *, max_wait: float) -> tuple[str, str]:
        """One request, no tools, JSON out: (text, model)."""
        data = self.post({"messages": [{"role": "system", "content": system},
                                       {"role": "user", "content": prompt}],
                          "response_format": {"type": "json_object"}, "temperature": 0},
                         max_wait=max_wait)
        try:
            return data["choices"][0]["message"].get("content") or "", self.model()
        except (KeyError, IndexError):
            raise ProviderError("groq", "no message returned", retryable=False) from None

    def start(self, system, history, message, tools) -> OpenAISession:
        return OpenAISession(self, system, history, message, tools)


class OpenRouter:
    """OpenRouter's OpenAI-compatible API (step 2), third in the default order: its free models
    are a separate quota from Gemini's and Groq's. Not verified live -- no key was available
    when it was written; every test of it is scripted HTTP."""

    name = "openrouter"
    #: Tried in order when OPENROUTER_MODEL is unset: free models (":free") that take tools; the
    #: first the account's model list offers wins. Failing all, the free tool-calling model with
    #: the longest context the list offers -- measured from the list, not recalled.
    PREFERENCE = ("openai/gpt-oss-120b:free", "meta-llama/llama-3.3-70b-instruct:free",
                  "qwen/qwen3-235b-a22b:free", "openai/gpt-oss-20b:free")

    def __init__(self) -> None:
        self._model: str | None = None

    def _key(self) -> str:
        return os.environ.get("OPENROUTER_API_KEY", "")

    def _headers(self) -> dict:
        # X-Title names the app on OpenRouter's side; optional, and no personal data.
        return {"Authorization": f"Bearer {self._key()}", "X-Title": "analytics-agent"}

    def available(self) -> bool:
        return bool(self._key())

    def model(self) -> str:
        if self._model is None:
            self._model = os.environ.get("OPENROUTER_MODEL") or self._discover()
        return self._model

    def _discover(self) -> str:
        data = _request("openrouter", f"{OPENROUTER_URL}/models", self._headers())
        free = [m for m in data.get("data", []) if isinstance(m, dict)
                and str(m.get("id", "")).endswith(":free")
                and "tools" in (m.get("supported_parameters") or [])]
        offered = {m["id"] for m in free}
        for name in self.PREFERENCE:
            if name in offered:
                return name
        if free:
            return max(free, key=lambda m: m.get("context_length") or 0)["id"]
        raise ProviderError("openrouter", "no free model that calls tools is offered; set "
                            "OPENROUTER_MODEL", retryable=False)

    def post(self, body: dict, *, max_wait: float = MAX_WAIT_S) -> dict:
        data = _request("openrouter", f"{OPENROUTER_URL}/chat/completions", self._headers(),
                        {"model": self.model(), **body}, max_wait=max_wait)
        # OpenRouter can answer 200 with an upstream provider's error in the body; read it as
        # the HTTP error it stands for, so a busy upstream fails over like a 503.
        error = data.get("error") if isinstance(data, dict) else None
        if error and not data.get("choices"):
            code = error.get("code") if isinstance(error, dict) else None
            raise _classify("openrouter", code if isinstance(code, int) else 502,
                            json.dumps(data))
        return data

    def complete(self, system: str, prompt: str, *, max_wait: float) -> tuple[str, str]:
        """One request, no tools, JSON out: (text, model)."""
        data = self.post({"messages": [{"role": "system", "content": system},
                                       {"role": "user", "content": prompt}],
                          "response_format": {"type": "json_object"}, "temperature": 0},
                         max_wait=max_wait)
        try:
            return data["choices"][0]["message"].get("content") or "", self.model()
        except (KeyError, IndexError, TypeError):
            raise ProviderError("openrouter", "no message returned", retryable=False) from None

    def start(self, system, history, message, tools) -> OpenAISession:
        return OpenAISession(self, system, history, message, tools)


PROVIDERS = {"gemini": Gemini, "groq": Groq, "openrouter": OpenRouter}
#: Tried in this order unless ANALYTICS_LLM says otherwise; each needs its key to be tried.
DEFAULT_ORDER = "gemini,groq,openrouter"

#: What a one-shot request waits on a rate limit before trying the next provider. The contract
#: form waits on it; the Ask screen once sat seven minutes on 60-second waits (25/09/2026).
QUICK_WAIT_S = 5.0


def parse_json(text: str) -> dict:
    """The JSON object in a model's reply: bare, fenced, or inside prose. ValueError if none."""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.rsplit("```", 1)[0]
    try:
        value = json.loads(body)
    except json.JSONDecodeError:
        start, end = body.find("{"), body.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object in the reply") from None
        value = json.loads(body[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("the reply is JSON but not an object")
    return value


def complete_json(providers: list, system: str, prompt: str, *,
                  max_wait: float = QUICK_WAIT_S) -> tuple[dict, str, list[str]]:
    """One JSON answer from the first provider that gives one: (object, "provider/model",
    the failures before it). ProviderError naming every failure when none does."""
    failures: list[str] = []
    for provider in providers:
        for _attempt in range(6):
            try:
                text, model = provider.complete(system, prompt, max_wait=max_wait)
                return parse_json(text), f"{provider.name}/{model}", failures
            except ProviderError as exc:
                failures.append(exc.summary)
                more = getattr(provider, "has_another_model", lambda: False)()
                if exc.kind in ("daily_quota", "model_gone") and more:
                    continue
                break
            except (ValueError, json.JSONDecodeError) as exc:
                failures.append(f"{provider.name}: the reply was not a JSON object ({exc})")
                break
    raise ProviderError("every provider", "; ".join(failures) or "none is configured", True,
                        summary="; ".join(failures) or "no model is configured")
_INSTANCES: dict[str, Provider] = {}  # one each per process, so a model is discovered once


def configured() -> list[Provider]:
    """Providers in ANALYTICS_LLM order (default DEFAULT_ORDER) that have a key."""
    load_env()
    order = [n.strip() for n in os.environ.get("ANALYTICS_LLM", DEFAULT_ORDER).split(",")]
    for n in order:
        if n in PROVIDERS and n not in _INSTANCES:
            _INSTANCES[n] = PROVIDERS[n]()
    return [_INSTANCES[n] for n in order if n in _INSTANCES and _INSTANCES[n].available()]


__all__ = ["DEFAULT_ORDER", "Call", "Event", "Gemini", "Groq", "OpenAISession", "OpenRouter",
           "Provider", "ProviderError", "QUICK_WAIT_S", "Reply", "Session", "ToolSpec",
           "choose_gemini_model", "complete_json", "configured", "gathered_text", "gemini_ladder",
           "convert_schema", "listening", "load_env", "notify", "parse_json", "to_json_schema"]
