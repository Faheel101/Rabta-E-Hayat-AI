"""Collection and inventory simulation (spec §15.3).

The old generator drew a fill probability at random and sized current inventory
as `avg_daily_demand x cover_days` for every facility. Two consequences:

  * Nothing was ever in surplus. A Regional Blood Centre with 1,800 units of
    PRBC capacity held 50. The optimizer had nothing to redistribute, which
    means the product's founding claim — blood sits in the wrong building at the
    wrong time — was not represented anywhere in the data.
  * Wastage was unmeasurable. Only current inventory existed, so there were no
    expired or issued units to count, and the 13.5%-wastage argument the whole
    product rests on had no support in its own demo data.

This module simulates the real process instead: facilities order to a target
cover level, collections are constrained by donor availability and collapse
during Ramadan, stock is issued FEFO with compatible substitution when the
identical group runs out, and whatever is not issued in time expires. Fill rate,
unmet demand, substitution rate and wastage all become consequences rather than
parameters.
"""

from __future__ import annotations

from collections import defaultdict

import numpy as np

from config.calendar import get_calendar_flags
from core import config
from core.policy import reserve_floor, storage_capacity

RARE_GROUPS = {"O-", "A-", "B-", "AB-"}

# Probability that a rare-group collection cycle yields anything at all.
#
# Pakistan's Rh-negative population share is about 7% (spec §19.2), so a facility
# that orders four units of O- does not receive a reduced quantity — often it
# receives none, because no matching donor presented. Modelling this as a simple
# multiplier meant every rare group still reached its target over a few cycles,
# and the data ended up with *more* days of cover for O- and AB- than for O+.
# That inverts the central fact of the problem: it is precisely the Rh-negative
# groups whose shortages dominate, which is why redistribution matters more here
# than in Rh-negative-rich populations.
GROUP_COLLECTION_SUCCESS = {
    "O-": 0.42,
    "B-": 0.46,
    "A-": 0.48,
    "AB-": 0.32,
}

REVIEW_INTERVAL_DAYS = {
    "RBC": 1,
    "TERTIARY_HOSPITAL": 1,
    "SPECIALIST_CENTRE": 2,
    "DHQ": 2,
    "THQ": 3,
}


def trailing_mean(series: np.ndarray, window: int = 28) -> np.ndarray:
    """Trailing mean of the preceding `window` days, excluding today."""

    if len(series) == 0:
        return series.astype(float)

    padded = np.concatenate([np.zeros(window), series.astype(float)])
    cumulative = np.cumsum(padded)

    totals = cumulative[window:] - cumulative[:-window]
    counts = np.minimum(np.arange(1, len(series) + 1), window)

    # Warm-up days have fewer than `window` observations behind them.
    return totals / np.maximum(counts, 1)


def assign_order_factors(facilities, rng):
    """Per-facility stocking posture.

    Spec §15.3 asks for facilities deliberately configured to over-order, because
    that is what generates the wastage the rescue engine finds. On its own that
    is not enough: if every other facility is exactly right-sized, the network
    wastes without ever running short, and the product's actual claim — that
    preventable shortage and preventable expiry happen in the same region on the
    same day — is not reproduced.

    So facilities are split three ways: over-stockers who hoard (and waste),
    chronically under-resourced facilities who run short, and the rest.
    """

    over_fraction = float(config.get("supply.over_order.facility_fraction", 0.30))
    over_low, over_high = config.get("supply.over_order.factor_range", [1.8, 2.6])
    rare_extra = float(config.get("supply.over_order.rare_group_extra_factor", 1.6))

    under_fraction = float(config.get("supply.under_order.facility_fraction", 0.30))
    under_low, under_high = config.get("supply.under_order.factor_range", [0.65, 0.85])

    eligible = [f for f in facilities if f.facility_type != "RBC"]
    order = rng.permutation(len(eligible)).tolist()

    over_count = int(round(len(eligible) * over_fraction))
    under_count = int(round(len(eligible) * under_fraction))

    over = set(order[:over_count])
    under = set(order[over_count : over_count + under_count])

    factors = {}
    posture = {}

    for index, facility in enumerate(eligible):
        if index in over:
            factors[facility.id] = float(rng.uniform(float(over_low), float(over_high)))
            posture[facility.id] = "OVER_STOCKED"
        elif index in under:
            factors[facility.id] = float(
                rng.uniform(float(under_low), float(under_high))
            )
            posture[facility.id] = "UNDER_RESOURCED"
        else:
            factors[facility.id] = 1.0
            posture[facility.id] = "BALANCED"

    for facility in facilities:
        factors.setdefault(facility.id, 1.0)
        posture.setdefault(facility.id, "HUB")

    return factors, posture, rare_extra


def build_substitution_order(compatibility_rows, group_ids):
    """(component, recipient group) -> donor groups, best preference first.

    Pairs flagged `requires_override` are excluded: a routine issue must not
    silently use ABO-incompatible platelets.
    """

    ordered = defaultdict(list)

    by_key = defaultdict(list)

    for component_id, recipient_id, donor_id, rank, requires_override in (
        compatibility_rows
    ):
        if requires_override:
            continue

        by_key[(component_id, recipient_id)].append((int(rank), donor_id))

    for key, entries in by_key.items():
        entries.sort(key=lambda item: (item[0], item[1]))
        ordered[key] = [donor_id for rank, donor_id in entries if donor_id != key[1]]

    return ordered


class InventorySimulation:
    """Day-by-day FEFO inventory process across the whole network."""

    def __init__(
        self,
        facilities,
        components,
        groups,
        days,
        requests,
        substitution_order,
        rng,
    ):
        self.facilities = facilities
        self.components = components
        self.groups = groups
        self.days = days
        self.requests = requests
        self.substitution_order = substitution_order
        self.rng = rng

        self.facility_by_id = {f.id: f for f in facilities}
        self.component_by_id = {c.id: c for c in components}
        self.group_by_id = {g.id: g for g in groups}

        self.cover_days = config.get("supply.cover_days") or {}
        self.screening_failure_rate = float(
            config.get("supply.screening_failure_rate", 0.03)
        )
        self.fefo_compliance = float(config.get("supply.fefo_compliance", 1.0))
        self.hub_spoke_share = float(config.get("supply.hub.spoke_demand_share", 0.30))
        self.hub_cover_multiplier = float(
            config.get("supply.hub.cover_days_multiplier", 2.5)
        )

        (
            self.over_order_factors,
            self.posture,
            self.rare_extra,
        ) = assign_order_factors(facilities, rng)

        self.capacity = {
            (facility.id, component.id): storage_capacity(facility, component.code)
            for facility in facilities
            for component in components
        }

        # stock[(facility_id, component_id)][group_id][expiry_day_index] = units
        self.stock = defaultdict(lambda: defaultdict(dict))

        self.series_keys = list(requests.keys())
        self.trailing = {
            key: trailing_mean(series) for key, series in requests.items()
        }

        self.reserve_floors = {
            (facility.id, component.id, group.id): reserve_floor(
                facility, component.code, group.code
            )
            for facility in facilities
            for component in components
            for group in groups
        }

        self._build_targets()
        self._reset_ledgers()

    # -- targets -----------------------------------------------------------

    def _build_targets(self):
        """Target stock level per series per day, in units."""

        spokes_by_hub = defaultdict(list)

        for facility in self.facilities:
            if facility.parent_rbc_id:
                spokes_by_hub[facility.parent_rbc_id].append(facility.id)

        day_count = len(self.days)
        self.targets = {}

        for facility in self.facilities:
            is_hub = facility.facility_type == "RBC"
            over_order = self.over_order_factors.get(facility.id, 1.0)

            for component in self.components:
                cover = float(self.cover_days.get(component.code, 7))

                for group in self.groups:
                    key = (facility.id, component.id, group.id)

                    own = self.trailing.get(key)
                    basis = (
                        own.copy()
                        if own is not None
                        else np.zeros(day_count, dtype=float)
                    )

                    effective_cover = cover

                    if is_hub:
                        # A hub stocks against the demand of the network it
                        # serves, not its own consumption.
                        for spoke_id in spokes_by_hub.get(facility.id, []):
                            spoke = self.trailing.get(
                                (spoke_id, component.id, group.id)
                            )

                            if spoke is not None:
                                basis += self.hub_spoke_share * spoke

                        effective_cover = cover * self.hub_cover_multiplier

                    ratio = over_order

                    if over_order > 1.0 and group.code in RARE_GROUPS:
                        # Hoarding rare groups is exactly how a district blood
                        # bank ends up with AB- units no one can use.
                        ratio *= self.rare_extra

                    target = basis * effective_cover * ratio

                    # A blood bank does not hold stock strictly in proportion to
                    # demand. It holds a floor of every group it might be asked
                    # for, and for a group it is asked for twice a month that
                    # floor is what expires. This is the dominant real wastage
                    # channel and the spec's own example of an unrescuable unit:
                    # AB- red cells sitting at a district hospital with no
                    # compatible recipient in range before expiry.
                    floor = self.reserve_floors.get(key, 0.0)

                    if floor <= 0 and key in self.requests:
                        floor = 1.0

                    if floor > 0:
                        target = np.maximum(target, floor)

                    if target.max() <= 0:
                        continue

                    self.targets[key] = target

    def _reset_ledgers(self):
        day_count = len(self.days)

        def zeros():
            return np.zeros(day_count, dtype=np.int32)

        self.issued = defaultdict(zeros)
        self.substituted_in = defaultdict(zeros)
        self.unmet = defaultdict(zeros)
        self.collected = defaultdict(zeros)
        self.screening_failed = defaultdict(zeros)
        self.expired = defaultdict(zeros)
        self.discarded_other = defaultdict(zeros)
        self.available_end = defaultdict(zeros)

        self.days_to_expiry_at_issue = []
        self.terminal_events = []

    # -- helpers -----------------------------------------------------------

    def _available(self, facility_id, component_id, group_id) -> int:
        buckets = self.stock[(facility_id, component_id)].get(group_id)

        if not buckets:
            return 0

        return int(sum(buckets.values()))

    def _take_fefo(
        self,
        facility_id,
        component_id,
        group_id,
        wanted,
        day_index,
        record_terminal,
    ) -> int:
        """Issue up to `wanted` units, first expiry first out."""

        buckets = self.stock[(facility_id, component_id)].get(group_id)

        if not buckets or wanted <= 0:
            return 0

        taken = 0

        # Real blood banks do not achieve perfect first-expiry-first-out: units
        # get issued by which shelf the technologist reached for. Strict FEFO is
        # what this product recommends, so the baseline data must not already
        # assume it — otherwise a measurable share of preventable expiry is
        # engineered out of the problem before the engine ever sees it.
        order = sorted(buckets)

        if self.fefo_compliance < 1.0 and self.rng.random() > self.fefo_compliance:
            order = sorted(buckets, reverse=True)

        for expiry_index in order:
            if taken >= wanted:
                break

            available = buckets[expiry_index]

            if available <= 0:
                del buckets[expiry_index]
                continue

            use = min(available, wanted - taken)
            buckets[expiry_index] = available - use

            if buckets[expiry_index] <= 0:
                del buckets[expiry_index]

            taken += use
            self.days_to_expiry_at_issue.append(
                (component_id, expiry_index - day_index, use)
            )

            if record_terminal:
                self.terminal_events.append(
                    (
                        facility_id,
                        component_id,
                        group_id,
                        expiry_index,
                        day_index,
                        use,
                        "ISSUED",
                        None,
                    )
                )

        return taken

    # -- the day loop ------------------------------------------------------

    def run(self, terminal_history_from_index: int):
        shelf_life_by_component = {
            c.id: int(c.shelf_life_days) for c in self.components
        }
        review_by_facility = {
            f.id: REVIEW_INTERVAL_DAYS.get(f.facility_type, 2) for f in self.facilities
        }

        component_ids = [c.id for c in self.components]
        group_ids = [g.id for g in self.groups]
        group_code = {g.id: g.code for g in self.groups}

        for day_index, day in enumerate(self.days):
            flags = get_calendar_flags(day)
            record_terminal = day_index >= terminal_history_from_index

            # Ramadan collection collapse. Demand for several components does
            # not fall by nearly as much, which is the squeeze.
            collection_factor = 0.5 if flags["ramadan"] else 1.0

            if flags["eid_fitr"] or flags["eid_adha"]:
                collection_factor = 0.35

            for facility in self.facilities:
                is_review_day = day_index % review_by_facility[facility.id] == 0

                for component_id in component_ids:
                    shelf_life = shelf_life_by_component[component_id]
                    stock_for_component = self.stock[(facility.id, component_id)]

                    self._expire(
                        facility.id,
                        component_id,
                        group_ids,
                        stock_for_component,
                        day_index,
                        record_terminal,
                    )

                    if is_review_day:
                        # Storage capacity is a physical constraint, not a
                        # preference: a facility cannot hold more than its
                        # fridge and freezer space allows, however much it
                        # over-orders. When it binds, the shortfall is shared
                        # pro rata across groups. Filling groups in id order
                        # instead would starve whichever groups happen to sort
                        # last, which is an artefact of the loop, not clinical
                        # behaviour.
                        self._order_component(
                            facility,
                            component_id,
                            group_ids,
                            group_code,
                            stock_for_component,
                            day_index,
                            shelf_life,
                            collection_factor,
                        )

                    for group_id in group_ids:
                        key = (facility.id, component_id, group_id)

                        self._issue(
                            key,
                            day_index,
                            component_id,
                            group_id,
                            facility.id,
                            record_terminal,
                        )

                        remaining = self._available(
                            facility.id, component_id, group_id
                        )

                        if remaining:
                            self.available_end[key][day_index] = remaining

        self._apply_non_expiry_discards(terminal_history_from_index)

    def _expire(
        self,
        facility_id,
        component_id,
        group_ids,
        stock_for_component,
        day_index,
        record_terminal,
    ):
        for group_id in group_ids:
            buckets = stock_for_component.get(group_id)

            if not buckets:
                continue

            due = [expiry for expiry in buckets if expiry <= day_index]

            if not due:
                continue

            expired_units = 0

            for expiry_index in due:
                units = buckets.pop(expiry_index)
                expired_units += units

                if record_terminal and units > 0:
                    self.terminal_events.append(
                        (
                            facility_id,
                            component_id,
                            group_id,
                            expiry_index,
                            day_index,
                            units,
                            "EXPIRED",
                            "EXPIRY",
                        )
                    )

            if expired_units:
                self.expired[(facility_id, component_id, group_id)][
                    day_index
                ] += expired_units

    def _order_component(
        self,
        facility,
        component_id,
        group_ids,
        group_code,
        stock_for_component,
        day_index,
        shelf_life,
        collection_factor,
    ) -> None:
        headroom = self.capacity.get((facility.id, component_id), 0.0) - sum(
            sum(buckets.values()) for buckets in stock_for_component.values()
        )

        if headroom <= 0:
            return

        wanted = {}

        for group_id in group_ids:
            key = (facility.id, component_id, group_id)
            target = self.targets.get(key)

            if target is None or target[day_index] <= 0:
                continue

            gap = float(target[day_index]) - self._available(
                facility.id, component_id, group_id
            )

            if gap <= 0.5:
                continue

            # Collection is lumpy and never exactly to plan.
            ordered = gap * collection_factor
            ordered *= float(self.rng.uniform(0.75, 1.20))

            # A rare group either has a matching donor this cycle or it does not.
            success = GROUP_COLLECTION_SUCCESS.get(group_code[group_id], 1.0)

            if success < 1.0 and self.rng.random() > success:
                continue

            if ordered >= 0.5:
                wanted[group_id] = ordered

        if not wanted:
            return

        total_wanted = sum(wanted.values())
        scale = min(1.0, headroom / total_wanted) if total_wanted > 0 else 0.0

        expiry_index = day_index + shelf_life

        for group_id, ordered in wanted.items():
            units = int(round(ordered * scale))

            if units <= 0:
                continue

            key = (facility.id, component_id, group_id)
            self.collected[key][day_index] += units

            failed = int(self.rng.binomial(units, self.screening_failure_rate))

            if failed:
                self.screening_failed[key][day_index] += failed

            passed = units - failed

            if passed > 0:
                buckets = stock_for_component.setdefault(group_id, {})
                buckets[expiry_index] = buckets.get(expiry_index, 0) + passed

    def _issue(
        self,
        key,
        day_index,
        component_id,
        group_id,
        facility_id,
        record_terminal,
    ):
        requested_series = self.requests.get(key)

        if requested_series is None:
            return

        requested = int(requested_series[day_index])

        if requested <= 0:
            return

        issued = self._take_fefo(
            facility_id,
            component_id,
            group_id,
            requested,
            day_index,
            record_terminal,
        )

        shortfall = requested - issued

        if shortfall > 0:
            for donor_group_id in self.substitution_order.get(
                (component_id, group_id), []
            ):
                if shortfall <= 0:
                    break

                substituted = self._take_fefo(
                    facility_id,
                    component_id,
                    donor_group_id,
                    shortfall,
                    day_index,
                    record_terminal,
                )

                if substituted > 0:
                    shortfall -= substituted
                    issued += substituted
                    self.substituted_in[key][day_index] += substituted

        self.issued[key][day_index] = issued

        if shortfall > 0:
            self.unmet[key][day_index] = shortfall

    def _apply_non_expiry_discards(self, terminal_history_from_index: int):
        """Breakage and cold-chain losses, so expiry is not 100% of wastage.

        Kept deliberately small: the Lahore audit found 96% of wastage was
        expiry, and the point of the product is that expiry is the predictable,
        preventable channel.
        """

        rate = 0.006

        for key, expired_series in list(self.expired.items()):
            issued_series = self.issued.get(key)

            if issued_series is None:
                continue

            total_issued = int(issued_series.sum())

            if total_issued <= 0:
                continue

            losses = int(self.rng.binomial(total_issued, rate))

            if losses <= 0:
                continue

            # Attribute the loss to days where units were actually issued.
            issue_days = np.nonzero(issued_series)[0]

            if len(issue_days) == 0:
                continue

            for day_index in self.rng.choice(issue_days, size=losses):
                day_index = int(day_index)

                if self.issued[key][day_index] <= 0:
                    continue

                self.issued[key][day_index] -= 1
                self.discarded_other[key][day_index] += 1

    # -- outputs -----------------------------------------------------------

    def live_units(self):
        """Units still in stock at the end of the simulation."""

        for (facility_id, component_id), by_group in self.stock.items():
            for group_id, buckets in by_group.items():
                for expiry_index, units in buckets.items():
                    if units > 0:
                        yield (
                            facility_id,
                            component_id,
                            group_id,
                            expiry_index,
                            int(units),
                        )

    def realism_report(self):
        def total(ledger):
            return int(sum(int(series.sum()) for series in ledger.values()))

        collected = total(self.collected)
        screening_failed = total(self.screening_failed)
        passed = collected - screening_failed

        expired = total(self.expired)
        discarded_other = total(self.discarded_other)
        wasted = expired + discarded_other

        requested = int(
            sum(int(series.sum()) for series in self.requests.values())
        )
        issued = total(self.issued)
        unmet = total(self.unmet)
        substituted = total(self.substituted_in)

        weighted = defaultdict(float)
        counted = defaultdict(int)

        for component_id, days_left, units in self.days_to_expiry_at_issue:
            weighted[component_id] += days_left * units
            counted[component_id] += units

        days_to_expiry_by_component = {
            component_id: weighted[component_id] / counted[component_id]
            for component_id in counted
            if counted[component_id] > 0
        }

        return {
            "collected": collected,
            "screening_failed": screening_failed,
            "screened_pass": passed,
            "expired": expired,
            "discarded_other": discarded_other,
            "wasted": wasted,
            "wastage_pct": (100.0 * wasted / passed) if passed else 0.0,
            "expiry_share_of_wastage": (expired / wasted) if wasted else 0.0,
            "requested": requested,
            "issued": issued,
            "unmet": unmet,
            "unmet_pct": (100.0 * unmet / requested) if requested else 0.0,
            "fill_rate": (issued / requested) if requested else 0.0,
            "substituted": substituted,
            "substitution_pct": (100.0 * substituted / issued) if issued else 0.0,
            "days_to_expiry_at_issue_by_component": days_to_expiry_by_component,
        }
