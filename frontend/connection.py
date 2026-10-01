"""The one APIClient a Streamlit server process shares (httpx pools its connections).

Tests replace `APIClient` on this module and clear Streamlit's resource cache.
"""

from __future__ import annotations

import streamlit as st

from frontend.api_client import APIClient


@st.cache_resource(show_spinner=False)
def get_client() -> APIClient:
    return APIClient()
