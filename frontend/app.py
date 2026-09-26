"""Run from the repository root: streamlit run frontend/app.py."""

import streamlit as st

st.set_page_config(page_title="Analytics agent", page_icon="📊", layout="wide")
st.navigation([st.Page("pages/start.py", title="Start", default=True)]).run()
