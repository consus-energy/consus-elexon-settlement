"""The message contract with the EMS.

Pure functions, no HTTP and no database, because this is the seam between two
systems owned by different code -- and a seam is where drift happens. These
tests are as much a specification of what the EMS must send as a check on what
we parse.

The rule underneath: a malformed message is NOT retryable. Redelivering it
produces the same failure forever, which is how a poison pill blocks
everything behind it. So MessageError is the signal to acknowledge and record
rather than to nack, and every rejection here names the field it failed on --
'invalid input' tells whoever is debugging the EMS nothing.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
from decimal import Decimal

import pytest

from consus_elexon_settlement.ems import messages

DATE = "2026-09-15"
PERIOD = 37
BMU = "V__ACNRG001"


def envelope(payload: dict, message_id: str = "msg-1") -> dict:
    """What Pub/Sub actually posts: our JSON, base64'd, inside a wrapper."""
    return {
        "message": {
            "data": base64.b64encode(json.dumps(payload).encode()).decode(),
            "messageId": message_id,
            "publishTime": "2026-09-15T12:00:00Z",
        },
        "subscription": "projects/x/subscriptions/settlement-intents",
    }


def a_trading_payload(**overrides) -> dict:
    defaults = {
        "kind": "trading",
        "settlement_date": DATE,
        "settlement_period": PERIOD,
        "bmu_id": BMU,
        "expected_mwh": "-0.1200",
        "contracted_mwh": "0.400",
        "ecvnaa_id": "AUTH000001",
        "ecvn_ecvnaa_id": "AUTH000001",
    }
    return {**defaults, **overrides}


def a_delivered_payload(**overrides) -> dict:
    defaults = {
        "kind": "delivered",
        "settlement_date": DATE,
        "settlement_period": PERIOD,
        "bmu_id": BMU,
        "gsp_group_id": "_A",
        "import_msid": 1300035399160,
        "delivered_mwh": "0.1000",
    }
    return {**defaults, **overrides}


# --- the envelope ------------------------------------------------------------

def test_unwrap_returns_the_payload():
    payload = a_trading_payload()
    assert messages.unwrap(envelope(payload)) == payload


def test_message_id_is_for_logging_only():
    """Deliberately not used for idempotency.

    Pub/Sub assigns a new id to a republished message, so two ids can carry
    the same decision -- and the EMS can also send the same decision twice by
    mistake. The natural key on the intent covers both; this covers neither.
    """
    assert messages.message_id(envelope({}, message_id="abc")) == "abc"


def test_message_id_survives_a_broken_envelope():
    """Used in the log line that reports the parse failure, so it must not
    itself fail on the message it is describing."""
    assert messages.message_id({}) == "unknown"


def test_an_envelope_with_no_message_is_rejected():
    with pytest.raises(messages.MessageError, match="no 'message' object"):
        messages.unwrap({"subscription": "x"})


def test_an_empty_message_is_rejected():
    """Pub/Sub permits attribute-only messages. The EMS does not send them,
    so one arriving means something is misconfigured."""
    with pytest.raises(messages.MessageError, match="carries no data"):
        messages.unwrap({"message": {"messageId": "1"}})


def test_data_that_is_not_base64_is_rejected():
    with pytest.raises(messages.MessageError, match="not valid base64"):
        messages.unwrap({"message": {"data": "not base64!!", "messageId": "1"}})


def test_data_that_is_not_json_is_rejected():
    bad = base64.b64encode(b"{not json").decode()
    with pytest.raises(messages.MessageError, match="not valid JSON"):
        messages.unwrap({"message": {"data": bad, "messageId": "1"}})


def test_a_json_array_is_rejected():
    """Valid JSON, wrong shape. Accepting it would fail later with a less
    useful message."""
    array = base64.b64encode(json.dumps([1, 2, 3]).encode()).decode()
    with pytest.raises(messages.MessageError, match="expected a JSON object"):
        messages.unwrap({"message": {"data": array, "messageId": "1"}})


# --- kind --------------------------------------------------------------------

def test_kind_distinguishes_the_two_message_types():
    assert messages.kind(a_trading_payload()) == messages.TRADING
    assert messages.kind(a_delivered_payload()) == messages.DELIVERED


def test_an_unknown_kind_is_rejected():
    """Two kinds because they are known at different times and have different
    deadlines. A third would be a change to the contract, not a message."""
    with pytest.raises(messages.MessageError, match="'kind' must be"):
        messages.kind({"kind": "something-else"})


def test_a_missing_kind_is_rejected():
    with pytest.raises(messages.MessageError, match="'kind' must be"):
        messages.kind({})


# --- trading intents ---------------------------------------------------------

def test_a_trading_message_becomes_an_intent():
    intent = messages.to_intent(a_trading_payload())

    assert intent.settlement_date == dt.date(2026, 9, 15)
    assert intent.settlement_period == PERIOD
    assert intent.bmu_id == BMU
    assert intent.expected_mwh == Decimal("-0.1200")
    assert intent.contracted_mwh == Decimal("0.400")
    assert intent.revision == 1


def test_revision_defaults_to_one():
    """The common case is a first decision. Requiring the EMS to send 1
    explicitly would be noise, and 1 is the only sensible default."""
    payload = a_trading_payload()
    del payload["revision"] if "revision" in payload else None
    assert messages.to_intent(payload).revision == 1


def test_revision_is_carried_when_present():
    assert messages.to_intent(a_trading_payload(revision=3)).revision == 3


@pytest.mark.parametrize("field", [
    "settlement_date", "settlement_period", "bmu_id",
    "expected_mwh", "contracted_mwh", "ecvnaa_id", "ecvn_ecvnaa_id",
])
def test_every_field_is_required(field):
    """No defaults. A default for a volume is a number nobody chose, and a
    default for an authorisation is the wrong counterparty."""
    payload = a_trading_payload()
    del payload[field]

    with pytest.raises(messages.MessageError, match=field):
        messages.to_intent(payload)


def test_a_float_volume_is_rejected_with_an_explanation():
    """The one rejection most likely to confuse whoever wrote the publisher.

    A JSON number is a float, and a float carries binary rounding into a
    volume settled to four decimal places. The message says so and shows the
    quoted form, because 'must be a string' alone invites the reply 'why?'.
    """
    with pytest.raises(messages.MessageError, match="binary rounding"):
        messages.to_intent(a_trading_payload(expected_mwh=-0.12))


def test_an_integer_volume_is_accepted():
    """Zero is a legitimate volume and arrives as a JSON integer, which
    carries no rounding. Rejecting it would be pedantry."""
    assert messages.to_intent(
        a_trading_payload(contracted_mwh=0)
    ).contracted_mwh == Decimal("0")


def test_a_decimal_string_keeps_its_precision():
    """-0.1200 and -0.12 are the same number but not the same field: the wire
    format is decimal(14,4) and trailing zeros are how the EMS says it means
    four places."""
    intent = messages.to_intent(a_trading_payload(expected_mwh="-0.1200"))
    assert str(intent.expected_mwh) == "-0.1200"


def test_a_malformed_date_names_the_format():
    with pytest.raises(messages.MessageError, match="ISO date"):
        messages.to_intent(a_trading_payload(settlement_date="15/09/2026"))


def test_a_string_period_is_rejected():
    """'37' and 37 are different in JSON, and accepting both would hide a
    publisher sending everything as strings."""
    with pytest.raises(messages.MessageError, match="settlement_period"):
        messages.to_intent(a_trading_payload(settlement_period="37"))


def test_a_boolean_period_is_rejected():
    """bool is an int in Python, so True would silently become period 1."""
    with pytest.raises(messages.MessageError, match="settlement_period"):
        messages.to_intent(a_trading_payload(settlement_period=True))


def test_a_period_out_of_range_is_rejected():
    """Raised by Intent.__post_init__ and re-raised as MessageError, so the
    caller treats it as unretryable rather than transient."""
    with pytest.raises(messages.MessageError, match="out of range"):
        messages.to_intent(a_trading_payload(settlement_period=51))


def test_period_fifty_is_accepted():
    """Clock-change days have 50 periods. Never assume 48."""
    assert messages.to_intent(
        a_trading_payload(settlement_period=50)
    ).settlement_period == 50


def test_too_much_precision_is_rejected():
    """decimal(14,4) on the wire. More would be silently truncated by the
    field encoder, which is worse than a rejection here."""
    with pytest.raises(messages.MessageError, match="four decimal places"):
        messages.to_intent(a_trading_payload(expected_mwh="-0.12345"))


def test_an_empty_string_is_not_a_bmu_id():
    with pytest.raises(messages.MessageError, match="bmu_id"):
        messages.to_intent(a_trading_payload(bmu_id=""))


# --- delivered intents -------------------------------------------------------

def test_a_delivered_message_becomes_a_delivered_intent():
    delivered = messages.to_delivered(a_delivered_payload())

    assert delivered.settlement_date == dt.date(2026, 9, 15)
    assert delivered.import_msid == 1300035399160
    assert delivered.delivered_mwh == Decimal("0.1000")
    assert delivered.export_msid is None


def test_export_msid_is_optional():
    """BSCP602 1.1.1: an MSID Pair must contain an Import Metering System but
    need not contain an Export one."""
    assert messages.to_delivered(
        a_delivered_payload(export_msid=None)
    ).export_msid is None


def test_export_msid_is_carried_when_present():
    assert messages.to_delivered(
        a_delivered_payload(export_msid=1300035399161)
    ).export_msid == 1300035399161


@pytest.mark.parametrize("field", [
    "settlement_date", "settlement_period", "bmu_id",
    "gsp_group_id", "import_msid", "delivered_mwh",
])
def test_delivered_fields_are_required(field):
    payload = a_delivered_payload()
    del payload[field]

    with pytest.raises(messages.MessageError, match=field):
        messages.to_delivered(payload)


def test_an_msid_longer_than_thirteen_digits_is_rejected():
    with pytest.raises(messages.MessageError, match="13 digits"):
        messages.to_delivered(a_delivered_payload(import_msid=12345678901234))


def test_a_negative_delivered_volume_is_accepted():
    """CVA convention: negative is Import. A turn-up reduces export or
    increases import, and the sign is how that is expressed."""
    assert messages.to_delivered(
        a_delivered_payload(delivered_mwh="-0.1000")
    ).delivered_mwh == Decimal("-0.1000")


# --- what we deliberately do not check ---------------------------------------

def test_the_gateway_does_not_judge_magnitudes():
    """Separation of concerns. The EMS owns the numbers -- forecast, trade,
    outturn -- and publishes them as facts. A volume that is wrong but well
    formed parses cleanly and settles wrongly, and that check belongs in the
    EMS. Inventing a plausibility rule here would mean owning a decision we
    do not have the information to make.
    """
    huge = messages.to_intent(a_trading_payload(contracted_mwh="9999999.999"))
    assert huge.contracted_mwh == Decimal("9999999.999")


def test_an_unknown_field_is_ignored():
    """Forward compatibility. The EMS adding a field it needs for its own
    reasons should not break a gateway that does not read it."""
    intent = messages.to_intent(
        a_trading_payload(trader_note="closing out the short")
    )
    assert intent.bmu_id == BMU