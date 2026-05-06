from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    jira_server: str
    jira_email: str
    jira_api_token: str
    jira_project_key: str
    jira_module_field: str
    openai_api_key: str
    openai_base_url: str
    default_model: str


def _get(key: str, default: str = "") -> str:
    """Read from Streamlit secrets first (cloud deployment), then env vars, then default."""
    try:
        import streamlit as st
        val = st.secrets.get(key)
        if val:
            return str(val)
    except Exception:
        pass
    return os.getenv(key, default)


def load_settings() -> Settings:
    # Shared settings — pre-filled from secrets/env for all users.
    # Per-user credentials (jira_email, jira_api_token, openai_api_key)
    # are intentionally left blank so each user must enter their own.
    return Settings(
        jira_server=_get("JIRA_SERVER"),
        jira_email="",
        jira_api_token="",
        jira_project_key=_get("JIRA_PROJECT_KEY"),
        jira_module_field=_get("JIRA_MODULE_FIELD"),
        openai_api_key="",
        openai_base_url=_get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        default_model=_get("DEFAULT_MODEL", "gpt-4.1-mini"),
    )


def calculate_window(cutoff: date, weeks: int) -> tuple[date, date]:
    # Reporting windows are week-aligned and end on the selected cutoff date.
    start_of_week = cutoff - timedelta(days=cutoff.weekday())
    start_date = start_of_week - timedelta(weeks=max(weeks - 1, 0))
    return start_date, cutoff
