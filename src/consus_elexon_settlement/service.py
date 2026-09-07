"""The intent service: a decision by the EMS becomes files on the wire.

Where intents.py and submissions.py meet. This is the only module that knows
both that an intent needs three flows and how to send them.

The order in `act` is fixed and each step exists for a reason:

    1. record the intent, or recognise a duplicate and stop
    2. check the deadline, before attempting anything
    3. send each outstanding flow, recording the outcome of each
    4. re-resolve the intent state from its flows

Step 1 before step 2 is deliberate. An intent that arrived too late is still
recorded -- as MISSED, with its gate closure and arrival time -- because the
question afterwards is always whether it arrived late or was delayed by us,
and a discarded message cannot answer it.

Step 2 before step 3 matters more. Submitting after Gate Closure is worse than
not submitting: central systems reject the file, the sequence number is spent
regardless, and the record suggests we tried when we had already run out of
time.

ACTED MEANS ACCEPTED. This service can only get an intent to ACTING -- it
sends, it does not receive. The move to ACTED happens when acceptance arrives
and the inbound handler calls back through `reconcile_intent`. An intent
sitting in ACTING is waiting for central systems, not stuck.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from decimal import Decimal

from psycopg import Connection

from . import db, intents
from .flows.delivered import Delivered, DeliveredPeriod, PairVolumes
from .flows.ecvn import ContractVolume, Ecvn
from .flows.sev import ExpectedPeriod, Sev, UnitVolumes
from .flows.wman import ActiveUnit, Wman
from .outbound.sender import Sent
from .outbound.submissions import Submitter

log = logging.getLogger(__name__)


class ServiceError(RuntimeError):
    """The intent could not be acted on at all.

    Distinct from a flow failing: this means there was nothing to attempt --
    no channel, an unknown authorisation. The intent is REJECTED rather than
    PARTIAL, because a retry would fail the same way.
    """


@dataclass(frozen=True)
class Channels:
    """The three channels an intent needs.

    Passed in rather than looked up per call: a channel lookup is a database
    round trip, and the set does not change between intents. Also makes the
    two-identity split visible at the call site -- ECVNs go out as the ECVN
    Agent, everything else as the VTP (ADR-0004).
    """

    vtp_to_ecvaa: db.Channel      # WMAN
    agent_to_ecvaa: db.Channel    # ECVN
    vtp_to_svaa: db.Channel       # SEV, Delivered


@dataclass(frozen=True)
class Outcome:
    """What happened to one intent."""

    intent_id: int
    state: str
    flows: dict[str, str]
    duplicate: bool = False

    @property
    def acted(self) -> bool:
        return self.state == intents.ACTED

    @property
    def needs_attention(self) -> bool:
        """Whether a human should look at this.

        MISSED and REJECTED are terminal failures. PARTIAL is retryable but
        the retry is automatic, so it only needs attention if it persists --
        which the sweep judges against Gate Closure rather than here.
        """
        return self.state in (intents.MISSED, intents.REJECTED)


class IntentService:
    """Acts on intents from the EMS.

    Holds a connection factory rather than a connection: this runs from a
    subscriber that may be long-lived, and a connection held open across hours
    is a connection that will be dead at Gate Closure.
    """

    def __init__(self, connect, submitter: Submitter, channels: Channels) -> None:
        self._connect = connect
        self._submitter = submitter
        self._channels = channels

    # --- trading intents ----------------------------------------------------

    def act(
        self, intent: intents.Intent, now: dt.datetime | None = None
    ) -> Outcome:
        """Record an intent and send what it requires.

        Idempotent by the intent's natural key. A second delivery of the same
        message finds the existing row and returns what happened the first
        time without sending anything -- Pub/Sub delivers at least once, and
        the EMS can also send the same decision twice by mistake.
        """
        now = now or dt.datetime.now(dt.timezone.utc)

        with self._connect() as conn:
            existing = _find(conn, intent.key)
            if existing is not None:
                intent_id, state = existing
                flows = _flow_states(conn, intent_id)
                log.info(
                    "intent %s already recorded in %s, sending nothing",
                    intent.key, state,
                )
                return Outcome(intent_id, state, flows, duplicate=True)

            intent_id = _record(conn, intent)

        # The deadline check happens after recording, so a late intent leaves
        # evidence of having arrived late rather than vanishing.
        if intent.arrived_too_late(now):
            with self._connect() as conn:
                _set_state(
                    conn, intent_id, intents.MISSED,
                    detail=(
                        f"arrived at {now:%Y-%m-%d %H:%M:%SZ}, after gate "
                        f"closure at {intent.gate_closure:%Y-%m-%d %H:%M:%SZ}"
                    ),
                )
            log.error(
                "intent %s arrived after gate closure; nothing submitted",
                intent.key,
            )
            return Outcome(intent_id, intents.MISSED, {})

        return self._send_flows(intent_id, intent)

    def retry(self, intent_id: int, now: dt.datetime | None = None) -> Outcome:
        """Attempt the flows that have not succeeded.

        Only PENDING and FAILED are retried. Resending an ACCEPTED flow would
        spend a sequence number on a duplicate; resending a REJECTED one would
        be refused again for the same reason as the first time.
        """
        now = now or dt.datetime.now(dt.timezone.utc)

        with self._connect() as conn:
            row = conn.execute(
                """SELECT settlement_date, settlement_period, bmu_id, revision,
                          expected_mwh, contracted_mwh, ecvnaa_id,
                          ecvn_ecvnaa_id, state
                     FROM intent WHERE id = %s""",
                (intent_id,),
            ).fetchone()

        if row is None:
            raise ServiceError(f"no intent {intent_id}")

        state = row[8]
        if intents.is_terminal(state):
            raise ServiceError(
                f"intent {intent_id} is {state} and cannot be retried"
            )

        intent = intents.Intent(
            settlement_date=row[0], settlement_period=row[1], bmu_id=row[2],
            revision=row[3], expected_mwh=row[4], contracted_mwh=row[5],
            ecvnaa_id=row[6], ecvn_ecvnaa_id=row[7],
        )

        if intent.arrived_too_late(now):
            with self._connect() as conn:
                _set_state(
                    conn, intent_id, intents.MISSED,
                    detail="gate closure passed before the retry",
                )
            return Outcome(intent_id, intents.MISSED, _flow_states_now(self, intent_id))

        return self._send_flows(intent_id, intent)

    def _send_flows(self, intent_id: int, intent: intents.Intent) -> Outcome:
        """Send whatever is outstanding, then re-resolve.

        Each flow is attempted independently. One failing does not stop the
        others: a WMAN that fails should not prevent the ECVN, because a
        partial submission is more recoverable than none and the retry knows
        which is which.

        A flow that already has a file is RESENT, not rebuilt. Its file
        reached the archive and only transport failed, so the bytes exist and
        the sequence number is already spent on them. Rebuilding would
        allocate a second number, leave a permanent gap at the first, and --
        because the ECVN reference code is deterministic -- collide with the
        notification row the first attempt already wrote.

        That collision is what made this visible rather than silent. Without
        it every transport failure would have been unrecoverable: the retry
        failing forever on a unique constraint, the intent stuck at PARTIAL,
        and the position unhedged. It is the same discipline as ADR-0002, one
        layer up.
        """
        with self._connect() as conn:
            _ensure_flows(conn, intent_id, intents.REQUIRED_FLOWS)
            outstanding = intents.retryable_flows(_flow_states(conn, intent_id))

        builders = {
            "wman": self._send_wman,
            "ecvn": self._send_ecvn,
            "sev": self._send_sev,
        }

        for flow in outstanding:
            existing = self._file_for_flow(intent_id, flow)

            try:
                if existing is not None:
                    # Built already; only the wire failed.
                    sent = self._submitter.resend(existing)
                else:
                    sent = builders[flow](intent)
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                # A build failure, a missing channel, an unanticipated
                # transport error. Recorded against the flow rather than
                # raised, so the other flows still get their turn.
                log.error(
                    "intent %s: %s failed to %s: %s",
                    intent.key, flow,
                    "resend" if existing is not None else "build",
                    exc,
                )
                with self._connect() as conn:
                    _record_flow(conn, intent_id, flow, intents.FAILED,
                                 detail=str(exc)[:500])
                continue

            state = intents.SENT if sent.delivered else intents.FAILED
            with self._connect() as conn:
                _record_flow(
                    conn, intent_id, flow, state,
                    detail=sent.error, outbound_file_id=sent.file_id,
                )
            if not sent.delivered:
                log.warning("intent %s: %s not delivered: %s",
                            intent.key, flow, sent.error)

        return self._resolve(intent_id, intent)

    def _file_for_flow(self, intent_id: int, flow: str) -> int | None:
        """The file this flow already built, if any.

        Its presence is what distinguishes 'transport failed' from 'never got
        that far'. The first is resent; the second is built.
        """
        with self._connect() as conn:
            row = conn.execute(
                """SELECT outbound_file_id FROM intent_flow
                    WHERE intent_id = %s AND flow = %s""",
                (intent_id, flow),
            ).fetchone()
        return row[0] if row and row[0] else None

    def _send_wman(self, intent: intents.Intent) -> Sent:
        return self._submitter.wman(
            self._channels.vtp_to_ecvaa,
            Wman(
                settlement_date=intent.settlement_date,
                settlement_period=intent.settlement_period,
                units=(ActiveUnit(intent.bmu_id),),
            ),
        )

    def _send_ecvn(self, intent: intents.Intent) -> Sent:
        # The reference code must be unique per authorisation and effective
        # date -- migration 0003 enforces it -- because E0091 rejection
        # carries no filename and this is the only way back to the row.
        # Revision is included so a revised position gets its own code.
        reference = (
            f"{intent.settlement_date:%y%m%d}"
            f"{intent.settlement_period:02d}{intent.revision:02d}"
        )
        return self._submitter.ecvn(
            self._channels.agent_to_ecvaa,
            Ecvn(
                ecvnaa_id=intent.ecvnaa_id,
                ecvn_ecvnaa_id=intent.ecvn_ecvnaa_id,
                reference_code=reference,
                effective_from=intent.settlement_date,
                effective_to=intent.settlement_date,
                volumes=(ContractVolume(
                    intent.settlement_period, intent.contracted_mwh,
                ),),
            ),
            ecvnaa_key=self._ecvnaa_key(intent.ecvnaa_id),
        )

    def _send_sev(self, intent: intents.Intent) -> Sent:
        # Per-period rather than Default: effective_to set to the same day.
        # The Default is registered separately and stands as the fallback.
        return self._submitter.sev(
            self._channels.vtp_to_svaa,
            Sev(
                effective_from=intent.settlement_date,
                effective_to=intent.settlement_date,
                units=(UnitVolumes(intent.bmu_id, (
                    ExpectedPeriod(intent.settlement_period, intent.expected_mwh),
                )),),
            ),
        )

    def _ecvnaa_key(self, ecvnaa_id: str) -> str:
        """The ECVNAA Key for an authorisation.

        Read from the secret store at send time and never held on the intent,
        so an intent can be stored, logged and replayed without the credential
        travelling with it.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT key_secret_ref FROM ecvnaa WHERE ecvnaa_id = %s",
                (ecvnaa_id,),
            ).fetchone()

        if row is None:
            raise ServiceError(
                f"authorisation {ecvnaa_id} is not registered. It is "
                f"established manually under BSCP71 and confirmed by E0071."
            )
        if row[0] is None:
            raise ServiceError(
                f"authorisation {ecvnaa_id} has no key. The key arrives in "
                f"E0071 and without it no ECVN can be submitted."
            )
        return _read_secret(row[0])

    # --- delivered volumes --------------------------------------------------

    def deliver(self, delivered: intents.DeliveredIntent) -> Outcome:
        """Submit a delivered volume at D+1.

        Its own path rather than a fourth flow: the MSID Pair is not known at
        trading time, the deadline is a day later, and one delivered volume
        may cover a period traded across several intents.
        """
        with self._connect() as conn:
            existing = _find_delivered(conn, delivered.key)
            if existing is not None:
                intent_id, state = existing
                return Outcome(intent_id, state, {}, duplicate=True)

            intent_id = _record_delivered(conn, delivered)

        try:
            sent = self._submitter.delivered(
                self._channels.vtp_to_svaa,
                Delivered(
                    settlement_date=delivered.settlement_date,
                    pairs=(PairVolumes(
                        import_msid=delivered.import_msid,
                        export_msid=delivered.export_msid,
                        gsp_group_id=delivered.gsp_group_id,
                        bmu_id=delivered.bmu_id,
                        periods=(DeliveredPeriod(
                            delivered.settlement_period, delivered.delivered_mwh,
                        ),),
                    ),),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            with self._connect() as conn:
                _set_delivered_state(
                    conn, intent_id, intents.PARTIAL, detail=str(exc)[:500]
                )
            return Outcome(intent_id, intents.PARTIAL, {})

        state = intents.ACTING if sent.delivered else intents.PARTIAL
        with self._connect() as conn:
            _set_delivered_state(
                conn, intent_id, state,
                detail=sent.error, outbound_file_id=sent.file_id,
            )
        return Outcome(intent_id, state, {DELIVERED: state})



    def register_default_sev(
        self, default: intents.DefaultSevIntent
    ) -> Outcome:
        """Register a standing expected volume profile.

        Not routed through the flow machinery: a Default is one file, not
        three, and has no Gate Closure to miss -- its deadline is 23:59 the
        day before, which is the EMS's to meet rather than ours to enforce.

        Idempotent on effective date, BM Unit and revision. Republishing the
        same profile sends nothing; a corrected profile is a new revision.
        """
        with self._connect() as conn:
            existing = _find_default_sev(conn, default.key)
            if existing is not None:
                intent_id, state = existing
                return Outcome(intent_id, state, {}, duplicate=True)

            intent_id = _record_default_sev(conn, default)

        try:
            sent = self._submitter.sev(
                self._channels.vtp_to_svaa,
                Sev(
                    effective_from=default.effective_from,
                    # No effective_to. That is what makes it a Default: it
                    # stands until replaced rather than covering one day.
                    units=(UnitVolumes(default.bmu_id, tuple(
                        ExpectedPeriod(period, volume)
                        for period, volume in default.periods
                    )),),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            with self._connect() as conn:
                _set_default_sev_state(
                    conn, intent_id, intents.PARTIAL, detail=str(exc)[:500]
                )
            return Outcome(intent_id, intents.PARTIAL, {})

        state = intents.ACTING if sent.delivered else intents.PARTIAL
        with self._connect() as conn:
            _set_default_sev_state(
                conn, intent_id, state,
                detail=sent.error, outbound_file_id=sent.file_id,
            )

        if not default.covers_full_day:
            log.warning(
                "default SEV for %s on %s covers %d periods, not the full day. "
                "Periods without a value fall to NULL if no per-period SEV is "
                "registered before Gate Closure (BSCP602 2.13.7).",
                default.bmu_id, default.effective_from, len(default.periods),
            )

        return Outcome(intent_id, state, {"sev": state})

    # --- resolution ---------------------------------------------------------

    def _resolve(
        self, intent_id: int, intent: intents.Intent,
        now: dt.datetime | None = None,
    ) -> Outcome:
        """Recompute the intent state from its flows.

        Derived rather than tracked, so the intent cannot disagree with its
        own flows. Called after every change to either.
        """
        with self._connect() as conn:
            flows = _flow_states(conn, intent_id)
            state = intents.resolve(flows, intent.arrived_too_late(now))
            _set_state(conn, intent_id, state)

        if state == intents.MISSED:
            # The [MISSED] suffix is matched by the gate-closure log metric in
            # infra/alerts.tf. Changing this format silently disables the
            # alert that tells a human to perform the manual fallback.
            log.error("intent %s: gate closure passed [MISSED]", intent.key)
        elif state == intents.PARTIAL:
            log.warning("intent %s: partial, outstanding %s",
                        intent.key, intents.retryable_flows(flows))

        return Outcome(intent_id, state, flows)


DELIVERED = intents.DELIVERED_FLOW


def reconcile_intent(conn: Connection, outbound_file_id: int) -> str | None:
    """Re-resolve the intent that owns a file, after feedback.

    Called from the inbound handlers when an acceptance or rejection arrives.
    Without this an intent never leaves ACTING, which would defeat the whole
    point of ACTED meaning accepted.

    Returns the new intent state, or None if the file belongs to no intent --
    which is normal for registration files and manual submissions.
    """
    row = conn.execute(
        """SELECT f.intent_id, f.flow, i.settlement_date, i.settlement_period,
                  i.state
             FROM intent_flow f
             JOIN intent i ON i.id = f.intent_id
            WHERE f.outbound_file_id = %s""",
        (outbound_file_id,),
    ).fetchone()

    if row is None:
        return None

    intent_id, flow, settlement_date, settlement_period, current = row
    if intents.is_terminal(current):
        # An acceptance arriving after the deadline does not un-miss an
        # intent, and a second acceptance does not reopen a closed one.
        return current

    # The flow's own state comes from the item tables, which the inbound
    # handlers have already updated. Read it back rather than passing it in,
    # so this function cannot disagree with what was recorded.
    flow_state = _flow_state_from_items(conn, outbound_file_id, flow)
    conn.execute(
        "UPDATE intent_flow SET state = %s WHERE outbound_file_id = %s",
        (flow_state, outbound_file_id),
    )

    from . import deadlines

    flows = _flow_states(conn, intent_id)
    state = intents.resolve(
        flows, deadlines.is_closed(settlement_date, settlement_period)
    )
    _set_state(conn, intent_id, state)
    return state


# --- persistence -------------------------------------------------------------


def _find(conn: Connection, key: tuple) -> tuple[int, str] | None:
    row = conn.execute(
        """SELECT id, state FROM intent
            WHERE settlement_date = %s AND settlement_period = %s
              AND bmu_id = %s AND revision = %s""",
        key,
    ).fetchone()
    return (row[0], row[1]) if row else None


def _record(conn: Connection, intent: intents.Intent) -> int:
    with conn.transaction():
        row = conn.execute(
            """INSERT INTO intent (settlement_date, settlement_period, bmu_id,
                                   revision, expected_mwh, contracted_mwh,
                                   ecvnaa_id, ecvn_ecvnaa_id, state,
                                   gate_closure)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                 RETURNING id""",
            (intent.settlement_date, intent.settlement_period, intent.bmu_id,
             intent.revision, intent.expected_mwh, intent.contracted_mwh,
             intent.ecvnaa_id, intent.ecvn_ecvnaa_id, intents.RECEIVED,
             intent.gate_closure),
        ).fetchone()
    return row[0]


def _ensure_flows(conn: Connection, intent_id: int, flows: tuple[str, ...]) -> None:
    with conn.transaction():
        for flow in flows:
            conn.execute(
                """INSERT INTO intent_flow (intent_id, flow, state)
                        VALUES (%s, %s, %s)
                   ON CONFLICT (intent_id, flow) DO NOTHING""",
                (intent_id, flow, intents.PENDING),
            )


def _flow_states(conn: Connection, intent_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT flow, state FROM intent_flow WHERE intent_id = %s",
        (intent_id,),
    ).fetchall()
    return {flow: state for flow, state in rows}


def _flow_states_now(service: IntentService, intent_id: int) -> dict[str, str]:
    with service._connect() as conn:  # noqa: SLF001 - same module
        return _flow_states(conn, intent_id)


def _record_flow(
    conn: Connection, intent_id: int, flow: str, state: str,
    detail: str | None = None, outbound_file_id: int | None = None,
) -> None:
    with conn.transaction():
        conn.execute(
            """UPDATE intent_flow
                  SET state = %s,
                      detail = %s,
                      outbound_file_id = COALESCE(%s, outbound_file_id),
                      attempts = attempts + 1,
                      last_attempt = now()
                WHERE intent_id = %s AND flow = %s""",
            (state, detail, outbound_file_id, intent_id, flow),
        )


def _set_state(
    conn: Connection, intent_id: int, state: str, detail: str | None = None
) -> None:
    with conn.transaction():
        conn.execute(
            """UPDATE intent
                  SET state = %s,
                      detail = COALESCE(%s, detail),
                      completed_at = CASE WHEN %s THEN now() ELSE completed_at END
                WHERE id = %s""",
            (state, detail, intents.is_terminal(state), intent_id),
        )


def _flow_state_from_items(
    conn: Connection, outbound_file_id: int, flow: str
) -> str:
    """The flow's state, read from whichever item table owns it.

    Read back rather than passed in, so this cannot disagree with what the
    inbound handler recorded. The item vocabulary maps onto the flow one:
    ACCEPTED and REJECTED are the same word in both, SUBMITTED means we are
    still waiting.
    """
    table = {
        "wman": "wman",
        "ecvn": "notification",
        "sev": "sev",
        DELIVERED: "delivered_volume",
    }[flow]

    row = conn.execute(
        f"SELECT state FROM {table} WHERE outbound_file_id = %s LIMIT 1",
        (outbound_file_id,),
    ).fetchone()

    if row is None:
        return intents.SENT

    from . import states

    return {
        states.ACCEPTED: intents.ACCEPTED,
        states.REJECTED: intents.FLOW_REJECTED,
        states.SUBMITTED: intents.SENT,
        states.PENDING: intents.PENDING,
    }.get(row[0], intents.SENT)


def _find_delivered(conn: Connection, key: tuple) -> tuple[int, str] | None:
    row = conn.execute(
        """SELECT id, state FROM delivered_intent
            WHERE settlement_date = %s AND settlement_period = %s
              AND bmu_id = %s AND import_msid = %s AND revision = %s""",
        key,
    ).fetchone()
    return (row[0], row[1]) if row else None


def _record_delivered(conn: Connection, d: intents.DeliveredIntent) -> int:
    # D+1 in working days needs a bank holiday calendar, which deadlines.py
    # does not have. Recorded as the next calendar day for now, which is
    # correct four days in five and wrong in a direction that makes us early
    # rather than late.
    due = dt.datetime.combine(
        d.settlement_date + dt.timedelta(days=1),
        dt.time(23, 59), tzinfo=dt.timezone.utc,
    )
    with conn.transaction():
        row = conn.execute(
            """INSERT INTO delivered_intent
                    (settlement_date, settlement_period, bmu_id, revision,
                     gsp_group_id, import_msid, export_msid, delivered_mwh,
                     state, due_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                 RETURNING id""",
            (d.settlement_date, d.settlement_period, d.bmu_id, d.revision,
             d.gsp_group_id, d.import_msid, d.export_msid, d.delivered_mwh,
             intents.RECEIVED, due),
        ).fetchone()
    return row[0]


def _set_delivered_state(
    conn: Connection, intent_id: int, state: str,
    detail: str | None = None, outbound_file_id: int | None = None,
) -> None:
    with conn.transaction():
        conn.execute(
            """UPDATE delivered_intent
                  SET state = %s,
                      detail = COALESCE(%s, detail),
                      outbound_file_id = COALESCE(%s, outbound_file_id),
                      completed_at = CASE WHEN %s THEN now() ELSE completed_at END
                WHERE id = %s""",
            (state, detail, outbound_file_id,
             intents.is_terminal(state), intent_id),
        )


def _read_secret(reference: str) -> str:
    """Fetch a secret by reference.

    Not implemented: the ECVNAA key arrives in E0071 and the secret store is
    not yet wired, which is the same gap app._no_key_store names. Failing here
    is correct -- an ECVN signed with a guessed key would be rejected, and a
    silent default would be worse.
    """
    raise NotImplementedError(
        f"secret store not configured; cannot read {reference}. The ECVNAA "
        f"key arrives in E0071 and must be written to Secret Manager."
    )