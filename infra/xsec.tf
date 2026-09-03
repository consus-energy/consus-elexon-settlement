# The XSec node.
#
# XSec is Elexon's encryption software, mandated by BSCP70 Appendix 1 and
# supplied by BSC CSA with the communications order. Every file exchanged with
# central systems is signed and encrypted through it.
#
# It runs only on Windows: the user guide states it is "explicitly not
# supported on any .NET based environment on any non-Microsoft Windows
# Operating System". So a Linux-only deployment is not available to us, and
# this VM exists because the code requires it rather than because we chose it.
#
# It is deliberately a small machine doing two things -- encrypt, and move
# bytes -- with all settlement logic staying on Cloud Run where the tests are.
# The less that runs here, the less there is to patch.
#
# Handover between the Linux jobs and this node is over the shared bucket
# below. Whether the XSec participant folders can be network shares is an open
# question with Elexon; if they can, this bucket is replaced by a mounted
# share and the sync agent goes away.

resource "google_project_service" "xsec_apis" {
  for_each = toset([
    "iap.googleapis.com",
    "oslogin.googleapis.com",
  ])

  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

# --- handover storage -------------------------------------------------------

# Separate from the settlement archive. The archive is the audit record and is
# retained for 40 months under BSC Section U1.6; this is a queue, and anything
# left here more than a day old is a stuck file rather than a record.
resource "google_storage_bucket" "handover" {
  name                        = "${local.prefix}-handover-${var.project_id}"
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  force_destroy               = true

  lifecycle_rule {
    condition { age = 7 }
    action { type = "Delete" }
  }

  depends_on = [google_project_service.apis]
}

# --- identity ---------------------------------------------------------------

resource "google_service_account" "xsec" {
  account_id   = "${local.prefix}-xsec"
  display_name = "XSec encryption node (${var.env})"
}

# Read and write the handover bucket, and nothing else. This node never
# touches the settlement database or the archive: it encrypts bytes and moves
# them. Keeping its reach narrow means a compromise here does not reach the
# settlement record.
resource "google_storage_bucket_iam_member" "xsec_handover" {
  bucket = google_storage_bucket.handover.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.xsec.email}"
}

# The Cloud Run jobs write outbound files here and read inbound ones back.
resource "google_storage_bucket_iam_member" "gateway_handover" {
  bucket = google_storage_bucket.handover.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.gateway.email}"
}

# --- the instance -----------------------------------------------------------

resource "google_compute_instance" "xsec" {
  name         = "${local.prefix}-xsec"
  machine_type = var.xsec_machine_type
  zone         = var.xsec_zone

  # Windows Server with a desktop, because XSecManager is a GUI and
  # configuration is done through it. Core would be a smaller attack surface
  # but cannot run the manager, and configuration is not a one-off: keys are
  # rotated and participants are added.
  boot_disk {
    initialize_params {
      image = var.xsec_image
      size  = 50
      type  = "pd-balanced"
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.subnet.id
    # No access_config block, so no external address. Outbound goes through
    # Cloud NAT and leaves on the reserved egress IP that Elexon whitelist.
    # Inbound is via IAP only.
  }

  service_account {
    email  = google_service_account.xsec.email
    scopes = ["cloud-platform"]
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  # The private key lives on this disk. Deletion protection guards against an
  # accidental terraform destroy taking it, which would mean repeating the key
  # exchange with Elexon.
  deletion_protection = var.xsec_deletion_protection

  # Not part of the settlement record, but the node is in the send path, so
  # its loss stops all outbound traffic. Snapshots make rebuilding a restore
  # rather than a reinstall-and-re-exchange.
  labels = {
    role = "xsec"
    env  = var.env
  }

  depends_on = [google_project_service.xsec_apis]
}

# --- access -----------------------------------------------------------------

# RDP over Identity-Aware Proxy rather than a public address. The instance has
# no external IP, so this is the only route in, and access is by IAM rather
# than by knowing an address.
resource "google_compute_firewall" "xsec_iap_rdp" {
  name    = "${local.prefix}-xsec-iap-rdp"
  network = google_compute_network.vpc.name

  allow {
    protocol = "tcp"
    ports    = ["3389"]
  }

  # The fixed range IAP forwards from. Not a guess: Google publish it.
  source_ranges = ["35.235.240.0/20"]
  target_service_accounts = [google_service_account.xsec.email]
}

resource "google_iap_tunnel_instance_iam_member" "xsec_rdp" {
  for_each = toset(var.xsec_admins)

  zone     = google_compute_instance.xsec.zone
  instance = google_compute_instance.xsec.name
  role     = "roles/iap.tunnelResourceAccessor"
  member   = each.key
}

# --- snapshots --------------------------------------------------------------

resource "google_compute_resource_policy" "xsec_snapshots" {
  name   = "${local.prefix}-xsec-snapshots"
  region = var.region

  snapshot_schedule_policy {
    schedule {
      daily_schedule {
        days_in_cycle = 1
        start_time    = "03:00"
      }
    }

    retention_policy {
      max_retention_days    = 14
      on_source_disk_delete = "KEEP_AUTO_SNAPSHOTS"
    }
  }
}

resource "google_compute_disk_resource_policy_attachment" "xsec_snapshots" {
  name = google_compute_resource_policy.xsec_snapshots.name
  disk = google_compute_instance.xsec.name
  zone = google_compute_instance.xsec.zone
}