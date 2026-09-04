"""Shared plumbing for the read services.

The current product is served by FastAPI. Streamlit caching remains an optional
compatibility layer for the original prototype, but it is not a production
runtime dependency. Cached functions return plain data rather than ORM objects.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
import os

import pandas as pd

st = None
if os.getenv("RABTA_ENABLE_STREAMLIT_CACHE", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}:
    try:  # Optional compatibility for the retired Streamlit prototype.
        import streamlit as st
    except ImportError:  # pragma: no cover - lean web deployment
        pass

from core.clock import DEMO_DATETIME  # noqa: F401  (re-exported for callers)
from db.session import SessionLocal

CACHE_TTL = 300

RISK_ORDER = ["CRITICAL", "WARNING", "WATCH", "SAFE"]
TIER_ORDER = ["ACT_NOW", "WATCH", "UNRESCUABLE", "NOT_TRANSFERABLE", "SAFE"]


def read_sql(statement) -> pd.DataFrame:
    """Run a select and return a DataFrame, with the session always closed."""

    session = SessionLocal()

    try:
        return pd.read_sql(statement, session.connection())
    finally:
        session.close()


def scalar(statement):
    session = SessionLocal()

    try:
        return session.scalar(statement)
    finally:
        session.close()


def cached(ttl: int = CACHE_TTL):
    """Use Streamlit caching when available, otherwise return the function."""

    def decorate(function):
        if st is None:
            return function

        try:
            return st.cache_data(ttl=ttl, show_spinner=False)(function)
        except Exception:  # pragma: no cover - non-Streamlit contexts
            return function

    return decorate


def risk_sort_key(bucket: str) -> int:
    try:
        return RISK_ORDER.index(bucket)
    except ValueError:
        return len(RISK_ORDER)


def tier_sort_key(tier: str) -> int:
    try:
        return TIER_ORDER.index(tier)
    except ValueError:
        return len(TIER_ORDER)


def clear_caches() -> None:
    """Called after any write, so the next rerun reads the new state."""

    if st is None:
        return

    try:
        st.cache_data.clear()
    except Exception:  # pragma: no cover
        pass
