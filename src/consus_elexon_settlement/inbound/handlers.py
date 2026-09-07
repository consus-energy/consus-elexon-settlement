"""Inbound handlers: feedback becomes state changes.

Each handler matches the Handler protocol -- (header, body, filename) in,
nothing out -- and is registered against a file type in app.build_router.

The correlation problem is not symmetric, which shapes this module:

    E0281 acceptance  carries our own filename and sequence number (N0301,
                      N0198), so the file is found directly.

    E0091 rejection   carries neither. Only the authorisation ids, reference
                      code and effective dates, so the notification is found
                      by its business key.

    E0521 WMAN        carries settlement date and period, and optionally the
                      BM Units. No file reference at all.

That asymmetry means the business key must be unique, or a rejection could
match more than one notification. Migration 0003 adds that constraint;
_find_notification still raises on ambiguity rather than guessing, because a
rejection applied to the wrong notification marks a live position as failed
while the failed one still looks healthy.

EVERY HANDLER THAT CHANGES AN ITEM STATE CALLS BACK TO THE INTENT.

An intent is ACTED only when every flow is ACCEPTED, and acceptance arrives
here. Without the callback an intent never leaves ACTING, which would defeat
the choice that ACTED means accepted rather than sent -- and the sweep would
show every submission as outstanding forever.

The callback is best-effort: it is wrapped so that a failure to reconcile does
not lose the item state change, which is the more important record. A stale
intent is a reporting problem; a lost acceptance is a settlement one.

Handlers raise on failure. The router records the exception on Received and
carries on -- a handler failure is our problem and must not change the
acknowledgement we send, which is about whether the file parsed.
"""

from __future__ import annotations

import logging

from psycopg import Connection

from .. import db, service, states
from ..idd.file import Header, Node
from . import ecvaa, svaa

log = logging.getLogger(__name__)


class HandlerError(RuntimeError):
    """Feedback arrived that we could not act on.

    Almost always means correlation failed: feedback for something we have no
    record of sending. Worth alerting on, because either our records are wrong
    or the feedback is not ours.
    """


def _reconcile(conn: Connection, outbound_file_id: int, what: str) -> None:
    """Re-resolve the intent that owns a file, if any.

    Best-effort by design. The item state change has already been recorded and
    is the record that matters; failing to update the intent leaves a
    reporting inconsistency, not a settlement one. Raising here would undo the
    acceptance we just recorded.

    Files with no intent are normal: registration submissions and anything
    sent manually.
    """
    try:
        state = service.reconcile_intent(conn, outbound_file_id)
    except Exception as exc:  # noqa: BLE001 - logged, not raised
        log.error("could not reconcile intent for %s: %s", what, exc)
        return

    if state:
        log.info("intent for %s is now %s", what, state)


class EcvaaHandlers:
    """The three ECVAA feedback flows that change what we believe.

    Holds a connection factory rather than a connection: handlers run from a
    poller that may be long-lived, and a connection held open for hours is a
    connection that will be dead when it matters.

    The filename is passed to every handler even where it is not yet used. It
    is the only key back to the inbound_file row, and a handler that needs to
    record why it failed will want it.
    """

    def __init__(self, connect) -> None:
        self._connect = connect

    # --- E0281 acceptance ---------------------------------------------------

    def ecvn_acceptance(
        self, header: Header, body: list[Node], filename: str
    ) -> None:
        """The notification passed validation and is in effect.

        Our own filename comes back in the feedback (N0301), so the file is
        found directly rather than by business key. The notification is then
        the one in that file -- EDN has cardinality 1, so a file carries
        exactly one.
        """
        acceptance = ecvaa.parse_acceptance(body)

        with self._connect() as conn:
            file_id = db.find_file_by_filename(conn, acceptance.our_filename)
            if file_id is None:
                raise HandlerError(
                    f"{filename}: acceptance for {acceptance.our_filename}, which we "
                    f"have no record of sending (reference {acceptance.reference_code})"
                )

            notification_id = db.find_notification_in_file(conn, file_id)
            if notification_id is None:
                raise HandlerError(
                    f"{filename}: file {acceptance.our_filename} has no notification "
                    f"recorded against it"
                )

            db.accept_notification(conn, notification_id)
            db.record_acceptance_detail(
                conn,
                notification_id,
                transaction_id=acceptance.transaction_id,
                first_effective_period=acceptance.first_effective_period,
            )
            _reconcile(conn, file_id, acceptance.our_filename)

    # --- E0091 rejection ----------------------------------------------------

    def ecvn_rejection(
        self, header: Header, body: list[Node], filename: str
    ) -> None:
        """The notification failed validation and is not in effect.

        No filename in this flow, so correlation is by business key. The
        reason is 80 characters of free text and is recorded verbatim: it is
        the only explanation we get, and reducing it to a category would lose
        detail needed when querying it with Elexon.
        """
        rejection = ecvaa.parse_rejection(body)

        with self._connect() as conn:
            notification_id = _find_notification(conn, rejection, filename)
            db.reject_notification(
                conn,
                notification_id,
                reason=rejection.reason,
                # E0091's CD2 names periods but carries no per-period reason:
                # N0187 sits on EDX only. Named periods therefore inherit the
                # notification reason rather than getting a specific one.
                periods={
                    p.settlement_period: rejection.reason for p in rejection.periods
                },
            )

            # The rejection found the notification by business key, so the
            # file id has to be read back before the intent can be found.
            file_id = _file_for_notification(conn, notification_id)
            if file_id is not None:
                _reconcile(conn, file_id, rejection.reference_code)

    # --- E0521 WMAN exception ----------------------------------------------

    def wman_exception(
        self, header: Header, body: list[Node], filename: str
    ) -> None:
        """A wholesale market activity notification was rejected.

        Two levels of rejection, and they mean different things. A
        period-level reason with no named units rejects the whole period.
        Named units reject only those. Treating either as total would be wrong
        in one direction, so both paths are handled explicitly.

        This is the most urgent of the three: a rejected WMAN means SVAA does
        not know we were active, so the deviation is not measured at all --
        whatever the ECVN says.
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
                            f"{filename}: WMAN rejection for {unit.bmu_id} on "
                            f"{exception.settlement_date} period "
                            f"{exception.settlement_period}, which we have no record of"
                        )
                _reconcile_wman(conn, exception)
                return

            rejected = db.reject_wman(
                conn,
                settlement_date=exception.settlement_date,
                settlement_period=exception.settlement_period,
                reason=exception.reason or "rejected, no reason given",
            )
            if not rejected:
                raise HandlerError(
                    f"{filename}: WMAN rejection for {exception.settlement_date} "
                    f"period {exception.settlement_period}, which we have no record of"
                )
            _reconcile_wman(conn, exception)


def _find_notification(
    conn: Connection, rejection: ecvaa.EcvnRejection, filename: str
) -> int:
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
            f"{filename}: rejection for reference {rejection.reference_code} "
            f"effective {rejection.effective_from}, which we have no record of sending"
        )
    if len(ids) > 1:
        raise HandlerError(
            f"{filename}: rejection for reference {rejection.reference_code} matches "
            f"{len(ids)} notifications: {ids}. Reference codes must be unique per "
            f"authorisation and effective date."
        )
    return ids[0]


def _file_for_notification(conn: Connection, notification_id: int) -> int | None:
    row = conn.execute(
        "SELECT outbound_file_id FROM notification WHERE id = %s",
        (notification_id,),
    ).fetchone()
    return row[0] if row else None


def _reconcile_wman(conn: Connection, exception: ecvaa.WmanException) -> None:
    """Re-resolve the intents behind a rejected WMAN.

    A WMAN exception names a settlement date and period rather than a file, so
    the files have to be found by what they contained. There may be several:
    one BM Unit per row, and a period can carry more than one.
    """
    rows = conn.execute(
        """SELECT DISTINCT outbound_file_id FROM wman
            WHERE settlement_date = %s AND settlement_period = %s""",
        (exception.settlement_date, exception.settlement_period),
    ).fetchall()

    for (file_id,) in rows:
        _reconcile(
            conn, file_id,
            f"WMAN {exception.settlement_date} period {exception.settlement_period}",
        )


class SvaaHandlers:
    """SVAA feedback on expected volumes and delivered volumes.

    Correlation here is weaker than on the ECVAA side. P0330 acceptance
    carries only the BM Unit id, so it can only be matched to an outstanding
    submission for that unit. Where more than one is outstanding, the handler
    raises: accepting the wrong submission would leave a rejected one looking
    live.
    """

    def __init__(self, connect) -> None:
        self._connect = connect

    def sev_acceptance(self, header: Header, body: list[Node], filename: str) -> None:
        acceptances = svaa.parse_sev_acceptances(body)
        with self._connect() as conn:
            for acceptance in acceptances:
                ids = db.find_outstanding_sev(conn, acceptance.bmu_id)
                if not ids:
                    raise HandlerError(
                        f"{filename}: SEV acceptance for {acceptance.bmu_id}, which "
                        f"has no outstanding submission"
                    )
                if len(ids) > 1:
                    raise HandlerError(
                        f"{filename}: SEV acceptance for {acceptance.bmu_id} matches "
                        f"{len(ids)} outstanding submissions. P0330 carries only the "
                        f"BM Unit id, so correlation needs exactly one."
                    )
                db.accept_sev(conn, ids[0])
                _reconcile_item(conn, "sev", ids[0], acceptance.bmu_id)

    def sev_rejection(self, header: Header, body: list[Node], filename: str) -> None:
        rejections = svaa.parse_sev_rejections(body)
        with self._connect() as conn:
            for rejection in rejections:
                if rejection.bmu_id is None:
                    # Every field but the reason is optional in P0329. A
                    # rejection naming no unit cannot be applied to a row, so
                    # it is escalated rather than guessed at.
                    raise HandlerError(
                        f"{filename}: SEV rejection with no BM Unit id: "
                        f"{rejection.reason}"
                    )
                ids = db.find_outstanding_sev(conn, rejection.bmu_id)
                if not ids:
                    raise HandlerError(
                        f"{filename}: SEV rejection for {rejection.bmu_id}, which has "
                        f"no outstanding submission: {rejection.reason}"
                    )
                db.reject_sev(
                    conn, ids[0], rejection.reason, rejection.settlement_period
                )
                _reconcile_item(conn, "sev", ids[0], rejection.bmu_id)

    def sev_warning(self, header: Header, body: list[Node], filename: str) -> None:
        """A Submitted pair has an expected volume of zero for a period.

        Not a rejection: the submission stands and no state changes, so no
        intent reconciliation either. But a zero usually means a forecast
        produced nothing rather than genuinely expecting nothing, so it is
        recorded for investigation before the deviation is measured against
        it.
        """
        warnings = svaa.parse_sev_warnings(body)
        with self._connect() as conn:
            for warning in warnings:
                conn.execute(
                    """UPDATE sev SET rejection_reason = %s
                        WHERE bmu_id = %s AND state = %s
                          AND rejection_reason IS NULL""",
                    (f"warning: zero expected volume on {warning.settlement_date}",
                     warning.bmu_id, states.SUBMITTED),
                )

    def delivered_confirmation(
        self, header: Header, body: list[Node], filename: str
    ) -> None:
        confirmation = svaa.parse_delivered_confirmation(body)
        with self._connect() as conn:
            ids = db.find_outstanding_delivered(
                conn, confirmation.settlement_date, confirmation.bmu_id
            )
            if not ids:
                raise HandlerError(
                    f"{filename}: delivered volume confirmation for "
                    f"{confirmation.settlement_date}, which has no outstanding "
                    f"submission"
                )
            for delivered_id in ids:
                db.accept_delivered(conn, delivered_id)
                _reconcile_delivered(conn, delivered_id)

    def delivered_rejection(
        self, header: Header, body: list[Node], filename: str
    ) -> None:
        rejections = svaa.parse_delivered_rejections(body)
        with self._connect() as conn:
            for rejection in rejections:
                if rejection.settlement_date is None:
                    raise HandlerError(
                        f"{filename}: delivered volume rejection with no settlement "
                        f"date: {rejection.reason}"
                    )
                ids = db.find_outstanding_delivered(
                    conn, rejection.settlement_date, rejection.bmu_id
                )
                if not ids:
                    raise HandlerError(
                        f"{filename}: delivered volume rejection for "
                        f"{rejection.settlement_date}, which has no outstanding "
                        f"submission: {rejection.reason}"
                    )
                db.reject_delivered(
                    conn, ids[0], rejection.reason, rejection.settlement_period
                )
                _reconcile_delivered(conn, ids[0])


def _reconcile_item(
    conn: Connection, table: str, item_id: int, what: str
) -> None:
    """Re-resolve the intent behind an item, found via its file."""
    row = conn.execute(
        f"SELECT outbound_file_id FROM {table} WHERE id = %s", (item_id,),
    ).fetchone()
    if row and row[0]:
        _reconcile(conn, row[0], what)


def _reconcile_delivered(conn: Connection, delivered_id: int) -> None:
    """Update a delivered_intent from its delivered_volume row.

    Delivered volumes are their own intent kind rather than a flow on a
    trading intent, so they do not go through reconcile_intent. The mapping is
    direct: the item state becomes the intent state.
    """
    row = conn.execute(
        """SELECT outbound_file_id, state FROM delivered_volume WHERE id = %s""",
        (delivered_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return

    from .. import intents

    state = {
        states.ACCEPTED: intents.ACTED,
        states.REJECTED: intents.PARTIAL,
    }.get(row[1])

    if state is None:
        return

    with conn.transaction():
        conn.execute(
            """UPDATE delivered_intent
                  SET state = %s,
                      completed_at = CASE WHEN %s THEN now() ELSE completed_at END
                WHERE outbound_file_id = %s""",
            (state, state == intents.ACTED, row[0]),
        )


class EcvnaaHandler:
    """E0071: an authorisation has been processed.

    The most consequential handler in the system despite doing least. The
    ECVNAA Key arrives here and nowhere else, and without it no ECVN can be
    submitted at all. Establishing the authorisation is manual under BSCP71;
    this is the only automated point at which the key reaches us.

    The key is written to the secret store, not to the database. A credential
    in a settlement database is a credential in every backup of it.
    """

    def __init__(self, connect, store_key) -> None:
        self._connect = connect
        self._store_key = store_key

    def __call__(self, header: Header, body: list[Node], filename: str) -> None:
        confirmation = ecvaa.parse_ecvnaa_confirmation(body)

        secret_ref = None
        if confirmation.carries_key:
            secret_ref = self._store_key(
                confirmation.ecvnaa_id, confirmation.ecvnaa_key
            )

        with self._connect() as conn:
            db.confirm_ecvnaa(
                conn,
                ecvnaa_id=confirmation.ecvnaa_id,
                key_secret_ref=secret_ref,
                effective_from=confirmation.effective_from,
            )