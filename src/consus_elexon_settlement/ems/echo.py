"""A receive-only endpoint, for proving the EMS bridge before settlement exists.

TEMPORARY. Delete this module and `infra/ems_echo.tf` together once the real
endpoint in `ems.tf.hold` is deployed.

WHAT IT IS FOR. `ems/endpoint.py` cannot start yet: `create_app` builds the
transport at import, and `_transport` requires CONSUS_FTP_HOST and four more
variables that Elexon have not supplied. So the push path -- the one that
actually matters -- is untestable, and the alternative is draining a pull
subscription by hand and eyeballing base64.

This closes that without loosening anything. It answers one question: does a
message the EMS publishes arrive here and parse? Nothing else.

WHAT IT DELIBERATELY CANNOT DO. It imports `ems.messages` and nothing else
from this package. That module is pure -- no HTTP, no database, no GCS -- so
this service has no submitter, no channels, no archive and no keyring, and
therefore CANNOT record an intent, build a file or send one. That is not
restraint, it is the import graph: the capability is absent rather than
declined.

The alternative was letting `_transport` fall back to local directories so the
real endpoint could start. `endpoint.py` already rejects that, correctly: "an
endpoint that accepts an intent and writes it to a directory nobody reads
would report success and submit nothing." A fallback added to make a test
possible is a fallback that lives in the production path forever.

EVERYTHING IS ACKNOWLEDGED, INCLUDING FAILURES. The real endpoint nacks in one
case -- nothing was recorded and redelivery might help. Here nothing is ever
recorded, so there is no failure redelivery could fix, and a nack would only
build a backlog that blocks whatever is behind it. A parse failure is the
finding; it is returned in the body and logged, not retried.
"""

from __future__ import annotations

import logging

from flask import Flask, jsonify, request

from . import messages

log = logging.getLogger("consus.settlement.ems.echo")


def _summarise(kind: str, payload: dict) -> dict:
    """Parse with the same functions the real endpoint uses, and report.

    THE POINT IS THE SHARED CODE PATH. A summariser that read the dict
    directly would prove the network and nothing else -- it would accept a
    message the real endpoint rejects, and the drift between two repositories
    is exactly what this is meant to catch. So every kind goes through the
    same `to_*` the real `/intent` calls, and what comes back is a report of
    the PARSED object, never the payload.
    """
    if kind == messages.TRADING:
        intent = messages.to_intent(payload)
        return {
            "bmu_id": intent.bmu_id,
            "settlement_date": intent.settlement_date.isoformat(),
            "settlement_period": intent.settlement_period,
            "expected_mwh": str(intent.expected_mwh),
            "contracted_mwh": str(intent.contracted_mwh),
        }

    if kind == messages.DEFAULT_SEV:
        intent = messages.to_default_sev(payload)
        return {
            "bmu_id": intent.bmu_id,
            "effective_from": intent.effective_from.isoformat(),
            "periods": len(intent.periods),
        }

    intent = messages.to_delivered(payload)
    return {
        "bmu_id": intent.bmu_id,
        "settlement_date": intent.settlement_date.isoformat(),
        "settlement_period": intent.settlement_period,
        "delivered_mwh": str(intent.delivered_mwh),
    }


def create_app() -> Flask:
    """Build the app. Takes no configuration, deliberately.

    `endpoint.create_app` reads a DSN, a bucket, five FTP variables and three
    secret mounts, and any one of them missing stops it starting. This reads
    nothing, so there is no deployment in which it starts degraded and no
    variable whose absence changes what it does.
    """
    app = Flask(__name__)

    @app.post("/intent")
    def receive():  # noqa: ANN202 - Flask view
        envelope = request.get_json(silent=True)
        if not isinstance(envelope, dict):
            log.error("echo: request body is not a JSON object")
            return jsonify(ok=False, error="expected a Pub/Sub push envelope"), 200

        pubsub_id = messages.message_id(envelope)

        try:
            payload = messages.unwrap(envelope)
            kind = messages.kind(payload)
            parsed = _summarise(kind, payload)
        except messages.MessageError as exc:
            # THE FINDING, not an error to retry. The EMS published something
            # this gateway cannot read, and the same bytes will fail the same
            # way forever.
            log.error("echo: message %s did not parse: %s", pubsub_id, exc)
            return jsonify(ok=False, message_id=pubsub_id, error=str(exc)), 200

        log.info("echo: message %s parsed as %s: %s", pubsub_id, kind, parsed)
        return jsonify(ok=True, message_id=pubsub_id, kind=kind, parsed=parsed), 200

    @app.get("/health")
    def health():  # noqa: ANN202
        return jsonify(status="ok"), 200

    return app


# gunicorn imports this. Unconditional, unlike endpoint.py's DSN guard, because
# there is no configuration whose absence should stop it.
app = create_app()
