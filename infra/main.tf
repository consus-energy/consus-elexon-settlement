locals {
  prefix = "${var.name_prefix}-${var.env}"
}

# --- APIs -------------------------------------------------------------------
# Only what this file needs. Add run/artifactregistry when there is an image.

resource "google_project_service" "apis" {
  for_each = toset([
    "compute.googleapis.com",
    "storage.googleapis.com",
    "secretmanager.googleapis.com",
    "sqladmin.googleapis.com",
    "servicenetworking.googleapis.com",
  ])

  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

# --- Network ----------------------------------------------------------------
# Egress only. Pull method means nothing initiates a connection to us, so
# there are no ingress rules and no external addresses on any instance.

resource "google_compute_network" "vpc" {
  name                    = local.prefix
  auto_create_subnetworks = false
  depends_on              = [google_project_service.apis]
}

resource "google_compute_subnetwork" "subnet" {
  name                     = local.prefix
  network                  = google_compute_network.vpc.id
  region                   = var.region
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true
}

# The long-lead item. Elexon whitelist this address; reserve it before
# anything needs it, and do not let it change.
resource "google_compute_address" "egress" {
  name         = "${local.prefix}-egress"
  region       = var.region
  address_type = "EXTERNAL"
  depends_on   = [google_project_service.apis]

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_compute_router" "router" {
  name    = local.prefix
  region  = var.region
  network = google_compute_network.vpc.id
}

resource "google_compute_router_nat" "nat" {
  name   = local.prefix
  router = google_compute_router.router.name
  region = var.region

  # MANUAL_ONLY pins egress to the reserved address above. AUTO_ONLY would
  # hand out ephemeral addresses and silently break the whitelist.
  nat_ip_allocate_option = "MANUAL_ONLY"
  nat_ips                = [google_compute_address.egress.self_link]

  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"

  subnetwork {
    name                    = google_compute_subnetwork.subnet.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

# --- Raw file archive -------------------------------------------------------
# Every file sent and received, as bytes. Audit evidence for qualification.

resource "google_storage_bucket" "archive" {
  name                        = "${local.prefix}-archive-${var.project_id}"
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  force_destroy               = false
  depends_on                  = [google_project_service.apis]

  versioning {
    enabled = true
  }

  dynamic "retention_policy" {
    for_each = var.archive_retention_days == null ? [] : [1]
    content {
      retention_period = var.archive_retention_days * 86400
      is_locked        = var.archive_retention_locked
    }
  }
}

# --- Identity ---------------------------------------------------------------

resource "google_service_account" "gateway" {
  account_id   = "${local.prefix}-gw"
  display_name = "ECVAA settlement gateway (${var.env})"
}

resource "google_storage_bucket_iam_member" "gateway_archive" {
  bucket = google_storage_bucket.archive.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.gateway.email}"
}
