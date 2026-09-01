"""Tests for the raw file archive."""

import datetime as dt

import pytest

from consus_elexon_settlement.archive import (
    AlreadyArchived,
    ArchiveError,
    LocalArchive,
    object_key,
)

CREATED = dt.datetime(2026, 8, 10, 9, 30, tzinfo=dt.timezone.utc)


@pytest.fixture
def archive(tmp_path):
    return LocalArchive(root=tmp_path)


def test_key_is_date_partitioned():
    key = object_key("outbound", CREATED, "EN000000000001", 1)
    assert key == "outbound/2026/08/10/EN000000000001-000000000001"


def test_key_uses_gmt_not_local_time():
    # 00:30 BST on 11 August is 23:30 GMT on 10 August. Partitioning by local
    # time would file it under the wrong settlement day.
    bst = dt.timezone(dt.timedelta(hours=1))
    created = dt.datetime(2026, 8, 11, 0, 30, tzinfo=bst)
    assert object_key("outbound", created, "EN000000000001", 1).startswith(
        "outbound/2026/08/10/"
    )


def test_key_requires_timezone():
    with pytest.raises(ArchiveError):
        object_key("outbound", dt.datetime(2026, 8, 10, 9, 30), "EN000000000001", 1)


def test_key_includes_file_id():
    # A corrected file after a header NACK reuses its sequence number and can
    # reuse its filename in principle; the file id keeps keys distinct.
    first = object_key("outbound", CREATED, "EN000000000001", 1)
    second = object_key("outbound", CREATED, "EN000000000001", 2)
    assert first != second


def test_round_trip(archive):
    payload = b"AAA|E0041001|D|20260810093000|EN|CONSUSEN|EC|ECVAA|1|TST1|\n"
    uri = archive.put("outbound/2026/08/10/EN000000000001-000000000001", payload)
    assert archive.get(uri) == payload


def test_write_once(archive):
    key = "outbound/2026/08/10/EN000000000001-000000000001"
    archive.put(key, b"original")

    with pytest.raises(AlreadyArchived):
        archive.put(key, b"replacement")

    # The original survives: settlement evidence is never overwritten.
    assert archive.get(f"file://{archive.root / key}") == b"original"


def test_exists(archive):
    key = "inbound/2026/08/10/EC000000000001-000000000001"
    assert not archive.exists(key)
    archive.put(key, b"x")
    assert archive.exists(key)


def test_missing_object_raises(archive):
    with pytest.raises(ArchiveError, match="not found"):
        archive.get(f"file://{archive.root}/nope")


def test_bytes_are_stored_verbatim(archive):
    # No newline translation, no re-encoding: what we archive is what went on
    # the wire, including the trailing LF the checksum excludes.
    payload = b"AAA|X|\nZZZ|2|0|\n"
    uri = archive.put("outbound/2026/08/10/EN000000000001-000000000001", payload)
    assert archive.get(uri) == payload


@pytest.mark.parametrize("key", ["/etc/passwd", "outbound/../../etc/passwd"])
def test_unsafe_keys_rejected(archive, key):
    with pytest.raises(ArchiveError, match="unsafe"):
        archive.put(key, b"x")
