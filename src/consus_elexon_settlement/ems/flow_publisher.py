"""Putting a flow event on the EMS's topic. One way, no retry.

THE MIRROR OF `ems/endpoint.py`, POINTING THE OTHER WAY. Intents arrive here
over a push subscription; flow events go back over a topic in the EMS project,
which they subscribe to. Two topics, each owned by the project that RECEIVES on
it -- the same shape `infra/ems_topic.tf` already uses for the inbound side,
and the reason a cross-project grant is the only access either side needs.

WHAT THE FAR SIDE DOES WITH THE BYTES, read from
`services/vtp/src/vtp/return_channel.py` in consus-prod rather than assumed:
the raw JSON is parsed by `parse_flow_event`, and a message it cannot believe
raises `FlowEventError`, which is RECORDED AND ACKED -- never nacked. Same
reasoning as our own endpoint: redelivering a malformed message reproduces the
failure forever and blocks the subscription behind it.

NO ATTRIBUTES. Their parser reads the message body and nothing else, so an
attribute we set would be silently ignored -- the worst place to put
information.

IT DOES NOT RETRY, AND IT DOES NOT RAISE INTO ITS CALLER.

Every caller is a settlement handler mid-transaction: the WMAN rejection
handler, the file sender. A publish failure there must not lose the state
change it accompanies -- a lost rejection is a settlement problem, a missed
flow event is a reporting one, and the EMS's gate fails SAFE without the event
(no evidence means no dispatch). So `publish` returns whether it worked and
logs loudly when it did not. Same best-effort contract as the intent callback
in `inbound/handlers.py`, and for the same reason.

CONFIGURATION ABSENT MEANS DISABLED, LOUDLY, ONCE. A deployment with no topic
configured is the normal state until the EMS side is stood up, and a service
that refused to start without it would be unstartable today. `from_env`
returns None and the caller logs a single line saying the channel is off --
rather than an error per event, which is how a real failure gets lost.
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("consus.settlement.ems.flow_publisher")

#: The full topic path, e.g. `projects/consus-ems/topics/ems-flow-events`.
#: NOT assembled from a project and a name: a topic in the wrong project is a
#: message sent to somebody else's system, and one string is one thing to get
#: right rather than two.
TOPIC_ENV = "CONSUS_EMS_FLOW_TOPIC"


class FlowPublisher:
    """Publishes flow events to the EMS. Holds a client and a topic path."""

    def __init__(self, publisher, topic: str) -> None:
        self._publisher = publisher
        self._topic = topic

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "FlowPublisher | None":
        """Build from the environment, or None when no topic is configured.

        The import is LAZY and deliberately so, matching `archive.GcsArchive`:
        `google-cloud-pubsub` is a deployment dependency, and a unit test of
        the message contract should not need it installed or credentialed.
        """
        topic = (env or os.environ).get(TOPIC_ENV, "").strip()
        if not topic:
            return None
        from google.cloud import pubsub_v1

        return cls(pubsub_v1.PublisherClient(), topic)

    def publish(self, message: dict) -> bool:
        """Publish one flow event. True if the server accepted it.

        NEVER RAISES. See the module docstring: every caller is mid-transaction
        on a state change that matters more than this message does.
        """
        try:
            future = self._publisher.publish(
                self._topic, json.dumps(message, separators=(",", ":")).encode("utf-8")
            )
            message_id = future.result(timeout=30)
        except Exception:
            log.exception(
                "flow_event.publish_failed",
                extra={
                    "topic": self._topic,
                    "flow": message.get("flow"),
                    "event": message.get("event"),
                    "bmu_id": message.get("bmu_id"),
                    "settlement_date": message.get("settlement_date"),
                    "settlement_period": message.get("settlement_period"),
                },
            )
            return False
        log.info(
            "flow_event.published",
            extra={
                "message_id": message_id,
                "flow": message.get("flow"),
                "event": message.get("event"),
                "bmu_id": message.get("bmu_id"),
                "settlement_period": message.get("settlement_period"),
            },
        )
        return True

    def publish_all(self, messages: list[dict]) -> int:
        """Publish many, returning how many landed.

        ONE FILE IS MANY EVENTS. A WMAN file carries every BM Unit and period
        we are notifying, and the EMS's gate is per BM Unit per period -- so
        one send produces one event per row in it. Partial success is the
        normal failure mode and the count is what a caller logs.
        """
        return sum(1 for message in messages if self.publish(message))
