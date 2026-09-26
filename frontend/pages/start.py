"""F0 landing screen; guided inputs arrive after session persistence."""

import streamlit as st

st.title("Analytics agent")
st.write("Understand your digital marketing data, with a source for every figure.")
st.info(
    "The frontend is being connected to the analytics service. "
    "Upload and analysis will be available when session saving is ready."
)
st.caption("Your work will be saved with your session, including approvals and questions.")
