"""Seed one channel and archive one file, so the DR test has something real
to recover. Run once against the test project."""
import datetime as dt, os
from consus_elexon_settlement import db
from consus_elexon_settlement.archive import GcsArchive
from consus_elexon_settlement.flows.wman import ActiveUnit, Wman, to_nodes, FILE_TYPE
from consus_elexon_settlement.idd import spec
from consus_elexon_settlement.outbound.sender import Sender
from consus_elexon_settlement.outbound.transport import LocalTransport, EncryptedTransport, NullCipher
from pathlib import Path

dsn = os.environ["CONSUS_SETTLEMENT_DSN"]

with db.connect(dsn) as conn:
    channel = db.ensure_channel(conn, "VT", "CONSUSVT", "EC", "UKDC", "TST1")
    print("channel", channel.id, "next sequence", channel.next_sequence)

sender = Sender(
    connect=lambda: db.connect(dsn),
    archive=GcsArchive(bucket_name=os.environ["CONSUS_ARCHIVE_BUCKET"]),
    transport=EncryptedTransport(
        inner=LocalTransport(outbox=Path("/tmp/out"), inbox=Path("/tmp/in")),
        cipher=NullCipher()),
)

sent = sender.send(
    channel=channel,
    flow=spec.SPEC.flows[FILE_TYPE],
    body=to_nodes(Wman(dt.date(2026, 9, 15), 37, (ActiveUnit("V__FCNRG001"),))),
)
print("file", sent.file_id, sent.filename, "seq", sent.sequence_number)
print("archived at", sent.gcs_uri)