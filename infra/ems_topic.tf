# The EMS boundary, part one: the topic and who may publish to it.
#
# Split out from ems.tf.hold deliberately. Whether the EMS can publish an
# intent depends on exactly two things -- the topic exists, and their service
# account holds pubsub.publisher on it. The endpoint service, the push
# subscription, GPG and FTP all sit BEHIND the topic and have no bearing on
# that. They live in the held file so that standing up the bridge cannot be
# blocked by an unrelated secret or image being absent.
#
# When ems.tf.hold is unheld, these resources stay here and the held file
# stops declaring them.

resource "google_project_service" "pubsub" {
  project            = var.project_id
  service            = "pubsub.googleapis.com"
  disable_on_destroy = false
}

resource "google_pubsub_topic" "intents" {
  name = "${local.prefix}-intents"

  # Long enough to replay a day's decisions if the subscription is
  # misconfigured and messages are acked without being acted on. Shorter than
  # the archive retention, because these are instructions rather than the
  # settlement record -- BSC Section U1.6 applies to files, not to what
  # prompted them.
  message_retention_duration = "86400s"

  depends_on = [google_project_service.pubsub]
}

# Whoever publishes. The EMS runs in its own project, so this is a
# cross-project grant and the only access it has here. Publishing an intent is
# submitting a settlement position, so the list should be short and named.
resource "google_pubsub_topic_iam_member" "publishers" {
  for_each = toset(var.ems_publishers)

  topic  = google_pubsub_topic.intents.name
  role   = "roles/pubsub.publisher"
  member = each.key
}

# --- bridge test only -------------------------------------------------------
#
# TEMPORARY. Pub/Sub retains nothing for a topic with no subscription, so
# publishing before one exists discards the messages silently: the publish
# succeeds, doctor reports may_publish true, and there is nothing to read.
# That looks like a broken bridge when it is not.
#
# This subscription exists so the first published intents can be pulled and
# run through ems.messages to prove the contract, not just the grant.
#
# DELETE IT when the push subscription in ems.tf.hold lands. Two subscriptions
# on one topic means every intent is delivered twice, and the second copy has
# nowhere to go.

resource "google_pubsub_subscription" "bridge_test" {
  count = var.ems_bridge_test_subscription ? 1 : 0

  name  = "${local.prefix}-intents-bridge-test"
  topic = google_pubsub_topic.intents.name

  # Default is 31 days of inactivity, after which Pub/Sub deletes the
  # subscription. A quiet week between test runs would remove it without
  # notice, and the next publish would be discarded as above.
  expiration_policy {
    ttl = ""
  }

  message_retention_duration = "86400s"
}
