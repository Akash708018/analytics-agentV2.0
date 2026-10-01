"""Session hydration, one versioned save per change, and explicit conflict choices.

The backend session (GET/PUT /sessions/{sid}) is the only durable copy of the
person's work; st.session_state is a cache. F0 measured why: refresh resets it,
and navigation deletes the state of widgets that leave the page. So widgets are
seeded from a draft and copy edits back into it, and only an explicit allowlist
of draft fields is ever sent. See docs/steps/F2.md.

Every function takes the session-state mapping `ss` so it can be tested with a dict.

Streamlit may stop a run at ANY session-state read or write, when a newer rerun is
waiting (SafeSessionState's yield callback). F2's browser check caught a save cut in
half that way: the PUT landed, the new version was never recorded, and the next run
reported a conflict with itself. So after a network call returns, functions here
touch only plain objects already in hand, never `ss` itself.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from collections.abc import MutableMapping
from typing import Any

from frontend.api_client import APIClient, APIError, VersionConflict

SCHEMA = 1
PAGES = ("session", "data", "ask")
DEFAULT_PAGE = "session"
LABEL_MAX = 120
NAME_MAX = 200
DATASETS_MAX = 50
_DATASET_ID = re.compile(r"^ds_[A-Za-z0-9_-]{1,64}$")
_KNOWN = {"schema", "page", "label", "dataset_id", "datasets"}

# Widget key -> draft field. These are the only widgets whose values are saved.
WIDGETS = {"ui.session.label": "label", "ui.data.dataset_id": "dataset_id"}

# Session-state keys owned by this module.
SID = "_sid"            # the sid the cache below belongs to
SERVER = "_server"      # last state read from / written to the backend
DRAFT = "_draft"        # what the person has now
SAVE = "_save"          # {"status": saved|error|rejected|conflict|expired, ...}
RESTORE = "_restore"    # True until the first run after hydration has picked the page

Mapping = MutableMapping[str, Any]


def parse_sid(raw: Any) -> str | None:
    """A canonical lowercase UUID, or None. Nothing else is sent to the backend."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = uuid.UUID(raw)
    except ValueError:
        return None
    return str(parsed) if str(parsed) == raw.lower() else None


def _dataset(raw: Any) -> dict | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("dataset_id"), str):
        return None
    if not _DATASET_ID.match(raw["dataset_id"]):
        return None
    name = raw.get("name")
    out: dict[str, Any] = {"dataset_id": raw["dataset_id"],
                           "name": name[:NAME_MAX] if isinstance(name, str) else ""}
    for field in ("rows", "columns"):
        value = raw.get(field)
        out[field] = value if isinstance(value, int) and not isinstance(value, bool) else None
    return out


def normalize(ui_state: Any) -> dict:
    """The allowlisted shape. Keys this version does not know are kept, unchanged."""
    raw = ui_state if isinstance(ui_state, dict) else {}
    datasets: list[dict] = []
    for item in raw.get("datasets", []) if isinstance(raw.get("datasets"), list) else []:
        ds = _dataset(item)
        if ds and all(d["dataset_id"] != ds["dataset_id"] for d in datasets):
            datasets.append(ds)
    datasets = datasets[-DATASETS_MAX:]
    ids = {d["dataset_id"] for d in datasets}
    label = raw.get("label")
    out = {k: copy.deepcopy(v) for k, v in raw.items() if k not in _KNOWN}
    out.update({
        "schema": SCHEMA,
        "page": raw.get("page") if raw.get("page") in PAGES else DEFAULT_PAGE,
        "label": label[:LABEL_MAX] if isinstance(label, str) else "",
        "dataset_id": raw.get("dataset_id") if raw.get("dataset_id") in ids else None,
        "datasets": datasets,
    })
    return out


def digest(ui_state: dict) -> str:
    blob = json.dumps(ui_state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def _work(ui_state: dict) -> str:
    """The person's work: everything but which page a tab last showed."""
    return digest({k: v for k, v in ui_state.items() if k != "page"})


def _remember(server: dict, session: dict) -> None:
    """Record what the server holds, in place (no session-state access)."""
    stored = normalize(session.get("ui_state"))
    server.clear()
    server.update({
        "version": session["version"],
        "ui_state": stored,
        "digest": digest(stored),
        "workspace_id": session.get("workspace_id"),
        "expires_at": session.get("expires_at"),
    })


def _set(save: dict, **status: Any) -> dict:
    save.clear()
    save.update(status)
    return save


def _reseed(ss: Mapping) -> None:
    """Drop cached widget values so the next render seeds them from the draft."""
    for key in WIDGETS:
        if key in ss:
            del ss[key]


def needs_hydration(ss: Mapping, sid: str) -> bool:
    return ss.get(SID) != sid or SERVER not in ss or DRAFT not in ss


def hydrate(ss: Mapping, api: APIClient, sid: str) -> APIError | None:
    """Load the session before any widget exists. Returns the error, or None."""
    try:
        session = api.get_session(sid)
    except APIError as error:
        return error
    server: dict = {}
    _remember(server, session)
    ss[SERVER] = server
    ss[SID] = sid
    ss[DRAFT] = copy.deepcopy(server["ui_state"])
    ss[SAVE] = {"status": "saved"}
    ss[RESTORE] = True
    _reseed(ss)
    return None


def seed(ss: Mapping, key: str) -> None:
    """Before the widget is created: give it the draft's value, or, if it already holds
    a different one, that is an edit whose callback was cut short: keep the edit."""
    draft = ss[DRAFT]
    if key not in ss:
        ss[key] = draft.get(WIDGETS[key])
    elif ss[key] != draft.get(WIDGETS[key]):
        draft[WIDGETS[key]] = ss[key]


def on_widget_change(ss: Mapping, key: str) -> None:
    ss[DRAFT][WIDGETS[key]] = ss[key]


def take_restore_target(ss: Mapping, current_page: str) -> str | None:
    """On the first run after hydration only: the saved page, if the URL named none."""
    if not ss.pop(RESTORE, False):
        return None
    saved = ss[DRAFT].get("page")
    if current_page == DEFAULT_PAGE and saved in PAGES and saved != DEFAULT_PAGE:
        return saved
    return None


def set_page(ss: Mapping, page: str) -> None:
    if page in PAGES:
        ss[DRAFT]["page"] = page


def add_dataset(draft: dict, dataset: dict) -> str | None:
    """Record an uploaded dataset (display metadata only) as the active one, in the
    draft already in hand. The caller then sets the widget key (seed() covers a lost write)."""
    ds = _dataset(dataset)
    if ds is None:
        return None
    draft["datasets"] = [d for d in draft["datasets"] if d["dataset_id"] != ds["dataset_id"]]
    draft["datasets"].append(ds)
    draft["dataset_id"] = ds["dataset_id"]
    return ds["dataset_id"]


def forget_dataset(ss: Mapping, dataset_id: str) -> None:
    draft = ss[DRAFT]
    draft["datasets"] = [d for d in draft["datasets"] if d["dataset_id"] != dataset_id]
    if draft["dataset_id"] == dataset_id:
        draft["dataset_id"] = None
    _reseed(ss)


def pending(ss: Mapping) -> bool:
    return digest(normalize(ss[DRAFT])) != ss[SERVER]["digest"]


def maybe_save(ss: Mapping, api: APIClient) -> dict:
    """At most one PUT, and only when the draft differs from what the server holds.
    Every `ss` access comes first; after the PUT only `server`/`save` change, in place."""
    save, server, draft, sid = ss[SAVE], ss[SERVER], ss[DRAFT], ss[SID]
    if save["status"] in {"conflict", "expired"}:
        return save                       # waits for the person's choice
    body = normalize(draft)
    body_digest = digest(body)
    if body_digest == server["digest"]:
        return _set(save, status="saved")
    if save["status"] == "rejected" and save.get("digest") == body_digest:
        return save                       # the same content was refused; no retry loop
    try:
        session = api.put_ui_state(sid, body, version=server["version"])
    except VersionConflict as error:
        current = error.current
        if error.code == "version_conflict" and isinstance(current, dict):
            if _work(normalize(current.get("ui_state"))) == _work(body):
                # Nothing of the person's work differs: our own write whose reply was
                # lost, or another tab that only moved to another page. This tab's
                # page is saved on the next run, at the new version.
                _remember(server, current)
                return _set(save, status="saved")
            return _set(save, status="conflict", current=current)
        return _set(save, status="error", code=error.code, message=error.message)
    except APIError as error:
        if error.status_code == 404:
            return _set(save, status="expired", message=error.message)
        if error.status_code in {413, 422}:
            return _set(save, status="rejected", code=error.code, message=error.message,
                        digest=body_digest)
        return _set(save, status="error", code=error.code, message=error.message)
    _remember(server, session)
    return _set(save, status="saved")


def load_latest(ss: Mapping) -> None:
    """Conflict choice: adopt the server's state. Dataset references are kept from both
    sides, because each points at a server record that exists either way."""
    save, server = ss[SAVE], ss[SERVER]
    current, mine = save["current"], ss[DRAFT]["datasets"]
    _remember(server, current)
    latest = copy.deepcopy(server["ui_state"])
    known = {d["dataset_id"] for d in latest["datasets"]}
    latest["datasets"] += [d for d in mine if d["dataset_id"] not in known]
    ss[DRAFT] = normalize(latest)
    _reseed(ss)
    _set(save, status="saved")


def keep_mine(ss: Mapping) -> None:
    """Conflict choice: save this tab's state over the newer version."""
    save = ss[SAVE]
    _remember(ss[SERVER], save["current"])
    _set(save, status="saved")
