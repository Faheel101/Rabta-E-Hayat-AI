"""ISBT 128 identifier invariants.

The four check-character vectors below are published values from ICCBBA and
JPAC documents, confirmed during verification by independent recomputation. They
are the reason this module can be trusted: a check-character algorithm that is
merely self-consistent proves nothing, because a wrong algorithm is also
self-consistent.
"""

from __future__ import annotations

import pytest

from core import isbt

# (13-character DIN, published check character, source)
PUBLISHED_VECTORS = [
    (
        "G123417654321",
        "A",
        "ICCBBA IG 'Use of the Donation Identification Number' v1.2.0 s3.3",
    ),
    (
        "A999917123456",
        "9",
        "ICCBBA IG 'Use of the Donation Identification Number' v1.2.0 Fig 1",
    ),
    (
        "A999916123456",
        "Q",
        "ICCBBA IN-015 'An Introduction to ISBT 128' 6th ed. 2018 s4",
    ),
    (
        "G123498654321",
        "H",
        "UK JPAC / NHSBT Red Book Annexe 2",
    ),
]


@pytest.mark.parametrize("din,expected,source", PUBLISHED_VECTORS)
def test_check_character_matches_published_vector(din, expected, source):
    assert isbt.check_character(din) == expected, (
        f"MOD 37-2 check character for {din} should be {expected} per {source}"
    )


@pytest.mark.parametrize("din,expected,_source", PUBLISHED_VECTORS)
def test_appended_check_character_self_verifies(din, expected, _source):
    assert isbt.verify(din + expected)


@pytest.mark.parametrize("din,expected,_source", PUBLISHED_VECTORS)
def test_wrong_check_character_is_rejected(din, expected, _source):
    wrong = "0" if expected != "0" else "1"

    assert not isbt.verify(din + wrong)


def test_alphabet_includes_i_and_o():
    """The ISO 7064 table is the full A-Z.

    Stripping I and O — as the FIN's first-character rule does, for human
    confusion reasons — would silently produce wrong check characters for every
    DIN containing them. The two alphabets are deliberately different.
    """

    assert isbt.ISO7064_ALPHABET.index("I") == 18
    assert isbt.ISO7064_ALPHABET.index("O") == 24
    assert len(isbt.ISO7064_ALPHABET) == 37


def test_check_character_catches_every_single_character_substitution():
    """MOD 37-2 is specified to detect all single-character errors."""

    din = "G123417654321"
    baseline = isbt.check_character(din)

    for position in range(len(din)):
        for replacement in isbt.ISO7064_ALPHABET[:36]:
            if replacement == din[position]:
                continue

            mutated = din[:position] + replacement + din[position + 1 :]

            assert isbt.check_character(mutated) != baseline, (
                f"substitution at {position} -> {replacement} was not detected"
            )


def test_check_character_catches_adjacent_transpositions():
    din = "G123417654321"
    baseline = isbt.check_character(din)

    for position in range(len(din) - 1):
        if din[position] == din[position + 1]:
            continue

        mutated = (
            din[:position]
            + din[position + 1]
            + din[position]
            + din[position + 2 :]
        )

        assert isbt.check_character(mutated) != baseline


def test_star_is_rejected_in_the_payload():
    with pytest.raises(isbt.IsbtError):
        isbt.check_character("G12341765432*")


def test_build_din_has_the_specified_shape():
    identifier = isbt.build_din(sequence=123456, year=2026, fin="G1234")

    assert identifier.din == "G123426123456"
    assert len(identifier.din) == isbt.DIN_LENGTH
    assert identifier.fin == "G1234"
    assert identifier.year == "26"
    assert identifier.sequence == "123456"
    assert isbt.verify(identifier.din + identifier.check_character)


def test_barcode_content_carries_the_data_identifier_but_the_din_does_not():
    """The '=' is a data identifier, not part of the DIN, and is excluded from
    the check-character calculation."""

    identifier = isbt.build_din(sequence=1, year=2026, fin="G1234")

    assert identifier.barcode_content == "=" + identifier.din
    assert not identifier.din.startswith("=")
    assert isbt.check_character(identifier.din) == identifier.check_character


def test_sequence_must_fit_the_field():
    with pytest.raises(isbt.IsbtError):
        isbt.build_din(sequence=1000000, year=2026, fin="G1234")


def test_parse_round_trips_and_validates_a_supplied_check_character():
    identifier = isbt.build_din(sequence=654321, year=2017, fin="G1234")

    assert isbt.parse_din(identifier.din).din == identifier.din
    assert (
        isbt.parse_din(identifier.din + identifier.check_character).din
        == identifier.din
    )

    with pytest.raises(isbt.IsbtError):
        wrong = "B" if identifier.check_character != "B" else "C"
        isbt.parse_din(identifier.din + wrong)


def test_without_an_assigned_fin_identifiers_declare_themselves_provisional():
    """A self-invented prefix is not a conformant DIN, and the system must not
    claim otherwise. The FIN is assigned by ICCBBA on registration."""

    identifier = isbt.build_din(sequence=42, year=2026)

    assert identifier.is_provisional is True

    status = isbt.conformance_status()

    assert status["is_conformant"] is False
    assert "not conformant" in status["message"]


def test_product_codes_are_absent_until_a_licensed_table_is_configured():
    """The Product Description Code Database is licence-gated by ICCBBA. A
    guessed E-code describes a different product, so returning nothing is the
    only safe default."""

    assert isbt.product_description_code("PRBC") is None


def test_checksum_flag_encoding_is_in_the_published_range():
    flags = isbt.flag_characters_for_checksum("G123417654321")

    assert flags.isdigit()
    assert 60 <= int(flags) <= 96


# ------------------------------------------------- conformance is not inferred


def test_parsing_never_claims_conformance_for_a_foreign_din():
    """A DIN read off a bag carries a Facility Identification Number this system
    neither issued nor can check against the ICCBBA register.

    The old logic reported is_provisional=False for ANY well-formed DIN as soon
    as some FIN was configured — including identifiers belonging to other
    facilities entirely. That is a conformance claim made on no evidence, and it
    is the same false-conformance bug that was fixed in build_din() reappearing
    on the read path.
    """

    foreign = isbt.build_din(sequence=99, year=2026, fin="W1234", provisional=True)

    assert isbt.parse_din(foreign.din).is_provisional is True


def test_a_z_block_placeholder_always_parses_as_provisional():
    """Z-prefixed FINs are the deliberate placeholder. ICCBBA has assigned none
    to this network, so one can never be conformant however the config reads."""

    placeholder = isbt.build_din(sequence=1, year=2026, fin="Z0001", provisional=True)

    assert isbt.parse_din(placeholder.din).is_provisional is True


def test_an_unstated_provisional_flag_defaults_to_provisional():
    """A configured FIN means somebody typed a value into the config; it does not
    mean ICCBBA assigned it. An unknown must never read as a confirmed fact on a
    conformance claim."""

    import inspect

    source = inspect.getsource(isbt.configured_fin)

    assert 'fin_is_provisional", False' not in source, (
        "configured_fin defaults the provisional flag to False, so an unstated "
        "fact reads as a confirmed one"
    )
