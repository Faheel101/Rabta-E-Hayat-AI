from db.session import SessionLocal, init_db
from services.simulation_service import run_simulation


SCENARIO = {
    "name": "Major bus accident — Motorway M2",
    "event_type": "BUS_ACCIDENT",
    "epicenter_label": "Lahore-Islamabad Motorway M2 near Lahore",
    "epicenter_lat": 31.6000,
    "epicenter_lon": 74.3000,
    "casualties": 180,
    "severity_mix": {
        "MINOR": 0.30,
        "MODERATE": 0.30,
        "SEVERE": 0.28,
        "CRITICAL": 0.12,
    },
    "onset_profile": "RAMP_6H",
    "duration_hours": 12,
    "seed": 42,
    "iterations": 1000,
}


def main():
    init_db()
    session = SessionLocal()

    try:
        print("Running emergency simulation...")
        results = run_simulation(session, SCENARIO, save=True)

        totals = results["totals"]

        print("Simulation complete.")
        print(f"Run id: {results.get('run_id')}")
        print()
        print("Requirement")
        print(f"  Monte Carlo total     P50 {totals['units_required_p50']:>5}"
              f"   P95 {totals['units_required_p95']:>5}")
        print(f"  Per-group planning    P50 {totals['planning_requirement_p50']:>5}"
              f"   P95 {totals['planning_requirement_p95']:>5}")
        print("    The planning figure sums each component-group cell's own P95.")
        print("    Quantiles are not additive, so it exceeds the Monte Carlo P95")
        print("    by construction — but a controller cannot net a B+ surplus")
        print("    against an O- shortfall, so it is the figure to procure to.")
        print()
        print("Coverage against the per-facility requirement (both on the same basis)")
        print(f"  as the network stands  {totals['network_can_supply_now']:>5} units"
              f"   {totals['coverage_before_actions_pct']:>5}%"
              f"   gap {totals['gap_units_now']}")
        print(f"  after the transfer plan {totals['network_can_supply_after_plan']:>4} units"
              f"   {totals['coverage_after_actions_pct']:>5}%"
              f"   gap {totals['gap_units_after_plan']}")
        print()
        print(f"Time to critical: {totals['time_to_critical_minutes']} minutes")
        print(f"Emergency transfers: {totals['emergency_transfers']}")

        print("Donor mobilization:")
        for item in results["donor_mobilization"]:
            print(
                f"  {item['blood_group_code']}: "
                f"{item['donors_needed']} donors "
                f"for gap {item['gap_units']} units"
            )

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


if __name__ == "__main__":
    main()