"""Tests for IDD file construction and parsing."""

import datetime as dt
from decimal import Decimal

import pytest

from consus_elexon_settlement.idd.checksum import file_checksum
from consus_elexon_settlement.idd.file import (
    FileError,
    Header,
    Node,
    build,
    parse,
)
from consus_elexon_settlement.idd.spec import SPEC

ECVN = SPEC["E0041001"]
WMAN = SPEC["E0511001"]

CREATED = dt.datetime(2026, 8, 10, 9, 30, tzinfo=dt.timezone.utc)


def ecvn_header(sequence: int = 1) -> Header:
    return Header(
        file_type="E0041001",
        message_role="D",
        creation_time=CREATED,
        from_role_code="EN",
        from_participant_id="CONSUSEN",
        to_role_code="EC",
        to_participant_id="ECVAA",
        sequence_number=sequence,
        test_flag="TST1",
    )


def ecvn_body(periods: int = 2) -> list[Node]:
    return [
        Node(
            "EDN",
            {
                "N0080": "ECVNAA0001",
                "N0297": "KEY0000001",
                "N0310": "ECVNAA0001",
                "N0077": "REF0000001",
                "N0081": dt.date(2026, 8, 11),
            },
            children=[
                Node("CD9", {"N0201": p, "N0085": Decimal("12.5")})
                for p in range(1, periods + 1)
            ],
        )
    ]


# --- structure --------------------------------------------------------------

def test_record_shape():
    lines = build(ECVN, ecvn_header(), ecvn_body(1)).decode().split("\n")
    assert lines[0] == "AAA|E0041001|D|20260810093000|EN|CONSUSEN|EC|ECVAA|1|TST1|"
    assert lines[1] == "EDN|ECVNAA0001|KEY0000001|ECVNAA0001|REF0000001|20260811||"
    assert lines[2] == "CD9|1|12.5|"
    assert lines[3].startswith("ZZZ|4|")
    assert lines[4] == ""  # trailing LF


def test_every_record_has_n_plus_one_separators():
    lines = build(ECVN, ecvn_header(), ecvn_body(2)).decode().rstrip("\n").split("\n")
    for line in lines:
        assert line.endswith("|")


def test_optional_field_absent_is_empty():
    # N0083 Effective To Date is optional and omitted: two adjacent separators.
    line = build(ECVN, ecvn_header(), ecvn_body(1)).decode().split("\n")[1]
    assert line.endswith("|20260811||")


def test_record_count_includes_header_and_footer():
    payload = build(ECVN, ecvn_header(), ecvn_body(3)).decode()
    # AAA + EDN + 3 x CD9 + ZZZ
    assert payload.split("\n")[-2].startswith("ZZZ|6|")


def test_file_is_ascii():
    build(ECVN, ecvn_header(), ecvn_body()).decode("ascii")


# --- round trip -------------------------------------------------------------

def test_round_trip_ecvn():
    payload = build(ECVN, ecvn_header(), ecvn_body(2))
    header, body = parse(payload, ECVN)

    assert header == ecvn_header()
    assert body[0].record_type == "EDN"
    assert body[0].values["N0080"] == "ECVNAA0001"
    assert body[0].values["N0081"] == dt.date(2026, 8, 11)
    assert "N0083" not in body[0].values
    assert [c.values["N0201"] for c in body[0].children] == [1, 2]
    assert body[0].children[0].values["N0085"] == Decimal("12.5")


def test_round_trip_wman():
    header = Header(
        "E0511001", "D", CREATED, "VT", "CONSUS", "EC", "ECVAA", 7, "TST1"
    )
    body = [
        Node(
            "SDP",
            {"N0200": dt.date(2026, 10, 25), "N0201": 50},
            children=[Node("WMA", {"N0034": "2__ABMU001", "N0652": True})],
        )
    ]
    parsed_header, parsed_body = parse(build(WMAN, header, body), WMAN)
    assert parsed_header == header
    assert parsed_body[0].values["N0201"] == 50
    assert parsed_body[0].children[0].values["N0652"] is True


# --- settlement periods 1-50 ------------------------------------------------

@pytest.mark.parametrize("periods", [46, 48, 50])
def test_clock_change_day_period_counts(periods):
    """Long and short days. Code that assumes 48 breaks twice a year."""
    payload = build(ECVN, ecvn_header(), ecvn_body(periods))
    _, body = parse(payload, ECVN)
    assert len(body[0].children) == periods
    assert body[0].children[-1].values["N0201"] == periods


def test_period_51_will_not_encode():
    body = ecvn_body(0)
    body[0].children.append(Node("CD9", {"N0201": 100, "N0085": Decimal("1")}))
    with pytest.raises(FileError):
        build(ECVN, ecvn_header(), body)


# --- cardinality ------------------------------------------------------------

def test_missing_mandatory_record_rejected():
    with pytest.raises(FileError, match="EDN appears 0 times"):
        build(ECVN, ecvn_header(), [])


def test_two_edn_records_rejected():
    with pytest.raises(FileError, match="EDN appears 2 times"):
        build(ECVN, ecvn_header(), ecvn_body(1) + ecvn_body(1))


def test_wman_requires_at_least_one_bmu():
    header = Header("E0511001", "D", CREATED, "VT", "CONSUS", "EC", "ECVAA", 1)
    body = [Node("SDP", {"N0200": dt.date(2026, 8, 11), "N0201": 1})]
    with pytest.raises(FileError, match="WMA appears 0 times"):
        build(WMAN, header, body)


def test_unknown_item_rejected():
    body = ecvn_body(1)
    body[0].values["N9999"] = "X"
    with pytest.raises(FileError, match="unknown items"):
        build(ECVN, ecvn_header(), body)


# --- header -----------------------------------------------------------------

def test_header_message_role_validated():
    with pytest.raises(FileError):
        Header("E0041001", "X", CREATED, "EN", "C", "EC", "E", 1)


def test_sequence_number_range():
    Header("E0041001", "D", CREATED, "EN", "C", "EC", "E", 999_999_999)
    with pytest.raises(FileError):
        Header("E0041001", "D", CREATED, "EN", "C", "EC", "E", 1_000_000_000)


def test_operational_flag_omitted_is_empty_field():
    header = Header("E0041001", "D", CREATED, "EN", "C", "EC", "E", 1, test_flag=None)
    assert header.to_record().endswith("|1||")


def test_response_header_reverses_participants_and_keeps_sequence():
    inbound = Header(
        "E0281001", "D", CREATED, "EC", "ECVAA", "EN", "CONSUSEN", 42, "TST1"
    )
    reply = inbound.response_header(our_role="EN", our_participant="CONSUSEN")

    assert reply.message_role == "R"
    assert reply.from_role_code == "EN"
    assert reply.to_participant_id == "ECVAA"
    # IDD 2.2.7: creation time and sequence are those of the message replied to.
    assert reply.sequence_number == 42
    assert reply.creation_time == CREATED


def test_file_type_must_match_flow():
    header = Header("E0511001", "D", CREATED, "EN", "C", "EC", "E", 1)
    with pytest.raises(FileError, match="does not match"):
        build(ECVN, header, ecvn_body())


# --- corruption maps to IDD response codes ----------------------------------

def test_bad_header_is_code_1():
    payload = build(ECVN, ecvn_header(), ecvn_body())
    corrupt = payload.replace(b"AAA|E0041001", b"AAA|E0041XX1", 1)
    with pytest.raises(FileError) as exc:
        parse(corrupt, ECVN)
    assert exc.value.response_code == 1


def reseal(lines: list[str]) -> bytes:
    """Rebuild a valid footer over modified records.

    Needed to test body errors in isolation: the footer is verified before the
    body is parsed, so a naive edit trips the checksum check first.
    """
    body = [line.encode("ascii") for line in lines]
    footer = f"ZZZ|{len(body) + 1}|{file_checksum(body)}|"
    return ("\n".join(lines + [footer]) + "\n").encode("ascii")


def test_bad_body_is_code_4():
    lines = build(ECVN, ecvn_header(), ecvn_body(1)).decode().rstrip("\n").split("\n")
    lines[2] = "CD9|1|12.5|99|"  # an extra field on the CD9 record
    with pytest.raises(FileError) as exc:
        parse(reseal(lines[:-1]), ECVN)
    assert exc.value.response_code == 4


def test_invalid_settlement_period_in_inbound_file_is_code_4():
    lines = build(ECVN, ecvn_header(), ecvn_body(1)).decode().rstrip("\n").split("\n")
    lines[2] = "CD9|01|12.5|"  # leading zero is not a valid integer
    with pytest.raises(FileError) as exc:
        parse(reseal(lines[:-1]), ECVN)
    assert exc.value.response_code == 4


def test_bad_footer_is_code_5():
    payload = build(ECVN, ecvn_header(), ecvn_body(1))
    corrupt = payload.rstrip(b"\n").rsplit(b"\n", 1)[0] + b"\nZZZ|4|\n"
    with pytest.raises(FileError) as exc:
        parse(corrupt, ECVN)
    assert exc.value.response_code == 5


def test_wrong_record_count_is_code_6():
    payload = build(ECVN, ecvn_header(), ecvn_body(1)).decode()
    lines = payload.rstrip("\n").split("\n")
    lines[-1] = lines[-1].replace("ZZZ|4|", "ZZZ|9|")
    with pytest.raises(FileError) as exc:
        parse(("\n".join(lines) + "\n").encode(), ECVN)
    assert exc.value.response_code == 6


def test_wrong_checksum_is_code_7():
    payload = build(ECVN, ecvn_header(), ecvn_body(1)).decode()
    lines = payload.rstrip("\n").split("\n")
    count, checksum = lines[-1].split("|")[1:3]
    lines[-1] = f"ZZZ|{count}|{int(checksum) ^ 1}|"
    with pytest.raises(FileError) as exc:
        parse(("\n".join(lines) + "\n").encode(), ECVN)
    assert exc.value.response_code == 7


def test_non_ascii_rejected():
    payload = build(ECVN, ecvn_header(), ecvn_body(1))
    with pytest.raises(FileError):
        parse(payload.replace(b"REF", b"R\xc9F"), ECVN)