"""Encoding and parsing of IDD data items.

IDD Part 1 section 2.2.3 defines how each data type appears on the wire. The
rules are unforgiving and the failure mode is a NACK 4 ("Syntax Error in Body")
which, per section 2.2.8, *consumes the sequence number* — so a malformed file
costs us a number and forces a resend at the next one. Everything here
therefore raises on violation rather than coercing: a file that fails locally
costs nothing.

Pure functions, no I/O, str in and str out. Bytes happen at file level.

The rules, condensed:

    integer(n)     optional leading '-', no leading zeros, at most n digits
    decimal(n,d)   at most n digits total, d after the point, n-d before;
                   '-' required for negatives; NO trailing zeros; no leading
                   zeros except the '0.x' form
    text(n)        at most n characters, restricted character set, no leading
                   or trailing spaces, may not contain the field separator
    char           a single character from the same set
    boolean        'T' or 'F'
    date           YYYYMMDD
    time           HHMM
    timestamp      HHMMSS
    datetime       YYYYMMDDHHMMSS, always GMT (section 2.2.9)
    null           always empty

An optional field that is absent is the empty string: IDD 2.2.3 says optional
fields are permitted to have nothing between the field separators.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

from .model import DataType, Field

FIELD_SEPARATOR = "|"

# IDD 2.2.3 character table. Note the exclusions: '$', '<', '>', '`', '~' and
# — critically — the field separator '|' itself. A rejection reason echoed
# back into a field we re-emit would corrupt the record structure, so this is
# enforced rather than stripped.
ALLOWED_CHARS = frozenset(
    " !\"#%&'()*+,-./0123456789:;=?@"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_"
    "abcdefghijklmnopqrstuvwxyz{}"
)

DATE_FORMAT = "%Y%m%d"
TIME_FORMAT = "%H%M"
TIMESTAMP_FORMAT = "%H%M%S"
DATETIME_FORMAT = "%Y%m%d%H%M%S"


class FieldError(ValueError):
    """A value cannot be represented, or a wire value is malformed."""


# --- encode -----------------------------------------------------------------

def _encode_text(value: str, data_type: DataType) -> str:
    if not isinstance(value, str):
        raise FieldError(f"{data_type} expects str, got {type(value).__name__}")
    if value != value.strip():
        raise FieldError(f"{data_type} may not have leading or trailing spaces: {value!r}")

    bad = sorted(set(value) - ALLOWED_CHARS)
    if bad:
        raise FieldError(f"{data_type} contains characters outside the IDD set: {bad}")

    limit = 1 if data_type.kind == "char" else data_type.length
    if limit is not None and len(value) > limit:
        raise FieldError(f"{data_type} value is {len(value)} characters, limit {limit}")
    return value


def _encode_integer(value: int, data_type: DataType) -> str:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FieldError(f"{data_type} expects int, got {type(value).__name__}")

    text = str(value)  # str() of an int never produces leading zeros
    digits = len(text.lstrip("-"))
    if data_type.length is not None and digits > data_type.length:
        raise FieldError(f"{data_type} value {value} has {digits} digits, limit {data_type.length}")
    return text


def _encode_decimal(value: Decimal, data_type: DataType) -> str:
    if not isinstance(value, Decimal):
        # float would introduce binary rounding into an MWh volume. Refuse it.
        raise FieldError(f"{data_type} expects Decimal, got {type(value).__name__}")
    if not value.is_finite():
        raise FieldError(f"{data_type} cannot represent {value}")

    total = data_type.length
    places = data_type.decimals
    if total is None or places is None:
        raise FieldError(f"{data_type} is missing its precision")

    sign = "-" if value < 0 else ""
    text = format(abs(value), "f")  # 'f' avoids exponent notation entirely

    if "." in text:
        text = text.rstrip("0").rstrip(".")  # IDD: no trailing zeros
    if text == "":
        text = "0"

    whole, _, fraction = text.partition(".")

    if len(fraction) > places:
        # Do not round silently — the caller decides what precision to send.
        raise FieldError(
            f"{data_type} value {value} has {len(fraction)} decimal places, limit {places}"
        )
    if len(whole) > total - places:
        raise FieldError(
            f"{data_type} value {value} has {len(whole)} digits before the point, "
            f"limit {total - places}"
        )
    if len(whole) + len(fraction) > total:
        raise FieldError(f"{data_type} value {value} exceeds {total} digits")

    if text == "0":
        return "0"  # zero is never signed
    return f"{sign}{text}"


def _encode_boolean(value: bool, data_type: DataType) -> str:
    if not isinstance(value, bool):
        raise FieldError(f"{data_type} expects bool, got {type(value).__name__}")
    return "T" if value else "F"


def _encode_date(value: dt.date, data_type: DataType) -> str:
    if isinstance(value, dt.datetime) or not isinstance(value, dt.date):
        raise FieldError(f"{data_type} expects date, got {type(value).__name__}")
    return value.strftime(DATE_FORMAT)


def _encode_datetime(value: dt.datetime, data_type: DataType) -> str:
    if not isinstance(value, dt.datetime):
        raise FieldError(f"{data_type} expects datetime, got {type(value).__name__}")
    if value.tzinfo is None:
        # IDD 2.2.9: all datetimes are GMT. A naive value is an assumption
        # waiting to be wrong on a British Summer Time day.
        raise FieldError(f"{data_type} requires a timezone-aware datetime")
    return value.astimezone(dt.timezone.utc).strftime(DATETIME_FORMAT)


def _encode_time(value: dt.time, data_type: DataType) -> str:
    if not isinstance(value, dt.time):
        raise FieldError(f"{data_type} expects time, got {type(value).__name__}")
    fmt = TIMESTAMP_FORMAT if data_type.kind == "timestamp" else TIME_FORMAT
    return value.strftime(fmt)


def encode(value: object, data_type: DataType) -> str:
    """Render a Python value as its IDD wire representation."""
    kind = data_type.kind

    if kind == "null":
        if value is not None:
            raise FieldError("null fields must be empty")
        return ""
    if value is None:
        raise FieldError(f"{data_type} has no value; use encode_field for optional items")

    if kind in ("text", "char"):
        return _encode_text(value, data_type)  # type: ignore[arg-type]
    if kind == "integer":
        return _encode_integer(value, data_type)  # type: ignore[arg-type]
    if kind == "decimal":
        return _encode_decimal(value, data_type)  # type: ignore[arg-type]
    if kind == "boolean":
        return _encode_boolean(value, data_type)  # type: ignore[arg-type]
    if kind == "date":
        return _encode_date(value, data_type)  # type: ignore[arg-type]
    if kind == "datetime":
        return _encode_datetime(value, data_type)  # type: ignore[arg-type]
    if kind in ("time", "timestamp"):
        return _encode_time(value, data_type)  # type: ignore[arg-type]

    raise FieldError(f"no encoder for data type {data_type}")


def encode_field(field: Field, value: object) -> str:
    """Encode one data item, honouring its presence marker.

    'N' (unused) always emits empty. 'O' (optional) emits empty for None.
    'M' (mandatory) rejects None.
    """
    if field.presence == "N":
        if value is not None:
            raise FieldError(f"{field.item_id} is unused in this flow and must be empty")
        return ""
    if value is None:
        if field.presence == "O":
            return ""
        raise FieldError(f"{field.item_id} ({field.name}) is mandatory")
    return encode(value, field.data_type)


# --- parse ------------------------------------------------------------------

def parse(text: str, data_type: DataType) -> object:
    """Read an IDD wire value back into a Python value.

    Applies the same validation as encode: an inbound file that breaks the
    rules is a file we should reject loudly, not silently reinterpret.
    """
    kind = data_type.kind

    if kind == "null":
        if text != "":
            raise FieldError(f"null field carries a value: {text!r}")
        return None

    if kind in ("text", "char"):
        return _encode_text(text, data_type)

    if kind == "integer":
        digits = text[1:] if text.startswith("-") else text
        if not digits.isdigit():
            raise FieldError(f"malformed integer: {text!r}")
        if digits.startswith("0") and digits != "0":
            raise FieldError(f"integer has a leading zero: {text!r}")
        value = int(text)
        _encode_integer(value, data_type)  # re-applies the length limit
        return value

    if kind == "decimal":
        try:
            value = Decimal(text)
        except InvalidOperation as exc:
            raise FieldError(f"malformed decimal: {text!r}") from exc
        if not value.is_finite():
            raise FieldError(f"malformed decimal: {text!r}")
        return value

    if kind == "boolean":
        if text not in ("T", "F"):
            raise FieldError(f"boolean must be 'T' or 'F', got {text!r}")
        return text == "T"

    if kind == "date":
        try:
            return dt.datetime.strptime(text, DATE_FORMAT).date()
        except ValueError as exc:
            raise FieldError(f"malformed date: {text!r}") from exc

    if kind == "datetime":
        try:
            naive = dt.datetime.strptime(text, DATETIME_FORMAT)
        except ValueError as exc:
            raise FieldError(f"malformed datetime: {text!r}") from exc
        return naive.replace(tzinfo=dt.timezone.utc)

    if kind in ("time", "timestamp"):
        fmt = TIMESTAMP_FORMAT if kind == "timestamp" else TIME_FORMAT
        try:
            return dt.datetime.strptime(text, fmt).time()
        except ValueError as exc:
            raise FieldError(f"malformed {kind}: {text!r}") from exc

    raise FieldError(f"no parser for data type {data_type}")


def parse_field(field: Field, text: str) -> object:
    """Parse one data item, honouring its presence marker."""
    if text == "":
        if field.presence == "M":
            raise FieldError(f"{field.item_id} ({field.name}) is mandatory but empty")
        return None
    if field.presence == "N":
        raise FieldError(f"{field.item_id} is unused in this flow but carries {text!r}")
    return parse(text, field.data_type)