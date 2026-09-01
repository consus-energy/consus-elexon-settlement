"""Tests for IDD data item encoding, per IDD Part 1 section 2.2.3."""

import datetime as dt
from decimal import Decimal

import pytest

from consus_elexon_settlement.idd.fields import FieldError, encode, encode_field, parse, parse_field
from consus_elexon_settlement.idd.model import DataType, Field

TEXT10 = DataType.parse("text(10)")
INT2 = DataType.parse("integer(2)")
DEC = DataType.parse("decimal(10,3)")
DATE = DataType.parse("date")
DATETIME = DataType.parse("datetime")
BOOL = DataType.parse("boolean")
CHAR = DataType.parse("char")


# --- decimal: the rules most likely to produce a NACK 4 ---------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        (Decimal("12.5"), "12.5"),
        (Decimal("12.500"), "12.5"),      # trailing zeros forbidden
        (Decimal("12.000"), "12"),        # strips to a bare integer
        (Decimal("0.123"), "0.123"),
        (Decimal("0.120"), "0.12"),
        (Decimal("-3.25"), "-3.25"),      # '-' required for negatives
        (Decimal("0"), "0"),
        (Decimal("0.000"), "0"),
        (Decimal("-0.0"), "0"),           # zero is never signed
        (Decimal("1000000.001"), "1000000.001"),  # 10 digits, at the limit
    ],
)
def test_decimal_encoding(value, expected):
    assert encode(value, DEC) == expected


def test_decimal_never_uses_exponent_notation():
    assert encode(Decimal("1E+3"), DEC) == "1000"


def test_decimal_rejects_excess_precision_rather_than_rounding():
    with pytest.raises(FieldError, match="decimal places"):
        encode(Decimal("1.2345"), DEC)


def test_decimal_rejects_too_many_whole_digits():
    with pytest.raises(FieldError, match="before the point"):
        encode(Decimal("12345678.0"), DEC)


def test_decimal_rejects_float():
    # 0.1 + 0.2 is not 0.3 in binary; MWh volumes must not go near float.
    with pytest.raises(FieldError, match="expects Decimal"):
        encode(12.5, DEC)


def test_decimal_rejects_nan():
    with pytest.raises(FieldError):
        encode(Decimal("NaN"), DEC)


# --- text -------------------------------------------------------------------

def test_text_rejects_field_separator():
    # A '|' inside a field would silently create an extra field.
    with pytest.raises(FieldError, match="outside the IDD set"):
        encode("A|B", TEXT10)


@pytest.mark.parametrize("bad", ["$", "<", ">", "`", "~"])
def test_text_rejects_characters_outside_the_idd_table(bad):
    with pytest.raises(FieldError):
        encode(f"AB{bad}", TEXT10)


def test_text_rejects_surrounding_spaces():
    with pytest.raises(FieldError, match="leading or trailing"):
        encode(" ABC", TEXT10)


def test_text_allows_interior_space():
    assert encode("A B", TEXT10) == "A B"


def test_text_length_limit():
    assert encode("ABCDEFGHIJ", TEXT10) == "ABCDEFGHIJ"
    with pytest.raises(FieldError, match="limit 10"):
        encode("ABCDEFGHIJK", TEXT10)


def test_char_is_a_single_character():
    assert encode("P", CHAR) == "P"
    with pytest.raises(FieldError):
        encode("PC", CHAR)


# --- integer ----------------------------------------------------------------

def test_integer_encoding():
    assert encode(1, INT2) == "1"
    assert encode(50, INT2) == "50"
    assert encode(-5, INT2) == "-5"


def test_integer_digit_limit_excludes_sign():
    assert encode(-50, INT2) == "-50"
    with pytest.raises(FieldError, match="limit 2"):
        encode(100, INT2)


def test_bool_is_not_an_integer():
    with pytest.raises(FieldError):
        encode(True, INT2)


def test_parse_rejects_leading_zeros():
    with pytest.raises(FieldError, match="leading zero"):
        parse("01", INT2)


# --- dates and times --------------------------------------------------------

def test_date_encoding():
    assert encode(dt.date(2026, 8, 11), DATE) == "20260811"


def test_datetime_requires_timezone():
    with pytest.raises(FieldError, match="timezone-aware"):
        encode(dt.datetime(2026, 8, 10, 9, 30), DATETIME)


def test_datetime_converted_to_gmt():
    # IDD 2.2.9: datetimes are GMT. 10:30 BST is 09:30 GMT.
    bst = dt.timezone(dt.timedelta(hours=1))
    value = dt.datetime(2026, 8, 10, 10, 30, 0, tzinfo=bst)
    assert encode(value, DATETIME) == "20260810093000"


def test_datetime_rejects_date():
    with pytest.raises(FieldError):
        encode(dt.date(2026, 8, 10), DATETIME)


# --- boolean ----------------------------------------------------------------

def test_boolean_encoding():
    assert encode(True, BOOL) == "T"
    assert encode(False, BOOL) == "F"


def test_boolean_parse_is_strict():
    assert parse("T", BOOL) is True
    with pytest.raises(FieldError):
        parse("Y", BOOL)


# --- presence ---------------------------------------------------------------

MANDATORY = Field("N0080", "ECVNAA Id", TEXT10, "M")
OPTIONAL = Field("N0083", "Effective To Date", DATE, "O")
UNUSED = Field("N0999", "Retired", TEXT10, "N")


def test_optional_absent_is_empty():
    assert encode_field(OPTIONAL, None) == ""


def test_mandatory_absent_raises():
    with pytest.raises(FieldError, match="mandatory"):
        encode_field(MANDATORY, None)


def test_unused_must_be_empty():
    assert encode_field(UNUSED, None) == ""
    with pytest.raises(FieldError, match="unused"):
        encode_field(UNUSED, "X")


def test_parse_empty_optional_is_none():
    assert parse_field(OPTIONAL, "") is None


def test_parse_empty_mandatory_raises():
    with pytest.raises(FieldError, match="mandatory"):
        parse_field(MANDATORY, "")


# --- round trip -------------------------------------------------------------

@pytest.mark.parametrize(
    "value,data_type",
    [
        ("ECVNAA0001", TEXT10),
        (1, INT2),
        (50, INT2),
        (Decimal("12.5"), DEC),
        (Decimal("-3.25"), DEC),
        (dt.date(2026, 8, 11), DATE),
        (True, BOOL),
        (dt.datetime(2026, 8, 10, 9, 30, tzinfo=dt.timezone.utc), DATETIME),
    ],
)
def test_round_trip(value, data_type):
    assert parse(encode(value, data_type), data_type) == value