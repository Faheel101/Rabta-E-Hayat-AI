"""HL7 v2 file-drop preview adapter.

The MVP accepts one message per payload. Standard MSH message types are checked,
then a small Rabta implementation-guide segment carries the canonical fields:

* ZBU for BPS/BRP/BTS/BRT blood-product messages.
* ZRH for ORM blood-transfusion requests.

This is a documented, deterministic stub—not a claim of plug-and-play support
for every vendor dialect. A real connection supplies a vendor mapping before it
can be enabled.
"""

from __future__ import annotations

from dataclasses import dataclass


class Hl7AdapterError(ValueError):
    pass


@dataclass(frozen=True)
class Hl7Preview:
    data_type: str
    rows: list[dict]
    message_type: str


def _segments(payload: str) -> list[list[str]]:
    normalized = payload.replace("\r\n", "\r").replace("\n", "\r")
    return [line.split("|") for line in normalized.split("\r") if line.strip()]


def parse_message(payload: str) -> Hl7Preview:
    if not isinstance(payload, str) or not payload.strip():
        raise Hl7AdapterError("HL7 payload is empty.")

    segments = _segments(payload)
    msh = next((row for row in segments if row[0] == "MSH"), None)
    if msh is None or len(msh) < 9:
        raise Hl7AdapterError("HL7 message is missing a valid MSH segment.")

    message_type = msh[8].replace("^", "_").upper()
    allowed_prefixes = ("ORM_", "BPS_", "BRP_", "BTS_", "BRT_", "ORU_")
    if not message_type.startswith(allowed_prefixes):
        raise Hl7AdapterError(f"Unsupported HL7 message type {msh[8]}.")

    inventory_rows = [row for row in segments if row[0] == "ZBU"]
    demand_rows = [row for row in segments if row[0] == "ZRH"]

    if inventory_rows:
        parsed = []
        for row in inventory_rows:
            if len(row) < 12:
                raise Hl7AdapterError("ZBU requires 11 fields after the segment name.")
            parsed.append(
                {
                    "source_system_ref": row[1],
                    "din": row[2],
                    "component_code": row[3],
                    "blood_group": row[4],
                    "collected_at": row[5],
                    "expires_at": row[6],
                    "status": row[7],
                    "screening_status": row[8],
                    "volume_ml": row[9],
                    "is_leucodepleted": row[10],
                    "is_irradiated": row[11],
                }
            )
        return Hl7Preview("INVENTORY", parsed, message_type)

    if demand_rows:
        parsed = []
        for row in demand_rows:
            if len(row) < 10:
                raise Hl7AdapterError("ZRH requires 9 fields after the segment name.")
            parsed.append(
                {
                    "source_system_ref": row[1],
                    "requested_at": row[2],
                    "component_code": row[3],
                    "blood_group": row[4],
                    "units_requested": row[5],
                    "units_issued": row[6],
                    "urgency": row[7],
                    "clinical_context": row[8],
                    "outcome": row[9],
                }
            )
        return Hl7Preview("DEMAND", parsed, message_type)

    raise Hl7AdapterError(
        "Supported message type received, but no ZBU or ZRH mapping segment was present."
    )
