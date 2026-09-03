"""Shared plumbing for the read services.

Spec §12.13: `@st.cache_data` on read queries, `@st.cache_resource` for the
engine and loaded models, and never a query inside a loop. Cached functions must
return plain data — DataFrames, dicts, lists — because Streamlit pickles the
result. Returning SQLAlchemy ORM instances would make the cache silently useless
and hand pages detached objects.
"""

from __future__ import annotations

from datetime import datetime, time, timezone

import pandas as pd
import streamlit as st

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
    """`st.cache_data` that degrades to a plain function outside Streamlit."""

    def decorate(function):
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

    try:
        st.cache_data.clear()
    except Exception:  # pragma: no cover
        pass
