"""Interactive candidate shortlisting preserves urgent network coverage."""

from engines.optimizer.plan import shortlist_route_pairs


def candidate(source, destination, recipient, *, minutes=60, rank=1, units=5):
    return {
        "source_id": source,
        "dest_id": destination,
        "component_id": 2,
        "donor_group_id": recipient,
        "recipient_group_id": recipient,
        "preference_rank": rank,
        "upper_bound": units,
        "travel_minutes": minutes,
    }


def test_shortlist_keeps_destination_floor_and_prefers_urgent_direct_routes():
    candidates = [
        candidate("s1", "d1", 1, minutes=15, units=8),
        candidate("s2", "d1", 1, minutes=90, units=8),
        candidate("s3", "d1", 1, minutes=180, rank=3, units=8),
        candidate("s1", "d2", 2, minutes=30, units=4),
        candidate("s2", "d2", 2, minutes=60, units=4),
        candidate("s3", "d2", 2, minutes=120, rank=2, units=4),
    ]
    deficits = {("d1", 2, 1): 8, ("d2", 2, 2): 4}
    buckets = {("d1", 2, 1): "CRITICAL", ("d2", 2, 2): "WATCH"}

    kept, diagnostics = shortlist_route_pairs(
        candidates=candidates,
        deficits=deficits,
        risk_buckets=buckets,
        criticality={2: 1.0},
        at_risk={},
        max_pairs=4,
        min_pairs_per_destination=1,
        log=lambda *_: None,
    )

    pairs = {(row["source_id"], row["dest_id"]) for row in kept}
    assert len(pairs) == 4
    assert {destination for _, destination in pairs} == {"d1", "d2"}
    assert ("s1", "d1") in pairs
    assert diagnostics == {
        "available_route_pairs": 6,
        "selected_route_pairs": 4,
        "excluded_route_pairs": 2,
    }


def test_shortlist_is_deterministic_and_zero_cap_keeps_every_candidate():
    candidates = [
        candidate("s3", "d1", 1),
        candidate("s1", "d1", 1),
        candidate("s2", "d1", 1),
    ]
    kwargs = {
        "candidates": candidates,
        "deficits": {("d1", 2, 1): 5},
        "risk_buckets": {("d1", 2, 1): "WARNING"},
        "criticality": {2: 1.0},
        "at_risk": {},
        "min_pairs_per_destination": 1,
        "log": lambda *_: None,
    }

    first, _ = shortlist_route_pairs(max_pairs=2, **kwargs)
    second, _ = shortlist_route_pairs(max_pairs=2, **kwargs)
    uncapped, diagnostics = shortlist_route_pairs(max_pairs=0, **kwargs)

    assert first == second
    assert {(row["source_id"], row["dest_id"]) for row in first} == {
        ("s1", "d1"),
        ("s2", "d1"),
    }
    assert uncapped == candidates
    assert diagnostics["excluded_route_pairs"] == 0
