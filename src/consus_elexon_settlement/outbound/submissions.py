"""Submitting: a decision becomes a file on the wire.

The layer between whatever decides to trade and the gateway that sends. Each
method takes a domain object, sends it, and records what was sent.

Ordering is fixed and not obvious. Sender.send reserves the file row, builds
the bytes, archives them, then delivers -- so the file id exists whether or
not delivery succeeded. The domain rows are therefore inserted AFTER the send
returns, against that file id, and moved to SUBMITTED only if it was
delivered. A transport failure leaves them PENDING against an archived file,
which is exactly what a retry needs to find.

Inserting before the send is not possible: there is no file id to reference
until reserve has run.

The four flows and where their content comes from:

    WMAN (E0511)       which BM Units are wholesale-active. From the traded
                       position. Before Gate Closure.

    ECVN (E0041)       the contracted volume. From the exchange fill. Before
                       Gate Closure.

    SEV (P0328)        what the BM Unit would have done absent our action.
                       From the forecast. Default by 23:59 the day before,
                       per-period before Gate Closure.

    Delivered (P0282)  what we actually delivered -- our deviation, not the
                       metered volume. SVAA already holds the metered data
                       from the HHDAs (BSCP602 Appendix 3.6) and uses this to
                       work out how much of it was ours. Due at D+1.

That last point is easy to get backwards. The Delivered Volume is capped by
the metered volume, not equal to it: claiming more than the meter shows
produces a P0285 exception.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from .. import db
from ..ems import flow_events
from ..flows import delivered as delivered_flow
from ..flows import ecvn as ecvn_flow
from ..flows import sev as sev_flow
from ..flows import wman as wman_flow
from ..idd import spec, spec_svaa
from ..idd.model import Flow
from .sender import Sender, Sent

log = logging.getLogger("consus.settlement.submissions")


class Submitter:
    """Builds, sends and records outbound submissions.

    Holds a connection factory rather than a connection, for the same reason
    everything else does: this runs from a scheduler, and a connection held
    open across hours is a connection that will be dead at Gate Closure.
    """

    def __init__(self, connect, sender: Sender, flow_publisher=None) -> None:
        self._connect = connect
        self._sender = sender
        #: Optional. None is the normal state until the EMS return topic is
        #: stood up, and a service that refused to start without it would be
        #: unstartable today. See `ems.flow_publisher.FlowPublisher.from_env`.
        self._flow_publisher = flow_publisher

    # --- ECVAA -------------------------------------------------------------

    def wman(self, channel: db.Channel, notification: wman_flow.Wman) -> Sent:
        """Tell ECVAA which BM Units are wholesale-active for a period.

        Must arrive before Gate Closure. A rejected or missing WMAN means SVAA
        never learns we were active, so no deviation is measured -- whatever
        the ECVN says.
        """
        sent = self._sender.send(
            channel=channel,
            flow=_flow(spec.SPEC, wman_flow.FILE_TYPE),
            body=wman_flow.to_nodes(notification),
        )

        with self._connect() as conn, conn.transaction():
            for unit in notification.units:
                conn.execute(
                    """INSERT INTO wman (outbound_file_id, settlement_date,
                                         settlement_period, bmu_id, active, state)
                            VALUES (%s, %s, %s, %s, %s, 'PENDING')""",
                    (sent.file_id, notification.settlement_date,
                     notification.settlement_period, unit.bmu_id, unit.active),
                )

        marked = self._mark(sent, "wman")
        self._tell_ems(marked, notification)
        return marked

    def ecvn(
        self, channel: db.Channel, notification: ecvn_flow.Ecvn, ecvnaa_key: str
    ) -> Sent:
        """Tell ECVAA the contracted volume.

        The key is read from the secret store by the caller and passed here
        rather than held on the Ecvn, so a notification can be stored and
        replayed without the credential travelling with it.
        """
        sent = self._sender.send(
            channel=channel,
            flow=_flow(spec.SPEC, ecvn_flow.FILE_TYPE),
            body=ecvn_flow.to_nodes(notification, ecvnaa_key),
        )

        with self._connect() as conn, conn.transaction():
            row = conn.execute(
                """INSERT INTO notification (outbound_file_id, ecvnaa_id,
                                             ecvn_ecvnaa_id, reference_code,
                                             effective_from, effective_to, state)
                        VALUES (%s, %s, %s, %s, %s, %s, 'PENDING')
                     RETURNING id""",
                (sent.file_id, notification.ecvnaa_id, notification.ecvn_ecvnaa_id,
                 notification.reference_code, notification.effective_from,
                 notification.effective_to),
            ).fetchone()

            for volume in notification.volumes:
                conn.execute(
                    """INSERT INTO notification_period
                            (notification_id, settlement_period, volume_mwh, state)
                            VALUES (%s, %s, %s, 'PENDING')""",
                    (row[0], volume.settlement_period, volume.volume_mwh),
                )

        return self._mark(sent, "notification")

    # --- SVAA --------------------------------------------------------------

    def sev(self, channel: db.Channel, expected: sev_flow.Sev) -> Sent:
        """Tell SVAA what the BM Unit would have done absent our action.

        A Default SEV -- no effective_to -- stands until replaced and is the
        safety net: if neither a Default nor a per-period value is registered
        before Gate Closure, SVAA sets Settlement Expected Volume to NULL and
        the deviation is lost entirely (BSCP602 2.13.7).
        """
        sent = self._sender.send(
            channel=channel,
            flow=_flow(spec_svaa.SPEC, sev_flow.FILE_TYPE),
            body=sev_flow.to_nodes(expected),
        )

        with self._connect() as conn, conn.transaction():
            for unit in expected.units:
                row = conn.execute(
                    """INSERT INTO sev (outbound_file_id, effective_from,
                                        effective_to, bmu_id, state)
                            VALUES (%s, %s, %s, %s, 'PENDING')
                         RETURNING id""",
                    (sent.file_id, expected.effective_from,
                     expected.effective_to, unit.bmu_id),
                ).fetchone()

                for period in unit.periods:
                    conn.execute(
                        """INSERT INTO sev_period
                                (sev_id, settlement_period, volume_mwh, state)
                                VALUES (%s, %s, %s, 'PENDING')""",
                        (row[0], period.settlement_period, period.volume_mwh),
                    )

        return self._mark(sent, "sev")

    def delivered(
        self, channel: db.Channel, volumes: delivered_flow.Delivered
    ) -> Sent:
        """Tell SVAA what we actually delivered, at D+1.

        This is our deviation, not the metered volume. SVAA already holds the
        metered data from the HHDAs and uses this to allocate how much of it
        was ours (BSCP602 Appendix 3.6). The value is capped by the metered
        volume, so over-claiming produces a P0285 exception rather than
        settling.
        """
        sent = self._sender.send(
            channel=channel,
            flow=_flow(spec_svaa.SPEC, delivered_flow.FILE_TYPE),
            body=delivered_flow.to_nodes(volumes),
        )

        with self._connect() as conn, conn.transaction():
            for pair in volumes.pairs:
                row = conn.execute(
                    """INSERT INTO delivered_volume
                            (outbound_file_id, settlement_date, gsp_group_id,
                             bmu_id, import_msid, export_msid, state)
                            VALUES (%s, %s, %s, %s, %s, %s, 'PENDING')
                         RETURNING id""",
                    (sent.file_id, volumes.settlement_date, pair.gsp_group_id,
                     pair.bmu_id, pair.import_msid, pair.export_msid),
                ).fetchone()

                for period in pair.periods:
                    conn.execute(
                        """INSERT INTO delivered_volume_period
                                (delivered_volume_id, settlement_period,
                                 volume_mwh, state)
                                VALUES (%s, %s, %s, 'PENDING')""",
                        (row[0], period.settlement_period, period.volume_mwh),
                    )

        return self._mark(sent, "delivered_volume")

        # --- retry --------------------------------------------------------------

    def resend(self, file_id: int) -> Sent:
        """Re-send a file that was already built and archived.

        For a flow whose first attempt reached the archive but failed at
        transport. Delegates to the sender, which reads the bytes back rather
        than rebuilding.

        No domain rows are touched. They exist from the first attempt and are
        still PENDING, which is exactly what makes them findable -- and what a
        rebuild would collide with, since the reference code is deterministic
        and the business key is unique.
        """
        return self._sender.retry(file_id)

    # --- internals ---------------------------------------------------------

    def _mark(self, sent: Sent, table: str) -> Sent:
        """Move the items to SUBMITTED, but only if the file was delivered.

        A transport failure leaves them PENDING against an archived file,
        which is what Sender.retry needs to find. Marking them submitted
        regardless would claim we had told Elexon something we had not.
        """
        if sent.delivered:
            with self._connect() as conn:
                db.submit_items(conn, table, sent.file_id)
        return sent

    def _tell_ems(self, sent: Sent, notification: wman_flow.Wman) -> None:
        """Publish one `submitted` flow event per BM Unit in a sent WMAN.

        WHY THE EMS NEEDS THIS AT ALL. Its dispatch gate refuses to move a
        battery on a period it cannot show was notified -- and without a
        return channel it could only read its OWN published intent, which
        records what it asked for rather than what we did. Deviation Volume
        is calculated only for a period with a BOA or a WMAN (BSC Section T
        4.3.AA.1), so a battery moved on an unnotified period deviates against
        nothing and whatever trade it was delivering settles unhedged.

        ONE FILE IS MANY EVENTS. The gate is per BM Unit per period, so a
        notification naming four units produces four messages.

        NOTHING IS PUBLISHED FOR A FILE THAT DID NOT LAND. `sent.delivered`
        false means the bytes are archived and transport failed; the items are
        still PENDING and `Sender.retry` will resend them. Telling the EMS we
        notified ECVAA at that point would open its gate on a notification
        nobody has received.

        THE TIMESTAMP IS OUR HANDOVER, AND THE MESSAGE SAYS SO. We do not know
        when ECVAA received the file -- that arrives later in an ADT, on an
        inbound path not yet wired to the router -- so the basis is `sent`,
        which the EMS treats as a LOWER BOUND: enough to prove lateness, not
        enough to prove punctuality. See `ems.flow_events`.

        BEST EFFORT, NEVER RAISING. The file is sent and the rows are
        SUBMITTED; that record matters more than this message. A missing flow
        event fails SAFE at the far end, because no evidence means no dispatch.
        """
        if self._flow_publisher is None or not sent.delivered:
            return
        with self._connect() as conn:
            handed_over = db.sent_at(conn, sent.file_id)
        if handed_over is None:
            # DELIVERED WITH NO SEND TIME IS A CONTRADICTION, and substituting
            # `now()` would put a timestamp minutes late into a Gate Closure
            # comparison. Report it and send nothing.
            log.error(
                "flow_event.no_send_time",
                extra={"file_id": sent.file_id, "filename": sent.filename},
            )
            return
        messages = [
            flow_events.submitted_event(
                flow=flow_events.WMAN,
                bmu_id=unit.bmu_id,
                settlement_date=notification.settlement_date,
                settlement_period=notification.settlement_period,
                occurred_at=handed_over,
                basis=flow_events.BASIS_SENT,
            )
            for unit in notification.units
        ]
        published = self._flow_publisher.publish_all(messages)
        if published != len(messages):
            log.warning(
                "flow_event.partial_publish",
                extra={
                    "file_id": sent.file_id,
                    "published": published,
                    "expected": len(messages),
                },
            )


def _flow(spec_module, file_type: str) -> Flow:
    flow = spec_module.flows.get(file_type)
    if flow is None:
        raise KeyError(
            f"{file_type} is not in the loaded spec. The spec is generated from "
            f"the IDD spreadsheet; a missing flow means the generator did not "
            f"emit it, not that the flow does not exist."
        )
    return flow


def deviation(expected_mwh: Decimal, actual_mwh: Decimal) -> Decimal:
    """The delivered volume for one settlement period.

    Both arguments are in CVA convention: positive is Export, negative is
    Import. The delivered volume is what changed because of us -- actual minus
    expected -- and is what P0282 reports.

    A single function so the sign convention is settled in one place. Getting
    it backwards produces a file that passes validation and settles the wrong
    way round, which is the worst kind of wrong.
    """
    return actual_mwh - expected_mwh