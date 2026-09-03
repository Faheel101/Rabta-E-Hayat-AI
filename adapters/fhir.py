"""FHIR R4 preview adapter for the Rabta MVP implementation guide.

Live hospital connectivity is explicitly outside the hackathon boundary. This
parser is intentionally useful, however: it accepts a transaction/search Bundle
containing BiologicallyDerivedProduct and blood-transfusion ServiceRequest
resources and maps them to the same canonical rows as CSV. Unsupported resources
are reported, never silently ignored.
"""

from __future__ import annotations

from dataclasses import dataclass


class FhirAdapterError(ValueError):
    pass


@dataclass(frozen=True)
class FhirPreview:
    inventory: list[dict]
    demand: list[dict]
    unsupported: list[dict]


def _coding_code(value) -> str | None:
    if isinstance(value, dict):
        for coding in value.get("coding") or []:
            if coding.get("code"):
                return str(coding["code"])
        if value.get("text"):
            return str(value["text"])
    return None


def _identifier(resource: dict) -> str | None:
    for item in resource.get("identifier") or []:
        if item.get("value"):
            return str(item["value"])
    return str(resource.get("id")) if resource.get("id") else None


def _extension(resource: dict, suffix: str):
    for item in resource.get("extension") or []:
        if str(item.get("url", "")).rstrip("/").endswith(suffix):
            for key, value in item.items():
                if key.startswith("value"):
                    if isinstance(value, dict):
                        return value.get("code") or value.get("value") or value.get("display")
                    return value
    return None


def _quantity(resource: dict) -> int:
    quantity = resource.get("quantityQuantity") or {}
    if quantity.get("value") is not None:
        return int(float(quantity["value"]))
    return 1


def parse_bundle(payload: dict) -> FhirPreview:
    if not isinstance(payload, dict):
        raise FhirAdapterError("FHIR payload must be a JSON object.")
    if payload.get("resourceType") != "Bundle":
        raise FhirAdapterError("FHIR payload must be a Bundle resource.")

    inventory: list[dict] = []
    demand: list[dict] = []
    unsupported: list[dict] = []

    for index, entry in enumerate(payload.get("entry") or [], start=1):
        resource = entry.get("resource") or {}
        resource_type = resource.get("resourceType")

        if resource_type == "BiologicallyDerivedProduct":
            identifier = _identifier(resource)
            collection = resource.get("collection") or {}
            product_code = (
                _coding_code(resource.get("productCode"))
                or _extension(resource, "component-code")
                or _extension(resource, "component")
            )
            blood_group = _extension(resource, "blood-group")
            inventory.append(
                {
                    "source_system_ref": identifier,
                    "din": identifier,
                    "component_code": product_code,
                    "blood_group": blood_group,
                    "collected_at": collection.get("collectedDateTime"),
                    "expires_at": resource.get("expirationDate"),
                    "status": str(resource.get("status") or "AVAILABLE").upper(),
                    "screening_status": str(
                        _extension(resource, "screening-status") or "PASSED"
                    ).upper(),
                    "volume_ml": _extension(resource, "volume-ml") or 350,
                    "is_leucodepleted": bool(_extension(resource, "leucodepleted") or False),
                    "is_irradiated": bool(_extension(resource, "irradiated") or False),
                }
            )
            continue

        if resource_type == "ServiceRequest":
            quantity = _quantity(resource)
            demand.append(
                {
                    "source_system_ref": _identifier(resource),
                    "requested_at": resource.get("authoredOn")
                    or (resource.get("occurrenceDateTime")),
                    "component_code": _coding_code(resource.get("code"))
                    or _extension(resource, "component-code"),
                    "blood_group": _extension(resource, "recipient-blood-group"),
                    "units_requested": quantity,
                    "units_issued": int(_extension(resource, "units-issued") or 0),
                    "urgency": str(resource.get("priority") or "ROUTINE").upper(),
                    "clinical_context": str(
                        _coding_code((resource.get("reasonCode") or [{}])[0]) or "OTHER"
                    ).upper(),
                    "outcome": str(_extension(resource, "fulfilment-outcome") or "UNFULFILLED").upper(),
                }
            )
            continue

        unsupported.append(
            {
                "entry": index,
                "resource_type": resource_type or "UNKNOWN",
                "id": resource.get("id"),
            }
        )

    if not inventory and not demand:
        raise FhirAdapterError(
            "Bundle contains no supported BiologicallyDerivedProduct or ServiceRequest resources."
        )

    return FhirPreview(inventory=inventory, demand=demand, unsupported=unsupported)
