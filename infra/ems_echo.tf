# A receive-only endpoint, so the EMS push path can be tested before
# settlement exists.
#
# TEMPORARY. Delete this file and src/consus_elexon_settlement/ems/echo.py
# together when the real endpoint in ems.tf.hold is deployed.
#
# WHY THIS EXISTS. ems.tf.hold cannot be applied: create_app builds the
# transport at import and _transport requires CONSUS_FTP_HOST plus four more
# variables Elexon have not supplied. So the push subscription -- the path
# that actually carries an intent -- is untestable, and the alternative is
# draining a pull subscription by hand and reading base64.
#
# The echo service imports ems.messages and nothing else from the package.
# That module is pure, so this service has no database, no archive, no
# keyring and no submitter: it CANNOT record an intent or build a file. The
# capability is absent from the import graph rather than declined at runtime.
#
# TWO SUBSCRIPTIONS ON ONE TOPIC, DELIBERATELY. ems_topic.tf warns that a
# second subscription means every intent is delivered twice with nowhere for
# the copy to go. During the bridge test both copies have somewhere to go and
# the pair is the point: the pull subscription shows the BYTES, this one
# proves they PARSE at the far end. They are enabled together and deleted
# together.

# The switch lives HERE rather than in variables.tf, unlike
# ems_bridge_test_subscription. That is deliberate for a file meant to be
# deleted: `rm ems_echo.tf` takes the variable with it, where a declaration in
# variables.tf would outlive the thing it configures.
variable "ems_echo_enabled" {
  description = <<-EOT
    Stand up the receive-only echo endpoint and a push subscription to it.
    Off by default and not a thing to leave on: it exists to prove the bridge,
    and the real endpoint replaces it. Set false, apply, then delete this file
    and the echo module.
  EOT
  type    = bool
  default = false
}

# --- the service ------------------------------------------------------------

resource "google_service_account" "ems_echo" {
  count = var.ems_echo_enabled ? 1 : 0

  account_id   = "${local.prefix}-echo"
  display_name = "Receive-only EMS echo endpoint (${var.env})"
  description  = "Parses published intents and reports. Holds NO project roles, deliberately: it reads nothing and writes nothing."
}

# No roles granted to that account anywhere in this file. That is not an
# omission -- the service touches no database, no bucket and no secret, so a
# role would be a capability nobody asked for on an endpoint reachable from
# outside this repository.

resource "google_cloud_run_v2_service" "ems_echo" {
  count = var.ems_echo_enabled ? 1 : 0

  name     = "${local.prefix}-ems-echo"
  location = var.region

  # Same ingress as the real endpoint in ems.tf.hold, and mirroring it is part
  # of the point: a push subscription in this project counts as internal
  # traffic, and if that turns out to be wrong we would rather find out on the
  # echo than on the endpoint that carries settlement positions.
  ingress = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  template {
    service_account = google_service_account.ems_echo[0].email

    # NO vpc_access. The real endpoint needs the reserved egress address
    # because it uploads to Elexon; this one never makes an outbound
    # connection, so the connector would be an attachment to a network it has
    # no reason to reach.

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image = local.image

      # `command` REPLACES the image's ENTRYPOINT, which is what we want:
      # docker-entrypoint.sh imports GPG keys from mounted secrets before
      # exec'ing the CLI, and there are no secrets mounted here. Bypassing it
      # is why this service needs none of the three GPG secrets to start.
      command = ["gunicorn"]
      args = [
        "--bind=:8080",
        "--workers=1",
        # Parsing a 50-period profile is microseconds. The real endpoint needs
        # 120s for three file builds and three uploads; this needs none of it.
        "--timeout=30",
        "--access-logfile=-",
        "consus_elexon_settlement.ems.echo:app",
      ]

      # NO env block, and no secret mounts. `echo.create_app` reads no
      # configuration at all, so there is no value whose absence would make it
      # start degraded -- unlike endpoint.py, where any of nine missing
      # variables stops it dead.

      # 512Mi, matching the real endpoint in ems.tf.hold. Not a guess at what
      # gunicorn needs -- Cloud Run REFUSES less than 512Mi when the CPU is
      # always allocated, which it is by default on a v2 service, and 256Mi
      # failed the apply with exactly that error. Parsing a 50-period profile
      # needs a fraction of it; matching the real endpoint costs nothing and
      # removes a difference nobody would remember was deliberate.
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
        initial_delay_seconds = 3
        period_seconds        = 3
        failure_threshold     = 5
      }
    }
  }

  depends_on = [google_project_service.run_apis]
}

# --- the push subscription --------------------------------------------------

resource "google_service_account" "echo_push" {
  count = var.ems_echo_enabled ? 1 : 0

  # NOT `${local.prefix}-push`. That name belongs to the real endpoint's push
  # identity in ems.tf.hold, and unholding that file while this one exists
  # would collide.
  account_id   = "${local.prefix}-echo-push"
  display_name = "Pub/Sub push to the echo endpoint (${var.env})"
}

resource "google_cloud_run_v2_service_iam_member" "echo_push_invoker" {
  count = var.ems_echo_enabled ? 1 : 0

  name     = google_cloud_run_v2_service.ems_echo[0].name
  location = google_cloud_run_v2_service.ems_echo[0].location
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.echo_push[0].email}"
}

# Pub/Sub's own agent mints tokens as the push account. A Google-managed grant
# that is easy to forget, and whose absence shows as messages that silently
# never arrive -- which on this service would look exactly like a broken
# bridge.
resource "google_service_account_iam_member" "echo_pubsub_token_creator" {
  count = var.ems_echo_enabled ? 1 : 0

  service_account_id = google_service_account.echo_push[0].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.echo[0].number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_subscription" "echo" {
  count = var.ems_echo_enabled ? 1 : 0

  name  = "${local.prefix}-intents-echo"
  topic = google_pubsub_topic.intents.name

  push_config {
    # THE PATH MATTERS. `.uri` is the service root and the route is /intent, so
    # without it every push is a 404 -- which Pub/Sub treats as a failure and
    # retries, so the symptom is not "nothing arrives" but a flood of retries
    # and, on a subscription with one, a dead letter queue full of intents.
    # Measured: the first real publish produced `POST / HTTP/1.1" 404` twice a
    # second until the message expired.
    #
    # The AUDIENCE stays the bare URI. It is the OIDC audience Cloud Run
    # validates the token against, which is the service, not the route.
    push_endpoint = "${google_cloud_run_v2_service.ems_echo[0].uri}/intent"

    oidc_token {
      service_account_email = google_service_account.echo_push[0].email
      audience              = google_cloud_run_v2_service.ems_echo[0].uri
    }
  }

  ack_deadline_seconds = 60

  # NO dead_letter_policy, and no retry beyond the default. The echo
  # acknowledges everything it receives, parse failures included -- a message
  # it cannot read is the finding, and redelivering it would produce the same
  # finding forever. There is nothing here for a dead letter queue to hold.

  expiration_policy {
    ttl = ""
  }
}

data "google_project" "echo" {
  count = var.ems_echo_enabled ? 1 : 0

  project_id = var.project_id
}

output "ems_echo_url" {
  description = "The echo endpoint, for reading its logs. Empty when disabled."
  value       = var.ems_echo_enabled ? google_cloud_run_v2_service.ems_echo[0].uri : ""
}
