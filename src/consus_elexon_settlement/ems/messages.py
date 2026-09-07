"""Parsing what the EMS publishes.

Pure functions over the Pub/Sub push envelope. No HTTP, no database, so the
message contract can be tested without either -- and the contract is the part
most likely to drift, because it is the seam between two systems owned by
different code.

The envelope Pub/Sub delivers:

    {"message": {"data": "<base64 of our JSON>",
                 "messageId": "...", "publishTime": "..."},
     "subscription": "projects/.../subscriptions/..."}

Inside, one of two shapes, distinguished by "kind":

    trading    a decision before Gate Closure: what we expect, what we traded
    delivered  what we actually delivered, at D+1

Two kinds rather than one because they are known at different times and have
different deadlines. Forcing them into one shape would mean half the fields
being absent half the time, and absence is how errors hide.

SEPARATION OF CONCERNS. The EMS owns the numbers -- forecast, trade, outturn --
and publishes them as facts. This module checks that a message is well formed,
not that it is right. A wrong expected volume parses cleanly and settles
wrongly, and that check belongs in the EMS.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
from decimal import Decimal, InvalidOperation

from .. import intents

TRADING = "trading"
DELIVERED = "delivered"
DEFAULT_SEV = "default_sev"


class MessageError(ValueError):
    """The message could not be understood.

    Not retryable: redelivering a malformed message produces the same result
    forever, which is how a poison pill blocks a subscription. The caller
    acknowledges and records rather than nacking.
    """


def unwrap(envelope: dict) -> dict:
    """Pull our payload out of the Pub/Sub push envelope.

    Raises rather than returning None on anything unexpected. A subscription
    delivering an envelope we do not recognise is misconfigured, and guessing
    at it would mean acting on a message meant for something else.
    """
    message = envelope.get("message")
    if not isinstance(message, dict):
        raise MessageError("no 'message' object in the push envelope")

    data = message.get("data")
    if not data:
        # An empty message is not a valid intent. Pub/Sub permits attribute-
        # only messages, but the EMS does not send them.
        raise MessageError("push message carries no data")

    try:
        decoded = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MessageError(f"data is not valid base64: {exc}") from exc

    try:
        payload = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise MessageError(f"data is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise MessageError(f"expected a JSON object, got {type(payload).__name__}")

    return payload


def message_id(envelope: dict) -> str:
    """Pub/Sub's own id, for logging only.

    Deliberately NOT used for idempotency. Pub/Sub assigns a new id to a
    republished message, so two ids can carry the same decision -- and the EMS
    can also send the same decision twice by mistake. The natural key on the
    intent covers both cases; this covers neither.
    """
    return str(envelope.get("message", {}).get("messageId", "unknown"))


def kind(payload: dict) -> str:
    value = payload.get("kind")
    if value not in (TRADING, DELIVERED, DEFAULT_SEV):
        raise MessageError(
            f"'kind' must be one of {TRADING!r}, {DELIVERED!r}, "
            f"{DEFAULT_SEV!r}, got {value!r}"
        )
    return value


def to_intent(payload: dict) -> intents.Intent:
    """A trading decision.

    Every field is required. There are no defaults, because a default for a
    volume is a number nobody chose, and a default for an authorisation is the
    wrong counterparty.
    """
    try:
        return intents.Intent(
            settlement_date=_date(payload, "settlement_date"),
            settlement_period=_int(payload, "settlement_period"),
            bmu_id=_str(payload, "bmu_id"),
            revision=_int(payload, "revision", default=1),
            expected_mwh=_decimal(payload, "expected_mwh"),
            contracted_mwh=_decimal(payload, "contracted_mwh"),
            ecvnaa_id=_str(payload, "ecvnaa_id"),
            ecvn_ecvnaa_id=_str(payload, "ecvn_ecvnaa_id"),
        )
    except ValueError as exc:
        # Intent.__post_init__ raises ValueError on a period out of range or
        # a volume with too much precision. Re-raised as MessageError so the
        # caller treats it as unretryable rather than as a transient fault.
        raise MessageError(str(exc)) from exc


def to_delivered(payload: dict) -> intents.DeliveredIntent:
    """A delivered volume, at D+1.

    delivered_mwh is our deviation, not the metered volume. SVAA holds the
    metered data already and uses this to allocate how much of it was ours
    (BSCP602 Appendix 3.6).
    """
    try:
        return intents.DeliveredIntent(
            settlement_date=_date(payload, "settlement_date"),
            settlement_period=_int(payload, "settlement_period"),
            bmu_id=_str(payload, "bmu_id"),
            revision=_int(payload, "revision", default=1),
            gsp_group_id=_str(payload, "gsp_group_id"),
            import_msid=_int(payload, "import_msid"),
            export_msid=_optional_int(payload, "export_msid"),
            delivered_mwh=_decimal(payload, "delivered_mwh"),
        )
    except ValueError as exc:
        raise MessageError(str(exc)) from exc


def to_default_sev(payload: dict) -> intents.DefaultSevIntent:
    """A standing expected volume profile.

    Periods arrive as an object keyed by period number rather than a list,
    because a Default may legitimately be partial and a sparse list would be
    ambiguous about which periods it covered.

        {"periods": {"1": "-0.0800", "2": "-0.0800", ...}}

    JSON object keys are strings, so the period numbers are parsed rather than
    read as integers.
    """
    periods_raw = payload.get("periods")
    if not isinstance(periods_raw, dict) or not periods_raw:
        raise MessageError("'periods' must be a non-empty object of period to volume")

    periods = []
    for key, value in periods_raw.items():
        try:
            period = int(key)
        except (TypeError, ValueError) as exc:
            raise MessageError(
                f"period key {key!r} is not an integer"
            ) from exc

        if isinstance(value, float):
            raise MessageError(
                f"period {period} volume must be a string, not a JSON number: "
                f"a float carries binary rounding into a settled volume."
            )
        try:
            periods.append((period, Decimal(str(value))))
        except InvalidOperation as exc:
            raise MessageError(
                f"period {period} volume is not a decimal: {value!r}"
            ) from exc

    try:
        return intents.DefaultSevIntent(
            effective_from=_date(payload, "effective_from"),
            bmu_id=_str(payload, "bmu_id"),
            revision=_int(payload, "revision", default=1),
            periods=tuple(sorted(periods)),
        )
    except ValueError as exc:
        raise MessageError(str(exc)) from exc


# --- field readers -----------------------------------------------------------
#
# Each names the field it failed on. A message rejected with 'invalid input'
# tells whoever is debugging the EMS nothing at all.


def _str(payload: dict, field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise MessageError(f"{field!r} must be a non-empty string, got {value!r}")
    return value


def _int(payload: dict, field: str, default: int | None = None) -> int:
    if field not in payload and default is not None:
        return default
    value = payload.get(field)
    # bool is an int in Python, and True would silently become 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise MessageError(f"{field!r} must be an integer, got {value!r}")
    return value


def _optional_int(payload: dict, field: str) -> int | None:
    if payload.get(field) is None:
        return None
    return _int(payload, field)


def _date(payload: dict, field: str) -> dt.date:
    value = _str(payload, field)
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise MessageError(
            f"{field!r} must be an ISO date such as 2026-09-15, got {value!r}"
        ) from exc


def _decimal(payload: dict, field: str) -> Decimal:
    """A volume, as a string.

    Strings, not JSON numbers. A JSON number is a float, and a float carries
    binary rounding into an MWh volume that is settled to four decimal places.
    The EMS sends "-0.1200"; sending -0.12 as a number would be accepted by
    json.loads and wrong by the time it reached the wire.
    """
    value = payload.get(field)
    if isinstance(value, float):
        raise MessageError(
            f"{field!r} must be a string, not a JSON number: a float carries "
            f"binary rounding into a settled volume. Send \"{value}\" quoted."
        )
    if not isinstance(value, (str, int)):
        raise MessageError(f"{field!r} must be a decimal string, got {value!r}")

    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise MessageError(f"{field!r} is not a decimal: {value!r}") from exc