"""Turns are server-owned: send a question once, then follow it by id.

A question is never sent again automatically. After a timeout the server may have
stored it, so the session's turn list is checked; only the person can send again.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from frontend.api_client import APIClient, APIError

ACTIVE = frozenset({"queued", "running"})
TERMINAL_NOTES = {
    "failed": "This question could not be answered.",
    "interrupted": "The service restarted while this question was running. It was not "
                   "run again; ask again if you still want an answer.",
}


@dataclass(frozen=True)
class Submission:
    outcome: str                  # sent | received | not_received | unknown | error
    turn_id: str | None = None
    error: APIError | None = None


def is_active(turn: dict) -> bool:
    return turn.get("status") in ACTIVE


def submit(api: APIClient, sid: str, dataset_id: str, question: str,
           known_turn_ids: Iterable[str]) -> Submission:
    """POST /turns once. On a lost reply, look for the question among new turns."""
    try:
        return Submission("sent", api.create_turn(sid, dataset_id, question)["turn_id"])
    except APIError as error:
        if error.status_code is not None:
            return Submission("error", error=error)       # the server answered: not stored
        sent_error = error
    known = set(known_turn_ids)
    try:
        turns = api.list_turns(sid)["turns"]
    except APIError:
        return Submission("unknown", error=sent_error)
    for turn in turns:
        if turn["turn_id"] not in known and turn.get("question") == question:
            return Submission("received", turn["turn_id"], error=sent_error)
    return Submission("not_received", error=sent_error)
