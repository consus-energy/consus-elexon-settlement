# The EMS boundary: a Pub/Sub topic, a push subscription, and the Cloud Run
# service it pushes to.
#
# The EMS publishes decisions as facts -- what it expects, what it traded,
# what it delivered -- and knows nothing else about settlement. It has publish
# rights on the topic and no other access to this project.
#
# PUSH, NOT PULL. Gate Closure is one hour, and a five-minute poll would spend
# a twelfth of that window on latency for no reason. Push also means no
# long-lived subscriber process: the service scales to zero between messages.
#
# SYNCHRONOUS. The endpoint acts on the intent and returns, so the EMS gets
# immediate feedback. Three file builds and three uploads inside an HTTP
# request is slow, but the volume is low and the alternative -- accept, ack,
# process later -- makes a failure after the ack invisible to the EMS.

resource "google_project_service" "pubsub" {
  project            = var.project_id
  service            = "pubsub.googleapis.com"
  disable_on_destroy = false
}

# --- the service ------------------------------------------------------------

resource "google_cloud_run_v2_service" "ems" {
  name     = "${local.prefix}-ems"
  location = var.region

  # Internal only. Nothing outside the VPC and Google's own services can reach
  # it, which removes the public internet from the attack surface entirely.
  ingress = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  deletion_protection = false

  template {
    service_account = google_service_account.gateway.email

    # Files go out through the reserved egress address that Elexon whitelist,
    # the same as the Jobs. A service bypassing the VPC would be refused at
    # the far end and it would look like a transport fault.
    vpc_access {
      connector = google_vpc_access_connector.gateway.id
      egress    = "ALL_TRAFFIC"
    }

    scaling {
      min_instance_count = 0
      # One at a time. Sequence allocation is transactional and would be
      # correct under concurrency, but there is no volume to justify testing
      # that in production -- a handful of messages per settlement period.
      max_instance_count = 2
    }

    containers {
      image = local.image

      # The endpoint, not the CLI. Gunicorn rather than Flask's development
      # server, which is single-threaded and says so on startup.
      command = ["gunicorn"]
      args = [
        "--bind=:8080",
        "--workers=1",
        # Three builds and three FTP uploads. The default 30s would time out
        # on a slow link and Pub/Sub would redeliver -- which the natural key
        # makes harmless, but routinely relying on that is not a design.
        "--timeout=120",
        "--access-logfile=-",
        "consus_elexon_settlement.ems.endpoint:app",
      ]

      dynamic "env" {
        for_each = local.job_env
        content {
          name  = env.value.name
          value = env.value.value
        }
      }

      env {
        name = "CONSUS_SETTLEMENT_DSN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.db_dsn.secret_id
            version = "latest"
          }
        }
      }

      env {
        name  = "CONSUS_GPG_PRIVATE_KEY_FILE"
        value = "/secrets/gpg-key/private-key"
      }

      env {
        name  = "CONSUS_GPG_PASSPHRASE_FILE"
        value = "/secrets/gpg-passphrase/passphrase"
      }

      env {
        name  = "CONSUS_GPG_RECIPIENT_KEY_FILE"
        value = "/secrets/gpg-recipient/recipient-key"
      }

      env {
        name  = "CONSUS_GPG_KEY"
        value = var.ecvna_participant_id
      }

      # Role code G is confirmed from the P0237 physical file specification in
      # the SVA Data Catalogue. The participant id is not: the spec writes it
      # as "Id of SVAA", and it is an open question with Elexon.
      env {
        name  = "CONSUS_SVAA_ROLE"
        value = var.svaa_role_code
      }

      env {
        name  = "CONSUS_SVAA_PARTICIPANT"
        value = var.svaa_participant_id
      }

      volume_mounts {
        name       = "gpg-key"
        mount_path = "/secrets/gpg-key"
      }

      volume_mounts {
        name       = "gpg-passphrase"
        mount_path = "/secrets/gpg-passphrase"
      }

      volume_mounts {
        name       = "gpg-recipient"
        mount_path = "/secrets/gpg-recipient"
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      startup_probe {
        http_get {
          path = "/health"
          port = 8080
        }
        # create_app builds the channels and reads secrets at import, so
        # startup is slower than a bare Flask app.
        initial_delay_seconds = 10
        period_seconds        = 5
        failure_threshold     = 6
      }
    }

    volumes {
      name = "gpg-key"
      secret {
        secret = google_secret_manager_secret.gpg_private_key.secret_id
        items {
          version = "latest"
          path    = "private-key"
          mode    = 256
        }
      }
    }

    volumes {
      name = "gpg-passphrase"
      secret {
        secret = google_secret_manager_secret.gpg_passphrase.secret_id
        items {
          version = "latest"
          path    = "passphrase"
          mode    = 256
        }
      }
    }

    volumes {
      name = "gpg-recipient"
      secret {
        secret = google_secret_manager_secret.gpg_recipient_key.secret_id
        items {
          version = "latest"
          path    = "recipient-key"
          mode    = 292
        }
      }
    }
  }

  depends_on = [
    google_project_service.run_apis,
    google_secret_manager_secret_iam_member.gateway_gpg,
  ]
}

# --- the topic --------------------------------------------------------------

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

resource "google_pubsub_topic" "dead_letter" {
  name = "${local.prefix}-intents-dead-letter"

  # Seven days. A message here has failed five deliveries and needs a human;
  # a day would not survive a weekend.
  message_retention_duration = "604800s"

  depends_on = [google_project_service.pubsub]
}

resource "google_pubsub_subscription" "intents" {
  name  = "${local.prefix}-intents"
  topic = google_pubsub_topic.intents.name

  push_config {
    push_endpoint = google_cloud_run_v2_service.ems.uri

    # Pub/Sub signs the request with this identity, and Cloud Run rejects
    # anything not carrying a valid token from a principal holding
    # run.invoker. That check happens before the request reaches the process,
    # so the endpoint does no authentication of its own -- and therefore
    # cannot get it subtly wrong.
    oidc_token {
      service_account_email = google_service_account.pubsub_push.email
      audience              = google_cloud_run_v2_service.ems.uri
    }
  }

  # Slightly longer than gunicorn's timeout, so a slow request completes
  # rather than being redelivered while still running.
  ack_deadline_seconds = 180

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = 5
  }

  # A message unacked for a day is not going to be acked. It belongs in the
  # dead letter queue, and this is the backstop if the policy above does not
  # catch it.
  expiration_policy {
    ttl = ""
  }
}

# --- identities -------------------------------------------------------------

resource "google_service_account" "pubsub_push" {
  account_id   = "${local.prefix}-push"
  display_name = "Pub/Sub push to the EMS endpoint (${var.env})"
}

resource "google_cloud_run_v2_service_iam_member" "push_invoker" {
  name     = google_cloud_run_v2_service.ems.name
  location = google_cloud_run_v2_service.ems.location
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.pubsub_push.email}"
}

# Pub/Sub's own agent needs to mint tokens as the push account and to write to
# the dead letter topic. Both are Google-managed grants that are easy to
# forget, and their absence shows as messages silently failing to deliver.
resource "google_service_account_iam_member" "pubsub_token_creator" {
  service_account_id = google_service_account.pubsub_push.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_topic_iam_member" "dead_letter_publisher" {
  topic  = google_pubsub_topic.dead_letter.name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_subscription_iam_member" "dead_letter_subscriber" {
  subscription = google_pubsub_subscription.intents.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

# Whoever publishes. The EMS runs in its own project, so this is a
# cross-project grant and the only access it has here.
resource "google_pubsub_topic_iam_member" "publishers" {
  for_each = toset(var.ems_publishers)

  topic  = google_pubsub_topic.intents.name
  role   = "roles/pubsub.publisher"
  member = each.key
}

data "google_project" "this" {
  project_id = var.project_id
}

# --- alerting ---------------------------------------------------------------

resource "google_monitoring_alert_policy" "dead_letter" {
  display_name = "${local.prefix}: intents in the dead letter queue"
  combiner     = "OR"

  documentation {
    content = <<-EOT
      A message failed five delivery attempts and has been dead-lettered.

      This means the endpoint returned non-2xx five times, which happens only
      when the intent could not be RECORDED -- a database outage, a missing
      secret. A malformed message is acknowledged rather than retried, so it
      never reaches here.

      So a message in this queue is a decision the EMS made that we have no
      record of. If Gate Closure has not passed, it can be republished.

        gcloud pubsub subscriptions pull ${local.prefix}-intents-dead-letter --limit 10
    EOT
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "messages in the dead letter topic"

    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"pubsub_topic\"",
        "resource.label.topic_id = \"${google_pubsub_topic.dead_letter.name}\"",
        "metric.type = \"pubsub.googleapis.com/topic/send_message_operation_count\"",
      ])

      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  notification_channels = local.channels

  # No auto_close: a dead-lettered intent does not resolve itself.
  depends_on = [google_project_service.monitoring]
}