"""세션 활동 로그."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

import streamlit as st

LOG_KEY = "_osmu_activity_log"
LEVELS = Literal["info", "success", "warning", "error"]


def _store():
    if LOG_KEY not in st.session_state:
        st.session_state[LOG_KEY] = []
    return st.session_state[LOG_KEY]


def log_activity(level, where, message, detail=None):
    _store().append({
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "level": level, "where": where, "message": message, "detail": detail or "",
    })
    if len(_store()) > 200:
        st.session_state[LOG_KEY] = _store()[-200:]


def get_activities():
    return list(reversed(_store()))


def clear_activities():
    st.session_state[LOG_KEY] = []
