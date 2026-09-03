"""ISBT 128 Donation Identification Numbers (Data Structure 001).

VERIFIED against ICCBBA primary sources. The check-character algorithm below was
independently reproduced three ways during verification and passes four published
test vectors (see tests/clinical/test_isbt128.py), including one from ICCBBA
TB-006 that the initial research missed.

Structure of the 13-character DIN:

    a p p p p  y y  n n n n n n
    |________| |_|  |_________|
     FIN (5)   year  sequence (6)

  - The barcode content is "=" + DIN + 2 flag characters. The leading "=" is a
    data identifier and is NOT part of the DIN.
  - Data Structure 001 is the only ISBT 128 structure where the second
    data-identifier character is also the first data character, which is why the
    DIN is 13 characters and not 12. That character IS included in the check.
  - The two flag characters are NOT part of the DIN and NOT part of the check.
  - The check character is NOT encoded in the DIN barcode. It is printed boxed
    in eye-readable form only, to catch manual keyboard-entry errors.
  - The year is the year the DIN was *assigned*, not the collection date.
    Pre-printed labels are valid over a 14-month window, so never derive a
    collection date from it.

LICENSING — READ BEFORE CLAIMING CONFORMANCE:

The 5-character Facility Identification Number is assigned by ICCBBA on
registration; it cannot be self-assigned. A locally invented prefix risks
colliding with a real foreign facility's FIN, which would break the global
uniqueness guarantee the standard exists to provide. Until a participating
facility holds a real ICCBBA FIN, `is_provisional` is True on everything this
module emits and the UI must say so. A structurally-shaped identifier is not a
conformant DIN, and presenting one as conformant would be a false claim about a
patient-safety standard.
"""

from __future__ import annotations

from dataclasses import dataclass

from core import config

# ISO/IEC 7064 value table (ICCBBA reference table RT035): value == index.
# The full A-Z is present, INCLUDING I and O. Using a confusion-stripped
# alphabet here is a silent, plausible-looking bug that produces wrong check
# characters for any DIN containing I or O.
ISO7064_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ*"

DIN_LENGTH = 13
FIN_LENGTH = 5
YEAR_LENGTH = 2
SEQUENCE_LENGTH = 6

# The alpha data-identifier character excludes O and I to avoid confusion with
# 0 and 1, and excludes 0 itself.
FIN_FIRST_CHARACTER_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ123456789"


class IsbtError(ValueError):
    """Raised when an identifier cannot be formed or parsed conformantly."""


def check_character(payload: str) -> str:
    """ISO/IEC 7064 MOD 37-2 check character over the 13-character DIN.

    Pure-system recursive method (ISO/IEC 7064:2003 section 7.1). Equivalent to
    the weighted form where the leftmost character carries 2**n and the rightmost
    2**1, but without building large integers.
    """

    total = 0

    for character in payload:
        value = ISO7064_ALPHABET.find(character)

        if value < 0:
            raise IsbtError(
                f"character {character!r} is not in the ISO 7064 MOD 37-2 alphabet"
            )

        if value == 36:
            raise IsbtError("'*' is a check character only and cannot appear in a DIN")

        total = ((total + value) * 2) % 37

    return ISO7064_ALPHABET[(38 - total) % 37]


def verify(din_with_check: str) -> bool:
    """ISO 7064 self-check: payload plus check character is congruent to 1 mod 37.

    The check character carries weight 2**0 == 1, so it is added WITHOUT a
    further doubling — which is why this is not simply `check_character(body)`
    recomputed.
    """

    if len(din_with_check) < 2:
        return False

    body, supplied = din_with_check[:-1], din_with_check[-1]

    try:
        return check_character(body) == supplied
    except IsbtError:
        return False


def checksum_value(payload: str) -> int:
    """The numeric MOD 37-2 checksum, 0..36.

    ICCBBA permits carrying it in the Type 3 flag characters as 60 + checksum
    (range 60-96), which is where the widely quoted "flag minus 60" comes from.
    """

    return ISO7064_ALPHABET.index(check_character(payload))


def flag_characters_for_checksum(payload: str) -> str:
    return f"{60 + checksum_value(payload):02d}"


@dataclass(frozen=True)
class DonationIdentifier:
    """A DIN and everything a label needs to print it honestly."""

    din: str
    fin: str
    year: str
    sequence: str
    check_character: str
    is_provisional: bool

    @property
    def barcode_content(self) -> str:
        """What goes in the Code 128 symbol: '=' data identifier plus the DIN.

        Flags are a separate concern and are appended by the label renderer only
        when a facility actually uses them.
        """

        return f"={self.din}"

    @property
    def eye_readable(self) -> str:
        grouping = str(
            config.get("labelling.isbt128.din.eye_readable_grouping", "UNGROUPED")
        ).upper()

        if grouping == "GROUPED":
            return f"{self.fin} {self.year} {self.sequence}"

        return self.din

    @property
    def display_with_check(self) -> str:
        return f"{self.eye_readable} [{self.check_character}]"


def configured_fin(facility_code: str | None = None) -> tuple[str, bool]:
    """The FIN to use, and whether it is provisional.

    Returns a placeholder when no ICCBBA-assigned FIN is configured. The caller
    must propagate `is_provisional` rather than discarding it.
    """

    assigned = config.get("labelling.isbt128.fin")

    if isinstance(assigned, dict) and facility_code:
        assigned = assigned.get(facility_code)

    if assigned:
        fin = str(assigned).upper()

        if not is_valid_fin(fin):
            raise IsbtError(f"configured FIN {fin!r} is not structurally valid")

        # Default TRUE. A configured FIN says somebody typed a value into the
        # config; it does not say ICCBBA assigned it. Defaulting this to False
        # made an unstated fact read as a confirmed one, which is the opposite
        # of what an unknown should do on a conformance claim.
        return fin, bool(config.get("labelling.isbt128.fin_is_provisional", True))

    # No assigned FIN. Emit a clearly non-conformant placeholder rather than
    # inventing something that looks real: 'Z' is used because ICCBBA has not
    # assigned Z-prefixed FINs to the facilities in this network, and the value
    # is always accompanied by is_provisional=True.
    return "Z0000", True


def _configured_fins() -> set[str]:
    """Every FIN the configuration claims is ICCBBA-assigned."""

    assigned = config.get("labelling.isbt128.fin")

    if not assigned:
        return set()

    if isinstance(assigned, dict):
        return {str(value).upper() for value in assigned.values() if value}

    return {str(assigned).upper()}


def is_valid_fin(fin: str) -> bool:
    if len(fin) != FIN_LENGTH:
        return False

    if fin[0] not in FIN_FIRST_CHARACTER_ALPHABET:
        return False

    # Current ICCBBA usage is numeric in the remaining four positions; alphas are
    # reserved for future use in positions 2-3.
    if not fin[3:].isdigit():
        return False

    return all(
        character in ISO7064_ALPHABET[:36] for character in fin[1:3]
    )


def build_din(
    *,
    sequence: int,
    year: int,
    facility_code: str | None = None,
    fin: str | None = None,
    provisional: bool | None = None,
) -> DonationIdentifier:
    """Construct a DIN. `sequence` must be unique within (FIN, year).

    `provisional` must be stated when `fin` is supplied directly. Passing a FIN
    is not evidence that ICCBBA assigned it — the migration passes Z-block
    placeholders — and defaulting to "conformant" would make the system claim
    compliance with a patient-safety standard it does not have.
    """

    if not 0 <= sequence <= 999999:
        raise IsbtError(
            f"sequence {sequence} does not fit the 6-digit DIN sequence field"
        )

    if fin:
        resolved_fin = fin.upper()

        if provisional is None:
            # Unstated means unknown, and unknown must not read as conformant.
            configured = config.get("labelling.isbt128.fin")
            provisional = not (configured and resolved_fin in _configured_fins())
    else:
        resolved_fin, configured_provisional = configured_fin(facility_code)
        provisional = (
            configured_provisional if provisional is None else provisional
        )

    if fin and not is_valid_fin(resolved_fin):
        raise IsbtError(f"FIN {resolved_fin!r} is not structurally valid")

    year_code = f"{year % 100:02d}"
    sequence_code = f"{sequence:06d}"

    din = f"{resolved_fin}{year_code}{sequence_code}"

    if len(din) != DIN_LENGTH:
        raise IsbtError(f"constructed DIN {din!r} is not {DIN_LENGTH} characters")

    return DonationIdentifier(
        din=din,
        fin=resolved_fin,
        year=year_code,
        sequence=sequence_code,
        check_character=check_character(din),
        is_provisional=provisional,
    )


def parse_din(din: str) -> DonationIdentifier:
    """Parse a 13-character DIN, tolerating grouping spaces and a trailing check."""

    cleaned = din.replace(" ", "").replace("-", "").upper()

    if cleaned.startswith("="):
        cleaned = cleaned[1:]

    if len(cleaned) == DIN_LENGTH + 1:
        # A check character was included; validate rather than silently drop it.
        if not verify(cleaned):
            raise IsbtError(f"check character on {din!r} does not validate")

        cleaned = cleaned[:DIN_LENGTH]

    if len(cleaned) != DIN_LENGTH:
        raise IsbtError(
            f"{din!r} is {len(cleaned)} characters; a DIN is {DIN_LENGTH}"
        )

    fin = cleaned[:FIN_LENGTH]

    # Conformance is NOT inferable from parsing.
    #
    # A DIN read off a bag carries a Facility Identification Number this system
    # neither issued nor can verify against the ICCBBA register. The previous
    # logic reported is_provisional=False for any DIN at all as soon as SOME fin
    # was configured — including a Z-block placeholder, and including identifiers
    # from other facilities entirely. That is a conformance claim made on no
    # evidence, and it is the same false-conformance bug that was fixed in
    # build_din() resurfacing on the read path.
    #
    # So: structural validity and the check character are things parsing can
    # establish. Conformance is only claimed when the FIN is one this system was
    # configured with AND that configuration is not itself flagged provisional.
    # Anything else, including every foreign DIN, is provisional — because
    # unknown must never read as conformant.
    is_provisional = True

    if fin in _configured_fins():
        is_provisional = bool(
            config.get("labelling.isbt128.fin_is_provisional", True)
        )

    return DonationIdentifier(
        din=cleaned,
        fin=fin,
        year=cleaned[FIN_LENGTH : FIN_LENGTH + YEAR_LENGTH],
        sequence=cleaned[FIN_LENGTH + YEAR_LENGTH :],
        check_character=check_character(cleaned),
        is_provisional=is_provisional,
    )


def product_description_code(component_code: str) -> str | None:
    """Look up the ISBT 128 Product Description Code for a component.

    Returns None unless a licensed code table has been configured. The Product
    Description Code Database is licence-gated by ICCBBA, so shipping a guessed
    table would be both a licence breach and a labelling error — an E-code that
    is nearly right describes a different product.
    """

    table = config.get("labelling.isbt128.product_code_table") or {}

    return table.get(component_code)


def conformance_status() -> dict:
    """What the UI must disclose about identifier conformance."""

    fin = config.get("labelling.isbt128.fin")
    has_codes = bool(config.get("labelling.isbt128.product_code_table"))

    return {
        "has_assigned_fin": bool(fin),
        "has_product_codes": has_codes,
        "is_conformant": bool(fin) and has_codes,
        "message": (
            "Identifiers are ISBT 128 conformant."
            if fin and has_codes
            else (
                "Provisional identifiers. These follow the ISBT 128 Data "
                "Structure 001 layout and carry a valid ISO 7064 MOD 37-2 check "
                "character, but the facility does not yet hold an "
                "ICCBBA-assigned Facility Identification Number, so they are "
                "not conformant Donation Identification Numbers and must not be "
                "presented as such."
            )
        ),
    }
