"""Run from the repository root: streamlit run frontend/app.py.

Router: read the sid, hydrate once per browser session, restore the saved page,
run the page, then save the draft (at most one PUT). See docs/steps/F2.md.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from frontend import connection, state  # noqa: E402
from frontend.api_client import APIError  # noqa: E402
from frontend.components import shell  # noqa: E402

st.set_page_config(page_title="Analytics agent", page_icon="📊", layout="wide")

TITLES = {"session": "Session", "data": "Data", "ask": "Ask"}
PAGES = {
    "session": st.Page("views/session.py", title=TITLES["session"], default=True),
    "data": st.Page("views/data.py", title=TITLES["data"], url_path="data"),
    "ask": st.Page("views/ask.py", title=TITLES["ask"], url_path="ask"),
}
current = st.navigation(list(PAGES.values()), position="hidden")
page_id = next(pid for pid, page in PAGES.items() if page.url_path == current.url_path)
ss = st.session_state


def main() -> None:
    raw_sid = st.query_params.get("sid")
    if raw_sid is None:
        shell.landing()
        return
    sid = state.parse_sid(raw_sid)
    if sid is None:
        shell.invalid_sid()
        return
    try:
        api = connection.get_client()
    except APIError as error:
        shell.unreachable(error)
        return
    if state.needs_hydration(ss, sid):
        error = state.hydrate(ss, api, sid)
        if error is not None:
            if error.status_code == 404:
                shell.expired(error.message)
            else:
                shell.unreachable(error)
            return
    target = state.take_restore_target(ss, page_id)
    if target is not None:
        st.switch_page(PAGES[target], query_params={"sid": sid})
    state.set_page(ss, page_id)

    status_slot = shell.sidebar(PAGES, TITLES, sid)
    banner = st.container()
    current.run()
    state.maybe_save(ss, api)
    shell.render_banner(banner, ss)
    shell.render_status(status_slot, ss)


main()
