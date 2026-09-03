"""Compatibility matrix invariants (spec §19.1).

Spec §19.1 carries an explicit implementation warning: the single most common bug
in blood-supply software is applying the red-cell matrix to plasma, because the
two are inverted. It asks for a unit test asserting that AB plasma is the
universal plasma donor and O red cells are the universal red cell donor. Those
are the first two tests here.
"""

from __future__ import annotations

import pytest

ALL_GROUPS = ["O-", "O+", "A-", "A+", "B-", "B+", "AB-", "AB+"]
# Separated red cells only. Whole blood is NOT a red cell component for
# compatibility purposes: the bag carries the donor's plasma too, so group O
# whole blood delivers anti-A and anti-B to the recipient. These tests
# previously listed WB here and therefore asserted, and enforced, a clinically
# wrong rule — the reference data was only "correct" because the test agreed
# with it.
RED_COMPONENTS = ["PRBC"]
WHOLE_BLOOD_COMPONENTS = ["WB"]
PLASMA_COMPONENTS = ["FFP", "CRYO"]
PLATELET_COMPONENTS = ["PLT_RD", "PLT_APH"]

# Spec §19.1, red cells: recipient (row) may receive from donor (column).
RED_MATRIX = {
    "O-": {"O-"},
    "O+": {"O-", "O+"},
    "A-": {"O-", "A-"},
    "A+": {"O-", "O+", "A-", "A+"},
    "B-": {"O-", "B-"},
    "B+": {"O-", "O+", "B-", "B+"},
    "AB-": {"O-", "A-", "B-", "AB-"},
    "AB+": set(ALL_GROUPS),
}

# Whole blood: ABO identical, Rh-negative may go to an Rh-positive recipient.
WHOLE_BLOOD_MATRIX = {
    "O-": {"O-"},
    "O+": {"O-", "O+"},
    "A-": {"A-"},
    "A+": {"A-", "A+"},
    "B-": {"B-"},
    "B+": {"B-", "B+"},
    "AB-": {"AB-"},
    "AB+": {"AB-", "AB+"},
}

# Spec §19.1, plasma: inverted ABO logic, Rh not a barrier.
PLASMA_ABO = {
    "O": {"O", "A", "B", "AB"},
    "A": {"A", "AB"},
    "B": {"B", "AB"},
    "AB": {"AB"},
}


def abo(code: str) -> str:
    return code[:-1]


@pytest.mark.parametrize("component", PLASMA_COMPONENTS)
@pytest.mark.parametrize("recipient", ALL_GROUPS)
def test_ab_plasma_is_universal_donor(compatibility, component, recipient):
    """AB plasma carries no anti-A or anti-B and suits every recipient."""

    for donor in ("AB+", "AB-"):
        assert (component, recipient, donor) in compatibility, (
            f"{component}: AB plasma must be compatible with {recipient}"
        )


@pytest.mark.parametrize("component", RED_COMPONENTS)
@pytest.mark.parametrize("recipient", ALL_GROUPS)
def test_o_negative_red_cells_are_universal_donor(
    compatibility, component, recipient
):
    assert (component, recipient, "O-") in compatibility, (
        f"{component}: O- red cells must be compatible with {recipient}"
    )


@pytest.mark.parametrize("component", PLASMA_COMPONENTS)
def test_o_plasma_is_not_universal(compatibility, component):
    """The inversion test. O plasma contains anti-A and anti-B, so it must NOT
    be issuable to a group A, B or AB recipient. If the red-cell matrix has been
    applied to plasma by mistake, this is the test that catches it."""

    for recipient in ("A+", "A-", "B+", "B-", "AB+", "AB-"):
        for donor in ("O+", "O-"):
            assert (component, recipient, donor) not in compatibility, (
                f"{component}: O plasma must not be compatible with {recipient} "
                "— the red-cell matrix has been applied to plasma"
            )


@pytest.mark.parametrize("component", RED_COMPONENTS)
def test_red_cell_matrix_matches_spec_exactly(compatibility, component):
    for recipient in ALL_GROUPS:
        for donor in ALL_GROUPS:
            expected = donor in RED_MATRIX[recipient]
            actual = (component, recipient, donor) in compatibility

            assert actual == expected, (
                f"{component} recipient {recipient} donor {donor}: "
                f"expected compatible={expected}, got {actual}"
            )


@pytest.mark.parametrize("component", WHOLE_BLOOD_COMPONENTS)
def test_whole_blood_is_abo_identical(compatibility, component):
    """A unit of whole blood contains the donor's plasma as well as their red
    cells, so ABO must match exactly. This is the single most common way the
    "O is the universal donor" shorthand gets misapplied."""

    for recipient in ALL_GROUPS:
        for donor in ALL_GROUPS:
            expected = donor in WHOLE_BLOOD_MATRIX[recipient]
            actual = (component, recipient, donor) in compatibility

            assert actual == expected, (
                f"{component} recipient {recipient} donor {donor}: "
                f"expected compatible={expected}, got {actual}"
            )


@pytest.mark.parametrize("component", WHOLE_BLOOD_COMPONENTS)
def test_group_o_whole_blood_is_not_a_universal_donor(compatibility, component):
    """The specific failure this rule exists to prevent: O whole blood into a
    group A, B or AB patient, delivering anti-A and anti-B antibodies."""

    for recipient in ("A+", "A-", "B+", "B-", "AB+", "AB-"):
        for donor in ("O+", "O-"):
            assert (component, recipient, donor) not in compatibility, (
                f"{component}: group O whole blood must not be issuable to "
                f"{recipient} — the red cell matrix has been applied to whole blood"
            )


@pytest.mark.parametrize("component", WHOLE_BLOOD_COMPONENTS)
def test_rh_positive_whole_blood_never_goes_to_an_rh_negative_recipient(
    compatibility, component
):
    for recipient in ("O-", "A-", "B-", "AB-"):
        for donor in ("O+", "A+", "B+", "AB+"):
            assert (component, recipient, donor) not in compatibility, (
                f"{component}: Rh-positive whole blood must not be issuable to "
                f"{recipient} without an explicit override"
            )


@pytest.mark.parametrize("component", PLASMA_COMPONENTS)
def test_plasma_matrix_matches_spec_exactly(compatibility, component):
    for recipient in ALL_GROUPS:
        for donor in ALL_GROUPS:
            expected = abo(donor) in PLASMA_ABO[abo(recipient)]
            actual = (component, recipient, donor) in compatibility

            assert actual == expected, (
                f"{component} recipient {recipient} donor {donor}: "
                f"expected compatible={expected}, got {actual}"
            )


@pytest.mark.parametrize("component", RED_COMPONENTS + PLASMA_COMPONENTS)
def test_identical_group_is_always_rank_one(compatibility, component):
    for group in ALL_GROUPS:
        entry = compatibility.get((component, group, group))

        assert entry is not None, f"{component}: {group} to {group} must be compatible"
        assert entry[0] == 1, (
            f"{component}: identical group {group} must be preference rank 1, "
            f"got {entry[0]}"
        )


@pytest.mark.parametrize("component", PLATELET_COMPONENTS)
def test_abo_incompatible_platelets_require_override(compatibility, component):
    """Spec §19.1: ABO-incompatible platelets may be issued in shortage with
    volume reduction, but only under an explicit override."""

    for recipient in ALL_GROUPS:
        for donor in ALL_GROUPS:
            entry = compatibility.get((component, recipient, donor))

            if entry is None:
                continue

            rank, requires_override = entry

            if abo(donor) == abo(recipient):
                assert not requires_override, (
                    f"{component}: {donor} to {recipient} is ABO-identical and "
                    "must not require an override"
                )
            else:
                assert requires_override, (
                    f"{component}: {donor} to {recipient} is ABO-incompatible "
                    "and must require an explicit override"
                )
                assert rank == 3, (
                    f"{component}: ABO-incompatible platelets must be rank 3, "
                    f"got {rank}"
                )


@pytest.mark.parametrize("component", PLATELET_COMPONENTS)
def test_platelets_are_not_universally_interchangeable(compatibility, component):
    """Regression test for the original defect: every one of the 64 platelet
    donor/recipient pairs was marked plainly compatible with no override, so a
    routine plan could ship AB+ platelets to an O- patient for the price of a
    substitution penalty."""

    plain = [
        (recipient, donor)
        for recipient in ALL_GROUPS
        for donor in ALL_GROUPS
        if (entry := compatibility.get((component, recipient, donor)))
        and not entry[1]
    ]

    assert len(plain) == 16, (
        f"{component}: expected 16 override-free platelet pairs (8 identical + "
        f"8 same-ABO different-Rh), found {len(plain)}"
    )
