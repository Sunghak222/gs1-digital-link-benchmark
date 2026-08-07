import pytest

from scripts.common.identifiers import check_digit, gtin_to_14, is_valid, make_demo_gln

# Real-world GTINs with known-correct check digits:
#   4006381333931  Stabilo (GS1 docs classic), 3017620422003 Nutella,
#   8801043014465  Shin Ramyun (880 = GS1 Korea), 88800026977779 demo-server OB Lager (GTIN-14)
REAL_GTINS = ["4006381333931", "3017620422003", "8801043014465", "88800026977779"]


@pytest.mark.parametrize("code", REAL_GTINS)
def test_real_gtins_validate(code):
    assert is_valid(code)


@pytest.mark.parametrize("code", REAL_GTINS)
def test_check_digit_roundtrip(code):
    assert check_digit(code[:-1]) == int(code[-1])


def test_tampered_digit_fails():
    code = "3017620422003"
    bad = code[:-1] + str((int(code[-1]) + 1) % 10)
    assert not is_valid(bad)


def test_gtin_to_14_pads_and_stays_valid():
    padded = gtin_to_14("3017620422003")
    assert padded == "03017620422003"
    assert is_valid(padded)


def test_gtin_to_14_rejects_invalid():
    with pytest.raises(ValueError):
        gtin_to_14("1234567890123")


def test_demo_gln_shape_and_validity():
    gln = make_demo_gln(1)
    assert len(gln) == 13
    assert gln.startswith("952000000001")
    assert is_valid(gln)


def test_demo_gln_unique_per_serial():
    assert len({make_demo_gln(i) for i in range(100)}) == 100


def test_demo_gln_serial_out_of_range():
    with pytest.raises(ValueError):
        make_demo_gln(10**9)
