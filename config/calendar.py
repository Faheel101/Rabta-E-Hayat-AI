"""Pakistan calendar features (spec §6.3).

Ramadan and Eid are the single most important Pakistan-specific signal in this
system: donation collection collapses during Ramadan, elective surgery is
deferred and then surges post-Eid, and trauma demand spikes on Eid ul-Adha and
during Muharram processions. A model that ignores the Hijri calendar is
systematically wrong for roughly two months a year.

These flags were previously two hardcoded copies of the same 2025-2026 date
table, one here and one in the synthetic generator. Any drift between them would
have silently destroyed the signal: the generator would inject a Ramadan effect
on dates the forecaster did not flag. They are now derived from the Hijri
calendar, so they hold for any year, and there is one copy.

Observed dates in Pakistan can differ from the tabular calendar by a day when
the moon sighting differs. `calendar.year_offset_days` in config/network.yaml
applies a per-Hijri-year correction where the observed date is known.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

from hijridate import Gregorian

from core import config

MASS_EVENT_DATES = {
    value if isinstance(value, date) else date.fromisoformat(str(value))
    for value in (config.get("calendar.mass_casualty_events") or [])
}


def _offsets() -> dict[int, int]:
    raw = config.get("calendar.year_offset_days") or {}
    return {int(year): int(days) for year, days in raw.items()}


@lru_cache(maxsize=4096)
def hijri_parts(day: date) -> tuple[int, int, int]:
    """(hijri_year, hijri_month, hijri_day), with the observed-date correction."""

    try:
        converted = Gregorian(day.year, day.month, day.day).to_hijri()
    except (OverflowError, ValueError):
        return (0, 0, 0)

    offset = _offsets().get(converted.year, 0)

    if offset:
        shifted = day - timedelta(days=offset)

        try:
            converted = Gregorian(
                shifted.year, shifted.month, shifted.day
            ).to_hijri()
        except (OverflowError, ValueError):
            return (0, 0, 0)

    return (converted.year, converted.month, converted.day)


def is_ramadan(day: date) -> bool:
    _, month, _ = hijri_parts(day)
    return month == int(config.get("calendar.ramadan.hijri_month", 9))


def ramadan_day_index(day: date) -> int:
    """Day 1-30 of Ramadan, else 0. Late Ramadan differs from early Ramadan."""

    _, month, hijri_day = hijri_parts(day)

    if month != int(config.get("calendar.ramadan.hijri_month", 9)):
        return 0

    return int(hijri_day)


def is_eid_ul_fitr(day: date) -> bool:
    _, month, hijri_day = hijri_parts(day)

    start_month = int(config.get("calendar.eid_ul_fitr.hijri_month", 10))
    start_day = int(config.get("calendar.eid_ul_fitr.hijri_day", 1))
    window = int(config.get("calendar.eid_ul_fitr.window_days", 3))

    return month == start_month and start_day <= hijri_day < start_day + window


def is_eid_ul_adha(day: date) -> bool:
    _, month, hijri_day = hijri_parts(day)

    start_month = int(config.get("calendar.eid_ul_adha.hijri_month", 12))
    start_day = int(config.get("calendar.eid_ul_adha.hijri_day", 10))
    window = int(config.get("calendar.eid_ul_adha.window_days", 4))

    return month == start_month and start_day <= hijri_day < start_day + window


def is_muharram(day: date) -> bool:
    _, month, hijri_day = hijri_parts(day)

    from_day = int(config.get("calendar.muharram.from_day", 1))
    to_day = int(config.get("calendar.muharram.to_day", 10))

    return month == int(
        config.get("calendar.muharram.hijri_month", 1)
    ) and from_day <= hijri_day <= to_day


def is_ashura(day: date) -> bool:
    _, month, hijri_day = hijri_parts(day)
    return month == int(config.get("calendar.muharram.hijri_month", 1)) and hijri_day == 10


def _is_eid_window_last_day(day: date) -> bool:
    _, month, hijri_day = hijri_parts(day)

    fitr_month = int(config.get("calendar.eid_ul_fitr.hijri_month", 10))
    fitr_start = int(config.get("calendar.eid_ul_fitr.hijri_day", 1))
    fitr_window = int(config.get("calendar.eid_ul_fitr.window_days", 3))

    if month == fitr_month and hijri_day == fitr_start + fitr_window - 1:
        return True

    adha_month = int(config.get("calendar.eid_ul_adha.hijri_month", 12))
    adha_start = int(config.get("calendar.eid_ul_adha.hijri_day", 10))
    adha_window = int(config.get("calendar.eid_ul_adha.window_days", 4))

    return month == adha_month and hijri_day == adha_start + adha_window - 1


def is_post_eid_backlog(day: date) -> bool:
    """The deferred-elective-surgery catch-up window after either Eid."""

    backlog_days = int(config.get("calendar.post_eid_backlog_days", 10))

    for offset in range(1, backlog_days + 1):
        if _is_eid_window_last_day(day - timedelta(days=offset)):
            return True

    return False


def is_dengue_season(day: date) -> bool:
    return day.month in (8, 9, 10, 11)


def is_monsoon(day: date) -> bool:
    return day.month in (7, 8, 9)


def is_heat_season(day: date) -> bool:
    return day.month in (5, 6, 7)


@lru_cache(maxsize=4096)
def get_calendar_flags(day: date) -> dict:
    """All calendar features for one day. Integer-valued for direct model use."""

    hijri_year, hijri_month, hijri_day = hijri_parts(day)

    return {
        "ramadan": int(is_ramadan(day)),
        "ramadan_day": ramadan_day_index(day),
        "eid_fitr": int(is_eid_ul_fitr(day)),
        "eid_adha": int(is_eid_ul_adha(day)),
        "post_eid": int(is_post_eid_backlog(day)),
        "muharram": int(is_muharram(day)),
        "ashura": int(is_ashura(day)),
        "hijri_month": int(hijri_month),
        "hijri_day": int(hijri_day),
        "dengue_season": int(is_dengue_season(day)),
        "monsoon": int(is_monsoon(day)),
        "heat_season": int(is_heat_season(day)),
        "mass_event": int(day in MASS_EVENT_DATES),
    }


CALENDAR_FEATURES = [
    "ramadan",
    "ramadan_day",
    "eid_fitr",
    "eid_adha",
    "post_eid",
    "muharram",
    "ashura",
    "hijri_month",
    "hijri_day",
    "dengue_season",
    "monsoon",
    "heat_season",
    "mass_event",
]
