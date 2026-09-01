"""Test vectors for IDD s2.2.2 checksum.

Vectors 1-5 are hand-derivable from the pseudocode. The realistic-file vector
is self-consistent only: it locks current behaviour against regression, it does
NOT prove agreement with ECVAA. Replace/augment with a known-good Elexon file
as soon as we have one (PTS slot or a sample from the ECVAA Service Desk).
"""

import pytest

from consus_elexon_settlement.idd.checksum import (
    ChecksumError,
    checksum_of_file,
    file_checksum,
    record_checksum,
    record_count,
    split_records,
)


# --- hand-derivable vectors -------------------------------------------------

def test_empty_record_is_zero():
    assert record_checksum(b"") == 0


def test_exact_four_bytes():
    # 'AAA|' -> 0x41 0x41 0x41 0x7C
    assert record_checksum(b"AAA|") == 0x4141417C


def test_null_padding_of_short_final_section():
    # 'ABCDE' -> 'ABCD' = 0x41424344, 'E\0\0\0' = 0x45000000
    assert record_checksum(b"ABCDE") == 0x41424344 ^ 0x45000000


def test_short_record_is_padded_not_left_aligned():
    # 'A' -> 0x41000000, not 0x00000041
    assert record_checksum(b"A") == 0x41000000


def test_xor_across_records():
    a, b = b"AAA|", b"ABCDE"
    assert file_checksum([a, b]) == record_checksum(a) ^ record_checksum(b)


def test_identical_records_cancel():
    # XOR is self-inverse: this is a real weakness of the algorithm, and the
    # reason record count is checked separately (NACK code 6).
    assert file_checksum([b"AAA|", b"AAA|"]) == 0


def test_result_is_32_bit_unsigned():
    value = file_checksum([b"\x7f\x7f\x7f\x7f"])
    assert 0 <= value <= 0xFFFFFFFF


# --- validation -------------------------------------------------------------

def test_lf_in_record_rejected():
    with pytest.raises(ChecksumError):
        file_checksum([b"AAA|\n"])


def test_non_ascii_rejected():
    with pytest.raises(ChecksumError):
        file_checksum([b"AAA|\xff|"])


def test_str_rejected():
    with pytest.raises(ChecksumError):
        file_checksum(["AAA|"])  # type: ignore[list-item]


# --- file-level -------------------------------------------------------------

HEADER = b"AAA|E0041001|D|20260810093000|EN|CONSUSEN|EC|ECVAA|1|TEST1|"
EDN = b"EDN|ECVNAA0001|KEY0000001|ECVNAA0001|REF0000001|20260811||"
CD9 = b"CD9|1|12.5|"

# Self-consistent regression vector. NOT externally validated.
EXPECTED = 0x31754270


def test_realistic_file_regression():
    assert file_checksum([HEADER, EDN, CD9]) == EXPECTED


def test_checksum_of_file_excludes_trailer():
    payload = b"\n".join([HEADER, EDN, CD9, b"ZZZ|4|%d|" % EXPECTED]) + b"\n"
    assert checksum_of_file(payload) == EXPECTED


def test_trailing_lf_optional():
    body = b"\n".join([HEADER, EDN, CD9, b"ZZZ|4|0|"])
    assert checksum_of_file(body) == checksum_of_file(body + b"\n")


def test_record_count_includes_header_and_footer():
    assert record_count([HEADER, EDN, CD9]) == 4


def test_trailer_must_be_last():
    payload = b"\n".join([HEADER, EDN, CD9]) + b"\n"
    with pytest.raises(ChecksumError):
        checksum_of_file(payload)


def test_split_records_drops_trailing_empty():
    assert split_records(b"AAA|\nZZZ|2|0|\n") == [b"AAA|", b"ZZZ|2|0|"]