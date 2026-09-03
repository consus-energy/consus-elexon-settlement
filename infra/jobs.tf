# Cloud Run Jobs and their schedules.
#
# Jobs rather than a service: collect and sweep are scheduled, finite and
# idempotent. A service would mean holding an HTTP process alive to do work on
# a timer, which is the wrong shape and costs more.
#
# Both run inside the VPC so they leave through the reserved egress address.
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
}

resource "google_cloud_run_v2_job" "gateway" {
  for_each = {
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
      # scheduled frequently enough that the next run is the retry.
      max_retries = 0

      vpc_access {
        connector = google_vpc_access_connector.gateway.id
        egress    = "ALL_TRAFFIC"
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

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }
      }
    }
  }

  depends_on = [google_project_service.run_apis]
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

resource "google_service_account" "scheduler" {
  account_id   = "${local.prefix}-sched"
  display_name = "Cloud Scheduler invoker (${var.env})"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_invoke" {
  # Keys stated literally rather than derived from the jobs resource.
  # for_each cannot iterate over something that does not exist yet, and
  # Terraform needs the key set at plan time.
  for_each = toset(["collect", "sweep"])

  name     = google_cloud_run_v2_job.gateway[each.key].name
  location = google_cloud_run_v2_job.gateway[each.key].location
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "gateway" {
  for_each = {
    collect = "*/5 * * * *"
    sweep   = "*/15 * * * *"
  }

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