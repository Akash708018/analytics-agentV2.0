"""Session hydration, one versioned save per change, and explicit conflict choices.

The backend session (GET/PUT /sessions/{sid}) is the only durable copy of the
person's work; st.session_state is a cache. F0 measured why: refresh resets it,
and navigation deletes the state of widgets that leave the page. So widgets are
bound to a path in the draft (`bind`), copy edits back into it, and only an
explicit allowlist of draft fields is ever sent. See docs/steps/F2.md, F3.md.

Every function takes the session-state mapping `ss` so it can be tested with a dict.

Streamlit may stop a run at ANY session-state read or write, when a newer rerun is
waiting (SafeSessionState's yield callback). F2's browser check caught a save cut in
half that way: the PUT landed, the new version was never recorded, and the next run
reported a conflict with itself. So after a network call returns, code touches only
plain objects already in hand (the draft, `work(ss)`), never `ss` itself; widget
resets are queued in `work(ss)["reseed"]` and done by the router at the next run.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from collections.abc import Callable, MutableMapping, Sequence
from typing import Any

from frontend.api_client import APIClient, APIError, VersionConflict

SCHEMA = 2
PAGES = ("session", "data", "clean", "domain", "contract", "metrics", "keywords", "tools",
         "results", "ask")
DEFAULT_PAGE = "session"
LABEL_MAX = 120
TEXT_MAX = 2000
NAME_MAX = 200
DATASETS_MAX = 50
# A column's role; the key is a separate list, because a key usually includes the date and
# dimensions (F3 browser check: key [campaign] alone was refused for "one row per campaign
# per day"; [date, campaign] passed).
ROLES = ("date", "measure", "dimension", "ignore")
_DATASET_ID = re.compile(r"^ds_[A-Za-z0-9_-]{1,64}$")
_KNOWN = {"schema", "page", "label", "dataset_id", "datasets", "drafts"}

# Session-state keys owned by this module.
SID = "_sid"            # the sid the cache below belongs to
SERVER = "_server"      # last state read from / written to the backend
DRAFT = "_draft"        # what the person has now
SAVE = "_save"          # {"status": saved|error|rejected|conflict|expired, ...}
RESTORE = "_restore"    # True until the first run after hydration has picked the page
WORK = "_work"          # browser-only: server caches, action results, queued widget resets

Mapping = MutableMapping[str, Any]
MISSING = object()


def parse_sid(raw: Any) -> str | None:
    """A canonical lowercase UUID, or None. Nothing else is sent to the backend."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = uuid.UUID(raw)
    except ValueError:
        return None
    return str(parsed) if str(parsed) == raw.lower() else None


def is_dataset_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_DATASET_ID.match(value))


# --- the allowlist -------------------------------------------------------------------------

def _text(value: Any, limit: int = TEXT_MAX) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v[:NAME_MAX] for v in value if isinstance(v, str)]


def _str_map(value: Any, limit: int = TEXT_MAX) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {k[:NAME_MAX]: v[:limit] for k, v in value.items()
            if isinstance(k, str) and isinstance(v, str)}


def _contract(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {}
    if (grain := _text(raw.get("grain"))) is not None:
        out["grain"] = grain
    if isinstance(raw.get("key"), list):
        out["key"] = _names(raw["key"])
    roles = raw.get("roles") if isinstance(raw.get("roles"), dict) else {}
    if roles:
        out["roles"] = {k[:NAME_MAX]: v for k, v in roles.items()
                        if isinstance(k, str) and v in ROLES}
    for field in ("aggregations", "definitions"):
        if raw.get(field):
            out[field] = _str_map(raw[field])
    per = raw.get("per") if isinstance(raw.get("per"), dict) else {}
    if per:
        out["per"] = {k[:NAME_MAX]: _names(v) for k, v in per.items() if isinstance(k, str)}
    for field in ("window_start", "window_end"):
        if isinstance(raw.get(field), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw[field]):
            out[field] = raw[field]
    if (caveats := _text(raw.get("caveats"))) is not None:
        out["caveats"] = caveats
    return out


def _tool_params(raw: Any) -> dict:
    """A tool's typed params: scalars only, plus `bindings` {concept: column}."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        if k == "bindings":
            if b := _str_map(v, NAME_MAX):
                out[k] = b
        elif isinstance(v, str):
            out[k[:NAME_MAX]] = v[:500]
        elif isinstance(v, (int, float)) and not isinstance(v, bool) or isinstance(v, bool):
            out[k[:NAME_MAX]] = v
    return out


def _keywords(raw: Any) -> dict:
    """Keyword groups (F6): the text column chosen, and ticked group ids. Never keywords."""
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {}
    if isinstance(raw.get("column"), str):
        out["column"] = raw["column"][:NAME_MAX]
    ticks = raw.get("ticks") if isinstance(raw.get("ticks"), dict) else {}
    if ticks := {k[:NAME_MAX]: True for k, v in ticks.items() if isinstance(k, str) and v is True}:
        out["ticks"] = ticks
    return out


def _dataset_draft(raw: Any) -> dict:
    """One dataset's half-done preparation. Only these fields, only these types."""
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {}
    clean = raw.get("clean") if isinstance(raw.get("clean"), dict) else {}
    if ticked := {k[:NAME_MAX]: True for k, v in clean.items() if isinstance(k, str) and v is True}:
        out["clean"] = ticked
    domains = raw.get("domains") if isinstance(raw.get("domains"), dict) else {}
    if domains := {k[:NAME_MAX]: v for k, v in domains.items()
                   if isinstance(k, str) and isinstance(v, bool)}:
        out["domains"] = domains
    if contract := _contract(raw.get("contract")):
        out["contract"] = contract
    if forks := _str_map(raw.get("forks"), NAME_MAX):
        out["forks"] = forks
    rules = raw.get("rules") if isinstance(raw.get("rules"), dict) else {}
    if rules := {k[:NAME_MAX]: v for k, v in rules.items()
                 if isinstance(k, str) and isinstance(v, bool)}:
        out["rules"] = rules
    if isinstance(raw.get("tool"), str):
        out["tool"] = raw["tool"][:NAME_MAX]
    params = raw.get("params") if isinstance(raw.get("params"), dict) else {}
    if params := {t[:NAME_MAX]: p for t, v in params.items()
                  if isinstance(t, str) and (p := _tool_params(v))}:
        out["params"] = params
    bindings = raw.get("bindings") if isinstance(raw.get("bindings"), dict) else {}
    if bindings := {t[:NAME_MAX]: b for t, v in bindings.items()
                    if isinstance(t, str) and (b := _str_map(v, NAME_MAX))}:
        out["bindings"] = bindings
    if keywords := _keywords(raw.get("keywords")):
        out["keywords"] = keywords
    if isinstance(raw.get("result"), str):          # F8: the stored result chosen on Results
        out["result"] = raw["result"][:NAME_MAX]
    version = raw.get("confirmed_version")
    if isinstance(version, int) and not isinstance(version, bool):
        out["confirmed_version"] = version
    return out


def _dataset_ids(raw: Any) -> list[str]:
    """Schema 2 holds ids; schema 1 (F2) held {dataset_id, name, rows, columns} objects,
    which the backend's data-row guard refuses at 20 (C8). Both read as ids."""
    ids: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        value = item.get("dataset_id") if isinstance(item, dict) else item
        if is_dataset_id(value) and value not in ids:
            ids.append(value)
    return ids[-DATASETS_MAX:]


def normalize(ui_state: Any) -> dict:
    """The allowlisted shape. Top-level keys this version does not know are kept."""
    raw = ui_state if isinstance(ui_state, dict) else {}
    ids = _dataset_ids(raw.get("datasets"))
    drafts_raw = raw.get("drafts") if isinstance(raw.get("drafts"), dict) else {}
    drafts = {ds: d for ds in ids if (d := _dataset_draft(drafts_raw.get(ds)))}
    label = raw.get("label")
    out = {k: copy.deepcopy(v) for k, v in raw.items() if k not in _KNOWN}
    out.update({
        "schema": SCHEMA,
        "page": raw.get("page") if raw.get("page") in PAGES else DEFAULT_PAGE,
        "label": label[:LABEL_MAX] if isinstance(label, str) else "",
        "dataset_id": raw.get("dataset_id") if raw.get("dataset_id") in ids else None,
        "datasets": ids,
        "drafts": drafts,
    })
    return out


def digest(ui_state: dict) -> str:
    blob = json.dumps(ui_state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def _work(ui_state: dict) -> str:
    """The person's work: everything but which page a tab last showed."""
    return digest({k: v for k, v in ui_state.items() if k != "page"})


# --- paths into the draft --------------------------------------------------------------

def get_path(tree: dict, path: Sequence[str], default: Any = None) -> Any:
    node: Any = tree
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_path(tree: dict, path: Sequence[str], value: Any) -> None:
    node = tree
    for part in path[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = node[part] = {}
        node = nxt
    node[path[-1]] = value


def drop_path(tree: dict, path: Sequence[str]) -> None:
    parent = get_path(tree, path[:-1], None)
    if isinstance(parent, dict):
        parent.pop(path[-1], None)


# --- browser-only work area ------------------------------------------------------------

def work(ss: Mapping) -> dict:
    """Caches of server reads, results of actions, and queued widget resets. Held objects:
    safe to change after a network call."""
    if WORK not in ss:
        ss[WORK] = {"cache": {}, "results": {}, "reseed": []}
    return ss[WORK]


def apply_reseeds(ss: Mapping) -> None:
    """Router, at the start of a run: drop widget values whose drafts were reset."""
    held = work(ss)
    prefixes, held["reseed"] = list(held["reseed"]), []
    if prefixes:
        for key in [k for k in ss if isinstance(k, str) and k.startswith(tuple(prefixes))]:
            del ss[key]


def _reseed_all(ss: Mapping) -> None:
    for key in [k for k in ss if isinstance(k, str) and k.startswith("ui.")]:
        del ss[key]


# --- widgets ---------------------------------------------------------------------------

def _same(v: Any) -> Any:
    return v


def bind(ss: Mapping, key: str, path: Sequence[str], default: Any = None, *,
         to_widget: Callable[[Any], Any] = _same,
         from_widget: Callable[[Any], Any] = _same) -> None:
    """Before the widget is created: give it the draft's value (or `default` when the
    draft has none). If it already holds a different value, that is an edit whose
    callback was cut short: keep the edit. Defaults that change under a widget (a new
    proposal) are reset by queueing a reseed, never by this.

    The value is set on EVERY run, not only the first: the browser shows a widget's value
    only when the server sends it, and a widget the browser rebuilt while the server kept
    its key (a run cut short by a click) showed its empty default over a saved value (C13)."""
    draft = ss[DRAFT]
    stored = get_path(draft, path, MISSING)
    value = to_widget(default if stored is MISSING else stored)
    if key in ss and ss[key] != value:
        on_change(ss, key, path, from_widget)
        value = ss[key]
    ss[key] = value


def on_change(ss: Mapping, key: str, path: Sequence[str],
              from_widget: Callable[[Any], Any] = _same) -> None:
    set_path(ss[DRAFT], path, from_widget(ss[key]))


# --- hydration, pages, datasets --------------------------------------------------------

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
    ss[WORK] = {"cache": {}, "results": {}, "reseed": []}
    _reseed_all(ss)
    return None


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
    """Record an uploaded dataset's id as the active one, in the draft already in hand.
    The caller then sets the widget key (bind() covers a lost write)."""
    dataset_id = dataset.get("dataset_id") if isinstance(dataset, dict) else None
    if not is_dataset_id(dataset_id):
        return None
    draft["datasets"] = [d for d in draft["datasets"] if d != dataset_id] + [dataset_id]
    draft["dataset_id"] = dataset_id
    return dataset_id


def forget_dataset(ss: Mapping, dataset_id: str) -> None:
    draft = ss[DRAFT]
    draft["datasets"] = [d for d in draft["datasets"] if d != dataset_id]
    draft["drafts"].pop(dataset_id, None)
    if draft["dataset_id"] == dataset_id:
        draft["dataset_id"] = None
    _reseed_all(ss)


def dataset_draft(draft: dict, dataset_id: str) -> dict:
    """This dataset's draft (created on first use), in the draft already in hand."""
    return draft.setdefault("drafts", {}).setdefault(dataset_id, {})


# --- saving ----------------------------------------------------------------------------

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
    """Conflict choice: adopt the server's state. Dataset references, and drafts for
    datasets the other side has no draft for, are kept from this tab: each points at a
    server record that exists either way, and nothing the other side wrote is replaced."""
    save, server, mine = ss[SAVE], ss[SERVER], ss[DRAFT]
    _remember(server, save["current"])
    latest = copy.deepcopy(server["ui_state"])
    latest["datasets"] += [d for d in mine["datasets"] if d not in latest["datasets"]]
    for dataset_id, draft in mine.get("drafts", {}).items():
        latest["drafts"].setdefault(dataset_id, copy.deepcopy(draft))
    ss[DRAFT] = normalize(latest)
    _reseed_all(ss)
    _set(save, status="saved")


def keep_mine(ss: Mapping) -> None:
    """Conflict choice: save this tab's state over the newer version."""
    save = ss[SAVE]
    _remember(ss[SERVER], save["current"])
    _set(save, status="saved")
