"""The header, checked against a file Elexon actually produced.

ParticipantToBSCTestFile.txt is supplied with XSec for round-trip testing:
encrypt it, send it to the Service Desk, they confirm they can decrypt it. Its
checksum is a placeholder -- 123456789, which is a sequence rather than a
computed value -- so it cannot validate the checksum algorithm. That remains
untested against anything real.

What it does validate is the header, which until now was built entirely from
our reading of IDD 2.2.1. This is the first check against something Elexon
produced rather than something we inferred.
"""

import datetime as dt
from pathlib import Path

from consus_elexon_settlement.idd.file import Header

# The participant id in the sample is the literal placeholder '<PARTY ID>',
# which fails validation because angle brackets are outside the IDD character
# set. Substituting a real-shaped id is the point: rejecting the placeholder
# is correct behaviour.
SAMPLE = "AAA|X9999001|D|20071225045823|BP|CONSUSEN|XX|UKDC|1|TR01|"


def test_header_matches_the_elexon_sample_byte_for_byte():
    """Field order, the trailing pipe, and the yyyymmddhhmmss timestamp all
    come from our reading of the IDD. This confirms that reading."""
    header = Header.from_record(SAMPLE)
    assert header.to_record() == SAMPLE


def test_header_fields_parse_as_expected():
    header = Header.from_record(SAMPLE)

    assert header.file_type == "X9999001"
    assert header.message_role == "D"
    assert header.creation_time == dt.datetime(
        2007, 12, 25, 4, 58, 23, tzinfo=dt.timezone.utc
    )
    assert header.from_role_code == "BP"
    assert header.to_role_code == "XX"
    # UKDC is BSC Central Services. Worth pinning: it is not derivable from
    # anything and would be a silent misdirection if wrong.
    assert header.to_participant_id == "UKDC"
    assert header.sequence_number == 1
    # text(4). TR01 confirms the width, which our validation already enforced
    # on the strength of the IDD alone.
    assert header.test_flag == "TR01"


def test_placeholder_participant_is_rejected():
    """'<PARTY ID>' contains angle brackets, outside the IDD character set.
    Rejecting it is correct: a header built with a placeholder would be
    accepted by us and rejected by Elexon, hours later, with an 80-character
    reason."""
    from consus_elexon_settlement.idd.file import FileError

    import pytest
    with pytest.raises(FileError, match="outside the IDD set"):
        Header.from_record(SAMPLE.replace("CONSUSEN", "<PARTY ID>"))