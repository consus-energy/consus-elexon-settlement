# Cloud SQL Postgres, private IP only.
#
# Reachable from Cloud Run over direct VPC egress on the subnet in main.tf.
# No public IP: the instance has no route in from the internet at all, which
# is both the simplest security answer and the simplest thing to describe in
# the qualification assessment.

# --- private services access ------------------------------------------------
# Cloud SQL private IPs live in a range Google peers into our VPC. That range
# has to be reserved and the peering established before the instance exists.

resource "google_compute_global_address" "private_services" {
  name          = "${local.prefix}-private-services"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = google_compute_network.vpc.id
}

resource "google_service_networking_connection" "private_services" {
  network                 = google_compute_network.vpc.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_services.name]
}

# --- instance ---------------------------------------------------------------

resource "google_sql_database_instance" "main" {
  name             = "${local.prefix}-pg"
  region           = var.region
  database_version = "POSTGRES_16"

  # Guards against an accidental `terraform destroy` taking the settlement
  # record with it. Retention obligations run to 14 months.
  deletion_protection = var.db_deletion_protection

  depends_on = [google_service_networking_connection.private_services]

  settings {
    tier              = var.db_tier
    availability_type = var.db_availability_type
    disk_type         = "PD_SSD"
    disk_size         = 10
    disk_autoresize   = true

    ip_configuration {
      ipv4_enabled                                  = false
      private_network                               = google_compute_network.vpc.id
      ssl_mode                                      = "ENCRYPTED_ONLY"
      enable_private_path_for_google_cloud_services = true
    }

    backup_configuration {
      enabled                        = true
      start_time                     = "02:00"
      point_in_time_recovery_enabled = true
      location                       = var.region

      backup_retention_settings {
        retained_backups = 30
      }
    }

    maintenance_window {
      day          = 7 # Sunday
      hour         = 3
      update_track = "stable"
    }

    database_flags {
      name  = "log_min_duration_statement"
      value = "1000"
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_sql_database" "settlement" {
  name     = "settlement"
  instance = google_sql_database_instance.main.name
}

# --- credentials ------------------------------------------------------------

resource "random_password" "db" {
  length  = 32
  special = false # keeps the DSN free of characters needing escaping
}

resource "google_sql_user" "app" {
  name     = "settlement_app"
  instance = google_sql_database_instance.main.name
  password = random_password.db.result
}

resource "google_secret_manager_secret" "db_dsn" {
  secret_id = "${local.prefix}-db-dsn"

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "db_dsn" {
  secret = google_secret_manager_secret.db_dsn.id
  secret_data = join(" ", [
    "host=${google_sql_database_instance.main.private_ip_address}",
    "port=5432",
    "dbname=${google_sql_database.settlement.name}",
    "user=${google_sql_user.app.name}",
    "password=${random_password.db.result}",
    "sslmode=require",
  ])
}

resource "google_secret_manager_secret_iam_member" "gateway_db_dsn" {
  secret_id = google_secret_manager_secret.db_dsn.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.gateway.email}"
}

resource "google_project_iam_member" "gateway_sql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.gateway.email}"
}
