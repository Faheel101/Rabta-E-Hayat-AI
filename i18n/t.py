"""Translation helper (spec §12.13, §10.4).

    from i18n.t import t
    t("cc.kpi_shortage_alerts")
    t("footer.feeds_healthy", healthy=12, total=14)

Spec §12.13: no hardcoded user-facing string in any page file. Spec §12.1
principle 4: bilingual is a toggle, not a separate app — one click swaps every
label, every generated narrative, and the text direction.

A key missing from ur.json falls back to English rather than showing the key or a
blank. That is deliberate, not a stopgap: spec §10.4 requires clinical terms to
stay in English with an Urdu gloss, because a Pakistani blood bank officer says
"platelets". Machine-translating them would be worse than leaving them.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

I18N_DIR = Path(__file__).resolve().parent

DEFAULT_LANGUAGE = "en"
FALLBACK_LANGUAGE = "en"

LANGUAGES = {
    "en": {"code": "en", "label": "EN", "name": "English", "dir": "ltr"},
    "ur": {"code": "ur", "label": "اردو", "name": "Urdu", "dir": "rtl"},
}

_SESSION_KEY = "language"


@lru_cache(maxsize=8)
def _catalogue(language: str) -> dict:
    path = I18N_DIR / f"{language}.json"

    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _lookup(catalogue: dict, key: str):
    node = catalogue

    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None

        node = node[part]

    return node if isinstance(node, str) else None


def _in_streamlit_runtime() -> bool:
    """True only inside a real script run.

    Touching st.session_state outside one emits a warning on every call, which
    would flood test output and any CLI use of the pipeline.
    """

    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        return get_script_run_ctx() is not None
    except Exception:
        return False


def current_language() -> str:
    """Reads the language from Streamlit session state when running in the app,
    and falls back to the default outside it so services and tests can call t()."""

    if not _in_streamlit_runtime():
        return DEFAULT_LANGUAGE

    import streamlit as st

    return st.session_state.get(_SESSION_KEY, DEFAULT_LANGUAGE)


def set_language(language: str) -> None:
    if language not in LANGUAGES or not _in_streamlit_runtime():
        return

    import streamlit as st

    st.session_state[_SESSION_KEY] = language


def direction(language: str | None = None) -> str:
    return LANGUAGES.get(language or current_language(), LANGUAGES["en"])["dir"]


def is_rtl(language: str | None = None) -> bool:
    return direction(language) == "rtl"


def t(key: str, language: str | None = None, **params) -> str:
    """Translate `key`, interpolating any {placeholders} from `params`."""

    language = language or current_language()

    value = _lookup(_catalogue(language), key)

    if value is None and language != FALLBACK_LANGUAGE:
        value = _lookup(_catalogue(FALLBACK_LANGUAGE), key)

    if value is None:
        # Surface the key rather than an empty string: a visible missing key gets
        # fixed, a blank label does not.
        return f"[{key}]"

    if params:
        try:
            return value.format(**params)
        except (KeyError, IndexError):
            return value

    return value


def translation_coverage() -> dict:
    """How much of the catalogue each language actually carries.

    Surfaced in the Admin page so an untranslated build cannot be mistaken for a
    finished one.
    """

    def flatten(node, prefix=""):
        keys = set()

        for name, value in (node or {}).items():
            if name.startswith("_"):
                continue

            path = f"{prefix}{name}"

            if isinstance(value, dict):
                keys |= flatten(value, f"{path}.")
            elif isinstance(value, str):
                keys.add(path)

        return keys

    english = flatten(_catalogue("en"))
    report = {}

    for code in LANGUAGES:
        translated = flatten(_catalogue(code))
        present = len(english & translated)

        report[code] = {
            "translated": present,
            "total": len(english),
            "pct": round(100.0 * present / max(1, len(english)), 1),
            "missing": sorted(english - translated),
        }

    return report
