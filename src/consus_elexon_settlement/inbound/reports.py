"""Reports: received, parsed, archived, not acted on.

Several inbound flows are reports rather than instructions. They confirm what
central systems believe, and we reconcile against them periodically. None
changes the state of a submission, so none needs a domain type until
something actually acts on it.

    E0131  Authorisation Report        what authorisations ECVAA holds
    E0141  Notification Report         our notified position for a day
    E0221  Forward Contract Report     contract position, next 7 days
    P0285  Delivered Volume Exception  volumes SVAA could not process
    P0288  Secondary HH Consumption    metered consumption per BM Unit
    P0333  Baselining Expected Volume  expected volumes SVAA calculated

The scale is the reason for handling them generically: CRA-I014 alone is 15
sub-flows and SAA-I014 is 13, so bespoke domain types would be roughly forty
modelling exercises for data nothing currently reads.

What this handler guarantees is what qualification requires: the file was
received, parsed against its spec, acknowledged, and archived immutably. When
a specific report is needed -- reconciling P0333 against our own expected
volumes, say -- it gets a parser then, reading from the archive rather than
needing to have been modelled in advance.
"""

from __future__ import annotations

from ..idd.file import Header, Node


class ReportHandler:
    """Records that a report arrived, without interpreting it.

    Holds a connection factory rather than a connection, for the same reason
    the other handlers do: a long-lived poller with a connection open for
    hours has a dead connection when it matters.
    """

    def __init__(self, connect) -> None:
        self._connect = connect

    def __call__(self, header: Header, body: list[Node], filename: str) -> None:
        """Parsing already happened in the router, so reaching here means the
        file was structurally valid.

        Recording the record count gives reconciliation something to check
        against, and distinguishes an empty report from one that was never
        processed. Storing the parsed tree instead would mean migrating stored
        data every time a spec changes, for the sake of data nothing reads.
        """
        with self._connect() as conn:
            conn.execute(
                """UPDATE inbound_file
                      SET record_count = %s
                    WHERE filename = %s AND record_count IS NULL""",
                (count_records(body), filename),
            )


def count_records(body: list[Node]) -> int:
    """Records in the parsed tree, at every level.

    Iterative rather than recursive: P0288 nests four deep across a full
    settlement day and a report is not the place to discover a recursion
    limit.
    """
    total = 0
    stack = list(body)
    while stack:
        node = stack.pop()
        total += 1
        stack.extend(node.children)
    return total