"""The adapter boundary shared by production-shaped and simulated feeds.

Nothing downstream is allowed to branch on FHIR, HL7, CSV or simulation. An
adapter returns canonical dictionaries; the integration service applies one
tenant, validation, idempotency, quarantine and provenance contract to all of
them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class AdapterHealth:
    status: str
    checked_at: datetime
    latency_ms: int | None = None
    detail: str | None = None


@dataclass(frozen=True)
class AdapterCapabilities:
    unit_level: bool = True
    aggregate_only: bool = False
    supports_push: bool = False
    max_lookback_days: int = 90

    def to_dict(self) -> dict:
        return asdict(self)


class BloodBankAdapter(Protocol):
    facility_id: str
    mode: str

    def health_check(self) -> AdapterHealth: ...

    def fetch_inventory(self, since: datetime | None = None) -> list[dict]: ...

    def fetch_demand_events(self, since: datetime | None = None) -> list[dict]: ...

    def push_transfer_notice(self, transfer: dict) -> dict: ...

    def capabilities(self) -> AdapterCapabilities: ...
