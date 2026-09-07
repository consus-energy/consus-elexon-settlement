"""The HTTP endpoint the EMS reaches through Pub/Sub.

A push subscription posts here. The response code decides whether Pub/Sub
redelivers, and getting that wrong is worse than either extreme:

    2xx  acknowledged, never redelivered
    non-2xx  redelivered, with backoff, then to the dead letter queue

THE RULE: ACK ONCE THE INTENT IS DURABLE.

Not once it succeeded. An intent that was recorded but whose flows failed is
already PARTIAL, and retrying it is the gateway's job through
IntentService.retry -- which resends the archived bytes rather than rebuilding
them. Nacking would have Pub/Sub redeliver the same message, and the natural
key would make the second delivery a no-op that sends nothing at all. The
retry would never happen.

So nack ONLY when nothing was recorded: the database was unreachable, or the
service could not start. Those are transient and redelivery is the right
answer.

A malformed message is acked. Redelivering it produces the same failure
forever, which is how a poison pill blocks a subscription behind it -- and the
message behind it might be one that matters.

AUTHENTICATION IS NOT DONE HERE.

The Cloud Run service requires authentication, and only the push
subscription's service account holds run.invoker. An unauthenticated request
is rejected by Cloud Run before it reaches this process, so there is no token
to verify and no verification to get subtly wrong.

That means this endpoint MUST NOT be deployed with --allow-unauthenticated.
Anyone able to reach it could submit a settlement position.
"""

from __future__ import annotations

import logging
import os

from flask import Flask, jsonify, request

from .. import app as gateway_app
from .. import db, service
from ..archive import GcsArchive
from ..cli import _cipher
from ..app import require_env as _require

from . import messages
from ..outbound.ftp import FtpTransport
from ..outbound.sender import Sender
from ..outbound.submissions import Submitter
from ..outbound.transport import EncryptedTransport, Transport

log = logging.getLogger("consus.settlement.endpoint")


def create_app() -> Flask:
    """Build the Flask app and everything behind it.

    Assembled once at startup rather than per request. Cloud Run keeps a
    container warm between requests, and rebuilding the channel lookups on
    every message would add a database round trip to a path measured against
    Gate Closure.
    """
    flask_app = Flask(__name__)
    config = gateway_app.Config.from_env()
    dsn = _require("CONSUS_SETTLEMENT_DSN")

    def connect():
        return db.connect(dsn)

    transport = _transport()
    sender = Sender(
        connect=connect,
        archive=GcsArchive(bucket_name=_require("CONSUS_ARCHIVE_BUCKET")),
        transport=transport,
    )
    submitter = Submitter(connect=connect, sender=sender)

    with connect() as conn:
        channels = service.Channels(
            vtp_to_ecvaa=gateway_app.channel_for(
                conn, config, config.vtp, "EC", "UKDC"),
            agent_to_ecvaa=gateway_app.channel_for(
                conn, config, config.ecvna, "EC", "UKDC"),
            vtp_to_svaa=gateway_app.channel_for(
                conn, config, config.vtp,
                os.environ.get("CONSUS_SVAA_ROLE", "G"),
                _require("CONSUS_SVAA_PARTICIPANT")),
        )

    intent_service = service.IntentService(
        connect=connect, submitter=submitter, channels=channels
    )

    @flask_app.post("/intent")
    def receive():  # noqa: ANN202 - Flask view
        envelope = request.get_json(silent=True)
        if not isinstance(envelope, dict):
            # Not a Pub/Sub envelope at all. Acked: redelivering will not make
            # it one.
            log.error("request body is not a JSON object")
            return jsonify(error="expected a Pub/Sub push envelope"), 200

        pubsub_id = messages.message_id(envelope)

        try:
            payload = messages.unwrap(envelope)
            kind = messages.kind(payload)
        except messages.MessageError as exc:
            # Malformed. Acked deliberately: a message that can never be
            # parsed would otherwise block everything behind it, and what is
            # behind it may be a position that matters.
            log.error("message %s is malformed and will not be retried: %s",
                      pubsub_id, exc)
            return jsonify(error=str(exc)), 200

        try:
            if kind == messages.TRADING:
                outcome = intent_service.act(messages.to_intent(payload))
            else:
                outcome = intent_service.deliver(messages.to_delivered(payload))
        except messages.MessageError as exc:
            log.error("message %s failed validation: %s", pubsub_id, exc)
            return jsonify(error=str(exc)), 200
        except Exception as exc:  # noqa: BLE001
            # Nothing was recorded, or we cannot tell. This is the only case
            # where redelivery helps: the database was unreachable, a secret
            # was missing, something transient. Nacked so Pub/Sub tries again.
            log.exception("message %s could not be processed: %s", pubsub_id, exc)
            return jsonify(error="could not process"), 500

        # Recorded. Acked whatever the flows did -- retrying a recorded intent
        # is IntentService.retry's job, and a redelivery would be a no-op
        # against the natural key.
        log.info("message %s: intent %s is %s%s",
                 pubsub_id, outcome.intent_id, outcome.state,
                 " (duplicate)" if outcome.duplicate else "")

        return jsonify(
            intent_id=outcome.intent_id,
            state=outcome.state,
            duplicate=outcome.duplicate,
        ), 200

    @flask_app.get("/health")
    def health():  # noqa: ANN202
        """Liveness only.

        Deliberately does not touch the database. A health check that fails
        when Postgres is briefly unavailable would have Cloud Run cycle the
        container, which does not fix Postgres and loses whatever was in
        flight.
        """
        return jsonify(status="ok"), 200

    return flask_app


def _transport() -> Transport:
    """FTP, encrypted. No local fallback.

    cli._transport falls back to local directories when no host is set, which
    is right for development. Here it is not: an endpoint that accepts an
    intent and writes it to a directory nobody reads would report success and
    submit nothing.
    """
    inner = FtpTransport(
        host=_require("CONSUS_FTP_HOST"),
        port=int(os.environ.get("CONSUS_FTP_PORT", "21")),
        username=_require("CONSUS_FTP_USER"),
        password=open(_require("CONSUS_FTP_PASSWORD_FILE")).read().strip(),
        outbound_dir=_require("CONSUS_FTP_OUTBOUND_DIR"),
        inbound_dir=_require("CONSUS_FTP_INBOUND_DIR"),
        tls=os.environ.get("CONSUS_FTP_TLS", "1") != "0",
    )
    return EncryptedTransport(inner=inner, cipher=_cipher())


# gunicorn imports this.
app = create_app() if os.environ.get("CONSUS_SETTLEMENT_DSN") else None