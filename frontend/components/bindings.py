"""'Name the column for a concept': when the engine says "no column is bound to 'X'", the
person can say which column holds X. Saved in the draft at `path` as {concept: column};
the Metrics page sends it as MetricApprove.bindings, the Tools page as params.bindings
(backend/tools/runner.py `column_for` takes it as an override). Nothing is inferred here.
"""

from __future__ import annotations

from collections.abc import MutableMapping, Sequence
from typing import Any

import streamlit as st

from frontend import state


def _add(ss: MutableMapping[str, Any], prefix: str, path: tuple[str, ...]) -> None:
    concept = (ss.get(f"{prefix}.concept") or "").strip()
    column = ss.get(f"{prefix}.column")
    if concept and column:
        state.set_path(ss[state.DRAFT], (*path, concept), column)


def _drop(ss: MutableMapping[str, Any], path: tuple[str, ...], concept: str) -> None:
    state.drop_path(ss[state.DRAFT], (*path, concept))


def binding_form(ss: MutableMapping[str, Any], prefix: str, path: tuple[str, ...],
                 columns: Sequence[str]) -> None:
    current = state.get_path(ss[state.DRAFT], path, {})
    title = "Name the column for a concept" + (f" ({len(current)})" if current else "")
    with st.expander(title):
        for concept, column in current.items():
            left, right = st.columns([3, 1])
            left.caption(f"{concept} → {column}")
            right.button("Remove", key=f"{prefix}.drop.{concept}", on_click=_drop,
                         args=(ss, path, concept))
        left, right = st.columns(2)
        left.text_input("Concept the engine named", key=f"{prefix}.concept",
                        placeholder="e.g. conv_value")
        right.selectbox("Column", list(columns), index=None, placeholder="choose…",
                        key=f"{prefix}.column")
        st.button("Add", key=f"{prefix}.add", on_click=_add, args=(ss, prefix, path))
