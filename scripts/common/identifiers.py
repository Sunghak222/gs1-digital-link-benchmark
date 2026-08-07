"""GS1 identifier helpers: check digits, GTIN padding, demo GLN issuance.

GS1 check digit (GTIN-8/12/13/14, GLN, SSCC all share it): from the rightmost
payload digit leftward, weights alternate 3, 1, 3, 1, ...; the check digit
brings the weighted sum up to a multiple of 10.

Place identifiers use GS1's demo/documentation prefix 952 so synthetic numbers
can never collide with a real company prefix.
"""
from __future__ import annotations

DEMO_PREFIX = "952"


def check_digit(payload: str) -> int:
    """Check digit for the digit-string ``payload`` (identifier without its last digit)."""
    if not payload.isdigit():
        raise ValueError(f"payload must be digits only: {payload!r}")
    total = 0
    for i, ch in enumerate(reversed(payload)):
        weight = 3 if i % 2 == 0 else 1
        total += int(ch) * weight
    return (10 - total % 10) % 10


def is_valid(code: str) -> bool:
    """True if ``code`` (GTIN-8/12/13/14 or GLN-13) has a correct check digit."""
    if not code.isdigit() or len(code) not in (8, 12, 13, 14):
        return False
    return check_digit(code[:-1]) == int(code[-1])


def gtin_to_14(code: str) -> str:
    """Zero-pad a GTIN-8/12/13 to GTIN-14 for the Digital Link ``01`` path.

    Leading zeros do not disturb the check digit (they add 0 to the weighted
    sum regardless of position), so the padded code stays valid.
    """
    if not is_valid(code):
        raise ValueError(f"invalid GTIN: {code!r}")
    return code.zfill(14)


def make_demo_gln(serial: int) -> str:
    """GLN-13 under the 952 demo prefix: 952 + 9-digit serial + check digit."""
    if not 0 <= serial <= 999_999_999:
        raise ValueError("serial must fit in 9 digits")
    payload = f"{DEMO_PREFIX}{serial:09d}"
    return payload + str(check_digit(payload))
