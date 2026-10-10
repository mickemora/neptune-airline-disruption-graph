"""Disruption Ops Assistant: chat with the agent, see the cascade it analyzes.

    streamlit run ui/app.py

Left: chat with the operations agent (hosted on AgentCore, or local).
Right: a live dashboard for the disruption the agent last simulated: headline numbers, the
cascade drawing, crew duty, missed connections and rebooking options. Before the first
analysis it shows the route network.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backend  # noqa: E402

EXAMPLES = [
    "WN3553 is going to be 3 hours late. What's the damage and what should I do first?",
    "What if it's only 90 minutes instead?",
    "How late can WN3553 get before any passenger misconnects?",
    "Which connections through DAL carry the most passengers?",
]

st.set_page_config(page_title="Disruption Ops Assistant", page_icon=":material/flight_takeoff:",
                   layout="wide")
st.markdown("""
<style>
  .block-container { padding-top: 1.6rem; padding-bottom: 1rem; }
  [data-testid="stMetricValue"] { font-size: 2rem; }
  .tool-badge { display:inline-block; margin:2px 4px 0 0; padding:1px 8px; border-radius:10px;
                background:#EEF2FF; color:#1E3A8A; font-size:0.75rem; }
</style>
""", unsafe_allow_html=True)


# --------------------------------------------------------------------------- state

def _reset(mode: str) -> None:
    st.session_state.mode = mode
    st.session_state.messages = []      # [{"role", "content", "tools"}]
    st.session_state.analyses = []      # [{"flightId", "delayMin"}], newest last
    st.session_state.agent = None
    st.session_state.pending = None


if "mode" not in st.session_state:
    _reset(backend.HOSTED)


@st.cache_data(show_spinner=False, ttl=3600)
def _impact(flight_id: str, delay_min: int) -> dict:
    return backend.impact_view(flight_id, delay_min)


@st.cache_data(show_spinner=False, ttl=3600)
def _network() -> dict:
    return backend.network_view()


def _ask(question: str) -> None:
    st.session_state.messages.append({"role": "user", "content": question})
    try:
        if st.session_state.agent is None:
            st.session_state.agent = backend.make_agent(st.session_state.mode)
        reply = st.session_state.agent.ask(question)
    except Exception as exc:  # show AWS/Bedrock errors in the chat instead of crashing
        reply = {"error": f"{type(exc).__name__}: {exc}"}
    if "error" in reply:
        st.session_state.messages.append(
            {"role": "assistant", "content": f"**Something went wrong:** {reply['error']}",
             "tools": []})
        return
    st.session_state.messages.append(
        {"role": "assistant", "content": reply.get("result", ""),
         "tools": reply.get("toolsUsed", [])})
    for a in reply.get("analyses", []):
        if a in st.session_state.analyses:
            st.session_state.analyses.remove(a)
        st.session_state.analyses.append(a)


# --------------------------------------------------------------------------- sidebar

with st.sidebar:
    st.subheader("Disruption Ops Assistant")
    mode = st.radio("Agent", [backend.HOSTED, backend.LOCAL],
                    index=[backend.HOSTED, backend.LOCAL].index(st.session_state.mode),
                    help="Hosted uses the agent on Amazon Bedrock AgentCore. Local runs the "
                         "same agent on this laptop.")
    if mode != st.session_state.mode:
        _reset(mode)
    if st.button("New conversation", icon=":material/refresh:", use_container_width=True):
        _reset(st.session_state.mode)
    st.markdown("**Try asking**")
    for i, example in enumerate(EXAMPLES):
        if st.button(example, key=f"ex{i}", use_container_width=True):
            st.session_state.pending = example
    st.caption("Synthetic one-day schedule for a Southwest-style network hubbed at Dallas Love "
               "Field. Times are hub (Central) time. Not affiliated with any airline.")


# --------------------------------------------------------------------------- layout

st.markdown("### Disruption Ops Assistant")
st.caption("Describe a delay in plain English. The agent analyzes the knock-on effects across "
           "aircraft, crews and passengers, and the dashboard shows the cascade.")
chat_col, dash_col = st.columns([5, 7], gap="large")

with chat_col:
    history = st.container(height=600, border=True)
    question = st.chat_input("e.g. WN3553 is 3 hours late. What should I do?")
    question = question or st.session_state.pending
    st.session_state.pending = None
    if question:
        with history, st.spinner("Analyzing the network…"):
            _ask(question)
    with history:
        if not st.session_state.messages:
            st.info("Ask about a delayed flight, or pick an example on the left.",
                    icon=":material/chat:")
        for m in st.session_state.messages:
            with st.chat_message(m["role"]):
                st.markdown(m["content"])
                if m.get("tools"):
                    st.markdown("".join(f'<span class="tool-badge">{t}</span>'
                                        for t in m["tools"]), unsafe_allow_html=True)

with dash_col:
    if st.session_state.analyses:
        options = list(reversed(st.session_state.analyses))
        chosen = st.selectbox(
            "Scenario", options, index=0,
            format_func=lambda a: f'{a["flightId"]} delayed {a["delayMin"]} min',
            label_visibility="collapsed")
        try:
            with st.spinner("Loading the cascade…"):
                view = _impact(chosen["flightId"], chosen["delayMin"])
        except Exception as exc:
            st.error(f"Couldn't load this scenario: {exc}")
        else:
            st.markdown(f"#### {view['title']}")
            st.caption(f"Aircraft {view['aircraft']}. Hover over the drawing for details; "
                       "drag to pan, scroll to zoom.")
            for col, (label, value, note) in zip(st.columns(4), view["metrics"]):
                col.metric(label, value)
                col.caption(note)
            components.html(view["graph_html"], height=470, scrolling=False)
            crew_tab, missed_tab, rebook_tab = st.tabs(
                [f"Crew duty ({len(view['crew'])})", f"Missed connections ({len(view['missed'])})",
                 f"Rebooking options ({len(view['rebooking'])})"])
            with crew_tab:
                st.dataframe(view["crew"], hide_index=True, use_container_width=True)
            with missed_tab:
                st.dataframe(view["missed"], hide_index=True, use_container_width=True)
            with rebook_tab:
                st.dataframe(view["rebooking"], hide_index=True, use_container_width=True)
    else:
        st.markdown("#### Route network")
        st.caption("The dashboard switches to the disruption cascade as soon as the agent "
                   "analyzes a delay.")
        try:
            with st.spinner("Loading the network…"):
                net = _network()
        except Exception as exc:
            st.warning(f"Couldn't reach the graph API ({exc}). Is the stack deployed and your "
                       "AWS profile set?")
        else:
            for col, (value, label) in zip(st.columns(4), net["stats"]):
                col.metric(label.capitalize(), value)
            components.html(net["graph_html"], height=520, scrolling=False)
