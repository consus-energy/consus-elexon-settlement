"""Inbound handlers: feedback becomes state changes.

Each handler matches the Handler protocol -- (header, body) in, nothing out --
and is registered against a file type in app.build_router.

The correlation problem is not symmetric, which shapes this module:

    E0281 acceptance  carries our own filename and sequence number (N0301,
                      N0198), so the file is found directly.

    E0091 rejection   carries neither. Only the authorisation ids, reference
                      code and effective dates, so the notification is found
                      by its business key.

    E0521 WMAN        carries settlement date and period, and optionally the
                      BM Units. No file reference at all.

That asymmetry means the business key must be unique, or a rejection could
match more than one notification. Migration 0003 adds that constraint; until
it is applied, _find_notification raises on ambiguity rather than guessing.

Handlers raise on failure. The router records the exception on Received and
carries on -- a handler failure is our problem and must not change the
acknowledgement we send.
"""

from __future__ import annotations

from psycopg import Connection

from .. import db
from ..idd.file import Header, Node
from . import ecvaa


class HandlerError(RuntimeError):
    """Feedback arrived that we could not act on.

    Almost always means correlation failed: feedback for something we have no
    record of sending. Worth alerting on, because either our records are wrong
    or the feedback is not ours.
    """


class EcvaaHandlers:
    """The three ECVAA feedback flows that change what we believe.

    Holds a connection factory rather than a connection: handlers run from a
    poller that may be long-lived, and a connection held open for hours is a
    connection that will be dead when it matters.
    """

    def __init__(self, connect) -> None:
        self._connect = connect

    # --- E0281 acceptance ---------------------------------------------------

    def ecvn_acceptance(self, header: Header, body: list[Node]) -> None:
        """The notification passed validation and is in effect.

        Our filename comes back in the feedback, so the file is found
        directly. The notification is then the one in that file -- EDN has
        cardinality 1, so a file carries exactly one.
        """
        acceptance = ecvaa.parse_acceptance(body)

        with self._connect() as conn:
            file_id = db.find_file_by_filename(conn, acceptance.our_filename)
            if file_id is None:
                raise HandlerError(
                    f"acceptance for {acceptance.our_filename}, which we have no record "
                    f"of sending (reference {acceptance.reference_code})"
                )

            notification_id = db.find_notification_in_file(conn, file_id)
            if notification_id is None:
                raise HandlerError(
                    f"file {acceptance.our_filename} has no notification recorded"
                )

            db.accept_notification(conn, notification_id)
            db.record_acceptance_detail(
                conn,
                notification_id,
                transaction_id=acceptance.transaction_id,
                first_effective_period=acceptance.first_effective_period,
            )

    # --- E0091 rejection ----------------------------------------------------

    def ecvn_rejection(self, header: Header, body: list[Node]) -> None:
        """The notification failed validation and is not in effect.

        No filename in this flow, so correlation is by business key. The
        reason is 80 characters of free text and is recorded verbatim: it is
        the only explanation we get, and paraphrasing it into a category would
        lose detail we may need when querying it.
        """
        rejection = ecvaa.parse_rejection(body)

        with self._connect() as conn:
            notification_id = _find_notification(conn, rejection)
            db.reject_notification(
                conn,
                notification_id,
                reason=rejection.reason,
                # E0091's CD2 names periods but carries no per-period reason:
                # N0187 is on EDX only. Named periods therefore inherit the
                # notification reason rather than getting a specific one.
                periods={p.settlement_period: rejection.reason for p in rejection.periods},
            )

    # --- E0521 WMAN exception ----------------------------------------------

    def wman_exception(self, header: Header, body: list[Node]) -> None:
        """A wholesale market activity notification was rejected.

        Two levels of rejection and they mean different things. A period-level
        reason with no named units rejects the whole period. Named units
        reject only those. Treating either as total would be wrong in one
        direction, so both paths are handled explicitly.

        This is the most urgent of the three: a rejected WMAN means SVAA does
        not know we were active, so the deviation is not measured, whatever
        the ECVN says.
        """
        exception = ecvaa.parse_wman_exception(body)

        with self._connect() as conn:
            if exception.units:
                for unit in exception.units:
                    rejected = db.reject_wman(
                        conn,
                        settlement_date=exception.settlement_date,
                        settlement_period=exception.settlement_period,
                        reason=unit.reason,
                        bmu_id=unit.bmu_id,
                    )
                    if not rejected:
                        raise HandlerError(
                            f"WMAN rejection for {unit.bmu_id} on "
                            f"{exception.settlement_date} period "
                            f"{exception.settlement_period}, which we have no record of"
                        )
                return

            rejected = db.reject_wman(
                conn,
                settlement_date=exception.settlement_date,
                settlement_period=exception.settlement_period,
                reason=exception.reason or "rejected, no reason given",
            )
            if not rejected:
                raise HandlerError(
                    f"WMAN rejection for {exception.settlement_date} period "
                    f"{exception.settlement_period}, which we have no record of"
                )


def _find_notification(conn: Connection, rejection: ecvaa.EcvnRejection) -> int:
    """Locate the notification a rejection refers to, by business key.

    Raises rather than guessing when the key matches more than one row. A
    rejection applied to the wrong notification would mark a live position as
    failed and leave the failed one looking healthy, which is worse than an
    alert.
    """
    ids = db.find_notifications_by_reference(
        conn,
        ecvnaa_id=rejection.ecvnaa_id,
        ecvn_ecvnaa_id=rejection.ecvn_ecvnaa_id,
        reference_code=rejection.reference_code,
        effective_from=rejection.effective_from,
    )
    if not ids:
        raise HandlerError(
            f"rejection for reference {rejection.reference_code} effective "
            f"{rejection.effective_from}, which we have no record of sending"
        )
    if len(ids) > 1:
        raise HandlerError(
            f"rejection for reference {rejection.reference_code} matches "
            f"{len(ids)} notifications: {ids}. Reference codes must be unique "
            f"per authorisation and effective date."
        )
    return ids[0]