# Cloud Run Jobs and their schedules.
#
# Jobs rather than a service: collect and sweep are scheduled, finite and
# idempotent. A service would mean holding an HTTP process alive to do work on
# a timer, which is the wrong shape and costs more.
#
# All run inside the VPC so they leave through the reserved egress address.
# Elexon whitelists that address, so a Job that bypassed the VPC would be
# refused at the far end -- and would look like a transport fault rather than
# a configuration one.

resource "google_project_service" "run_apis" {
  for_each = toset([
    "run.googleapis.com",
    "cloudscheduler.googleapis.com",
    "artifactregistry.googleapis.com",
    "vpcaccess.googleapis.com",
  ])

  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = local.prefix
  format        = "DOCKER"
  description   = "Settlement gateway images (${var.env})"

  depends_on = [google_project_service.run_apis]
}

# Serverless VPC connector. Without this a Job gets an ephemeral egress
# address and Elexon rejects the connection. This is the single piece of
# infrastructure most likely to be forgotten and hardest to diagnose when it
# is.
resource "google_vpc_access_connector" "gateway" {
  name   = "${local.prefix}-vpc"
  region = var.region

  # A dedicated /28, not the main subnet. GCP requires the connector to own
  # its range exclusively -- pointing it at a shared subnet fails with
  # "Subnets used for VPC connectors must have a netmask of 28".
  ip_cidr_range = var.connector_cidr
  network       = google_compute_network.vpc.name

  min_instances = 2
  max_instances = 3

  depends_on = [google_project_service.run_apis]
}

locals {
  image = "${var.region}-docker.pkg.dev/${var.project_id}/${local.prefix}/gateway:${var.image_tag}"

  # Every Job gets the same environment. Differences between Jobs belong in
  # the command, not in configuration: two Jobs configured differently is two
  # things to keep in step.
  job_env = [
    { name = "CONSUS_ENVIRONMENT", value = var.settlement_environment },
    { name = "CONSUS_VTP_PARTICIPANT", value = var.vtp_participant_id },
    { name = "CONSUS_ECVNA_PARTICIPANT", value = var.ecvna_participant_id },
    { name = "CONSUS_ARCHIVE_BUCKET", value = google_storage_bucket.archive.name },
    { name = "CONSUS_LOG_LEVEL", value = var.log_level },
  ]

  # Jobs that run on a schedule. migrate is excluded deliberately: a schema
  # change is a deliberate act, and a migration applying itself on a timer
  # would land at an unpredictable moment relative to a deployment.
  scheduled_jobs = {
    collect = "*/5 * * * *"
    sweep   = "*/15 * * * *"
  }
}

resource "google_cloud_run_v2_job" "gateway" {
  for_each = {
    # Run on demand, from a deployment step or by hand. Safe to repeat:
    # applied migrations are recorded with a checksum, so a second run applies
    # nothing and an edited migration fails loudly.
    migrate = {
      args    = ["migrate"]
      timeout = "600s"
    }
    collect = {
      args    = ["collect"]
      timeout = "600s"
    }
    sweep = {
      args    = ["sweep"]
      timeout = "300s"
    }
  }

  name     = "${local.prefix}-${each.key}"
  location = var.region

  template {
    template {
      service_account = google_service_account.gateway.email
      timeout         = each.value.timeout

      # One attempt. A retry that re-runs collect would re-acknowledge files
      # already acknowledged; a retry of sweep would re-alert. Both are
      # scheduled frequently enough that the next run is the retry, and
      # migrate is deliberate rather than automatic.
      max_retries = 0

      vpc_access {
        connector = google_vpc_access_connector.gateway.id
        egress    = "ALL_TRAFFIC"
      }

      # Keys as mounted files rather than environment variables. A multi-line
      # armoured key does not survive an environment variable cleanly, and
      # environment variables are readable through /proc. One volume per
      # secret: Cloud Run mounts a secret as a volume, not a directory of
      # several.
      #
      # migrate does not need these, but shares them rather than duplicating
      # the whole resource for one Job. The service account is the same
      # either way, so mounting them grants nothing it did not already have.
      volumes {
        name = "gpg-key"
        secret {
          secret = google_secret_manager_secret.gpg_private_key.secret_id
          items {
            version = "latest"
            path    = "private-key"
            mode    = 256 # 0400 octal
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
            mode    = 292 # 0444 octal, this one is public
          }
        }
      }

      containers {
        image = local.image
        args  = each.value.args

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

        # Where the entrypoint looks for the mounted keys.
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

        # The key name gpg signs with. Our ECVNA id, because that is what the
        # key was generated as and what Central Services hold for us.
        env {
          name  = "CONSUS_GPG_KEY"
          value = var.ecvna_participant_id
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
      }
    }
  }

  depends_on = [
    google_project_service.run_apis,
    # IAM must be in place before Cloud Run validates the secret mounts, and
    # Terraform cannot infer that from the secret_id reference alone: it sees
    # a string, not a grant.
    google_secret_manager_secret_iam_member.gateway_gpg,
  ]
}

# --- schedules --------------------------------------------------------------
#
# collect runs every five minutes. Elexon sends feedback continuously and a
# rejection is only useful before Gate Closure, so the interval is the delay
# we are prepared to add to noticing one.
#
# sweep runs every fifteen. Its job is to notice silence, and the urgency
# judgement is per settlement period rather than per run, so a coarser
# interval loses nothing.
#
# migrate has no schedule. It is invoked deliberately.

resource "google_service_account" "scheduler" {
  account_id   = "${local.prefix}-sched"
  display_name = "Cloud Scheduler invoker (${var.env})"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_invoke" {
  # Only the scheduled jobs. The scheduler has no reason to be able to run a
  # migration, and granting it would mean a misconfigured schedule could apply
  # one unattended.
  for_each = local.scheduled_jobs

  name     = google_cloud_run_v2_job.gateway[each.key].name
  location = google_cloud_run_v2_job.gateway[each.key].location
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "gateway" {
  for_each = local.scheduled_jobs

  name     = "${local.prefix}-${each.key}"
  region   = var.region
  schedule = each.value

  # Europe/London so a schedule expressed in local terms stays put across a
  # clock change. Nothing here is anchored to a settlement period, but a
  # schedule that silently shifts by an hour twice a year is worth avoiding
  # on principle.
  time_zone = "Europe/London"

  attempt_deadline = "320s"

  retry_config {
    retry_count = 1
  }

  http_target {
    http_method = "POST"
    uri = join("", [
      "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/",
      "namespaces/${var.project_id}/jobs/",
      google_cloud_run_v2_job.gateway[each.key].name,
      ":run",
    ])

    oauth_token {
      service_account_email = google_service_account.scheduler.email
    }
  }

  depends_on = [google_project_service.run_apis]
}