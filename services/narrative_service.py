"""Deterministic narrative templates (spec §10.3).

Spec §10.3 requires a template fallback for every narrative type, because the
system must remain fully functional with the LLM endpoint unavailable
(acceptance criterion 11). The templates are therefore the primary path, not a
degraded one, and the LLM layer will render the same fact dictionary.

The hard architectural rule from §10.1 applies here too: every number in the
prose traces to a field in the fact dictionary. Nothing is computed while
writing the sentence.
"""

from __future__ import annotations

import pandas as pd

from i18n.t import t
from services.common import DEMO_DATETIME


def num(value) -> str:
    """Thousands-separated, Western Arabic numerals (spec §10.4)."""

    if value is None:
        return "—"

    try:
        return f"{int(round(float(value))):,}"
    except (TypeError, ValueError):
        return "—"


def travel_label(minutes) -> str:
    if minutes is None:
        return "—"

    minutes = int(minutes)

    if minutes < 90:
        return f"{minutes} min"

    return f"{minutes // 60}h {minutes % 60:02d}m"


def briefing_facts(
    *,
    facility_name: str,
    summary: dict,
    cover: pd.DataFrame,
    rescue: pd.DataFrame,
    transfers: pd.DataFrame,
) -> dict:
    """Everything the briefing may mention, resolved before any prose exists."""

    facts: dict = {
        "facility_name": facility_name,
        "as_of": DEMO_DATETIME,
        "shortage_alerts": summary.get("shortage_alerts", 0),
        "shortage_alerts_critical": summary.get("shortage_alerts_critical", 0),
        "expiring_72h": summary.get("expiring_72h", 0),
        "pending_transfers": summary.get("pending_transfers", 0),
        "network_health_pct": summary.get("network_health_pct"),
    }

    if cover is not None and not cover.empty:
        measurable = cover[cover["days_of_cover"].notna()]
        critical = measurable[measurable["risk_bucket"] == "CRITICAL"]

        if not critical.empty:
            worst = critical.loc[critical["days_of_cover"].idxmin()]
            facts["worst_series"] = {
                "component_code": worst["component_code"],
                "group_code": worst["group_code"],
                "units_available": int(worst["units_available"]),
                "days_of_cover": float(worst["days_of_cover"]),
            }

    if rescue is not None and not rescue.empty:
        act_now = rescue[rescue["rescue_tier"] == "ACT_NOW"]
        urgent = act_now[act_now["hours_to_deadline"].notna()]
        urgent = urgent[urgent["hours_to_deadline"] <= 72]

        if not urgent.empty:
            soonest = urgent.loc[urgent["hours_to_deadline"].idxmin()]
            same_route = urgent[
                (urgent["component_code"] == soonest["component_code"])
                & (
                    urgent["best_recipient_facility_id"]
                    == soonest["best_recipient_facility_id"]
                )
            ]

            facts["top_rescue"] = {
                "units": int(len(same_route)),
                "component_code": soonest["component_code"],
                "group_code": soonest["group_code"],
                "destination": soonest["destination_name"],
                "travel_minutes": (
                    int(soonest["best_travel_minutes"])
                    if pd.notna(soonest["best_travel_minutes"])
                    else None
                ),
                "hours_to_deadline": float(soonest["hours_to_deadline"]),
            }

        facts["unrescuable"] = int((rescue["rescue_tier"] == "UNRESCUABLE").sum())

    if transfers is not None and not transfers.empty:
        pending = transfers[transfers["status"] == "RECOMMENDED"]
        facts["pending_units"] = int(pending["units"].sum())
        facts["pending_count"] = int(len(pending))

    return facts


def facility_briefing(facts: dict) -> list[str]:
    """The morning brief as sentences. 120-180 words per spec §10.2.

    Returns a list of paragraphs so the caller controls layout. Any clause whose
    underlying value is missing is omitted rather than guessed — §10.3's prompt
    contract requires exactly that of the LLM, and the template holds itself to
    the same rule.
    """

    lines: list[str] = []

    worst = facts.get("worst_series")

    if worst:
        lines.append(
            f"Your **{worst['group_code']} {worst['component_code']}** stock is "
            f"down to **{num(worst['units_available'])} units**, about "
            f"**{worst['days_of_cover']:.1f} days** of cover at your recent "
            "rate of use. That is the tightest position on your shelves today."
        )
    elif facts.get("shortage_alerts") == 0:
        lines.append(
            "No series is above the shortage-warning threshold at your facility "
            "today. Stock positions are within their reserve floors across every "
            "component and group."
        )

    rescue = facts.get("top_rescue")

    if rescue:
        destination = rescue.get("destination")
        travel = rescue.get("travel_minutes")

        sentence = (
            f"**{num(rescue['units'])} "
            f"{rescue['group_code']} {rescue['component_code']}** "
            f"{'unit' if rescue['units'] == 1 else 'units'} must leave within "
            f"**{rescue['hours_to_deadline']:.0f} hours** to still arrive usable"
        )

        if destination:
            sentence += f", and **{destination}**"

            if travel is not None:
                sentence += f" — {travel_label(travel)} away —"

            sentence += " has projected demand for them"

        lines.append(sentence + ".")

    unrescuable = facts.get("unrescuable")

    if unrescuable:
        lines.append(
            f"{num(unrescuable)} units cannot be saved from the current position; "
            "each one shows the reason on the Expiry Rescue page."
        )

    pending = facts.get("pending_count")

    if pending:
        lines.append(
            f"**{num(pending)} recommended transfers** "
            f"({num(facts.get('pending_units', 0))} units) are waiting for your "
            "decision."
        )

    if not lines:
        lines.append(
            "Nothing needs your decision this morning. Stock positions, expiry "
            "risk and the transfer plan are all clear at your facility."
        )

    return lines


def transfer_rationale(row) -> str:
    """One or two sentences per recommended transfer (spec §10.2)."""

    stored = row.get("rationale_en")

    if stored:
        return stored

    return (
        f"Move {num(row.get('units'))} units of {row.get('component_code')} "
        f"{row.get('group_code')} to {row.get('to_name')}."
    )


def incident_brief(
    results: dict,
    scenario: dict,
    *,
    language: str = "en",
) -> list[str]:
    """One-page emergency brief (spec §9.5 item 7, §10.2 ~250 words)."""

    totals = results.get("totals", {})

    if language == "ur":
        lines = [
            f"**{num(totals.get('casualties'))} متاثرین** والے "
            f"{scenario.get('name', 'ہنگامی واقعے')} کے لیے پورے نیٹ ورک میں "
            f"تقریباً **{num(totals.get('units_required_p50'))} خون کے یونٹس** "
            f"درکار ہوں گے (P95: {num(totals.get('units_required_p95'))})۔ یہ تخمینہ "
            f"{num(totals.get('iterations'))} مونٹی کارلو تکرار پر مبنی ہے۔",
            f"ہر بلڈ گروپ کی ضرورت الگ محفوظ رکھنے پر منصوبہ بندی کی حد "
            f"**{num(totals.get('planning_requirement_p95'))} یونٹس** بنتی ہے، "
            "کیونکہ ایک گروپ کی اضافی مقدار دوسرے گروپ کی کمی پوری نہیں کرتی۔",
            f"موجودہ مقامی ذخیرہ متاثرہ مراکز کی ضرورت کا "
            f"**{totals.get('coverage_before_actions_pct')}%** پورا کر سکتا ہے؛ "
            f"فوری کمی **{num(totals.get('gap_units_now'))} یونٹس** ہے۔",
        ]

        transfers = results.get("emergency_transfers") or []
        if transfers:
            lines.append(
                f"**{num(len(transfers))} ہنگامی منتقلیوں** کے مجوزہ منصوبے سے "
                f"دستیابی **{totals.get('coverage_after_actions_pct')}%** تک بڑھتی ہے "
                f"اور باقی کمی {num(totals.get('gap_units_after_plan'))} یونٹس رہتی ہے۔"
            )

        donors = results.get("donor_mobilization") or []
        if donors:
            top = donors[0]
            lines.append(
                f"باقی کمی کے لیے عطیہ دہندگان کو متحرک کرنا ہوگا؛ سب سے پہلے "
                f"**{num(top['donors_needed'])} {top['blood_group_code']} عطیہ دہندگان** "
                f"درکار ہیں تاکہ {num(top['gap_units'])} یونٹس کی کمی پوری ہو۔"
            )

        time_to_critical = totals.get("time_to_critical_minutes")
        if time_to_critical is not None:
            hours = time_to_critical // 60
            minutes = time_to_critical % 60
            lines.append(
                f"منتخب آغاز کے خاکے میں مجموعی طلب دستیاب رسد سے "
                f"**h+{hours}:{minutes:02d}** پر بڑھ جاتی ہے۔"
            )

        if totals.get("unplaced_casualties"):
            lines.append(
                f"علاقائی علاج کی فوری گنجائش سے **{num(totals['unplaced_casualties'])} "
                "متاثرین** زیادہ ہیں؛ مریضوں کی تقسیم اور اضافی طبی صلاحیت الگ سے درکار ہے۔"
            )

        return lines

    lines = [
        f"A {scenario.get('name', 'mass-casualty event')} producing "
        f"**{num(totals.get('casualties'))} casualties** would require an "
        f"estimated **{num(totals.get('units_required_p50'))} blood units** "
        f"(P95: {num(totals.get('units_required_p95'))}) across the network, "
        f"based on {num(totals.get('iterations'))} Monte Carlo iterations.",
        f"Planning against each blood group separately raises the requirement to "
        f"**{num(totals.get('planning_requirement_p95'))} units**, because a "
        "surplus in one group does not answer a shortfall in another.",
        f"As the network stands, affected facilities can meet "
        f"**{totals.get('coverage_before_actions_pct')}%** of that requirement "
        f"from local stock, leaving a gap of "
        f"**{num(totals.get('gap_units_now'))} units**.",
    ]

    transfers = results.get("emergency_transfers") or []

    if transfers:
        lines.append(
            f"An emergency redistribution of **{num(len(transfers))} movements** "
            f"raises coverage to "
            f"**{totals.get('coverage_after_actions_pct')}%**, leaving "
            f"{num(totals.get('gap_units_after_plan'))} units outstanding."
        )

    donors = results.get("donor_mobilization") or []

    if donors:
        top = donors[0]
        lines.append(
            f"Closing the remainder requires donor mobilization, led by "
            f"**{num(top['donors_needed'])} {top['blood_group_code']} donors** "
            f"for a {num(top['gap_units'])}-unit shortfall."
        )

    time_to_critical = totals.get("time_to_critical_minutes")

    if time_to_critical is not None:
        hours = time_to_critical // 60
        minutes = time_to_critical % 60
        lines.append(
            f"On the modelled onset profile, cumulative demand overtakes "
            f"available supply at **h+{hours}:{minutes:02d}**."
        )

    if totals.get("unplaced_casualties"):
        lines.append(
            f"Regional receiving capacity is exceeded by "
            f"**{num(totals['unplaced_casualties'])} casualties**; patient "
            "distribution and additional clinical capacity require separate action."
        )

    return lines
