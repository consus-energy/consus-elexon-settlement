# FTP credentials.
#
# PCIG 5.1: an FTP account is issued per registered Participant ID, and the
# Participant ID is the username. We hold two identities, so there are two
# accounts and two passwords. The username is not stored here -- it is the
# Participant ID, which the container already has.
#
# As with the gpg keys, Terraform creates the containers and the access grants
# and never sees the values. A password in a Terraform state file is a
# password in the state bucket, in every backup of it, and in the plan output
# of anyone who runs terraform plan.
#
# Load them out of band once Elexon issue them:
#
#   printf '%s' 'THEPASSWORD' | gcloud secrets versions add \
#     settlement-test-ftp-password-vtp --data-file=- \
#     --project=consus-elexon-settlement
#
# printf rather than echo: a trailing newline in an FTP password is a login
# failure that looks like a wrong password.

locals {
  # Keyed by role, valued by the Participant ID whose account it is. The
  # environment variable the container reads is derived from the Participant
  # ID, because that is what the transport looks up when the sender asks for
  # a channel's account.
  ftp_accounts = {
    vtp   = var.vtp_participant_id
    ecvna = var.ecvna_participant_id
  }
}

resource "google_secret_manager_secret" "ftp_password" {
  for_each = local.ftp_accounts

  secret_id = "${local.prefix}-ftp-password-${each.key}"

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "gateway_ftp" {
  for_each = google_secret_manager_secret.ftp_password

  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.gateway.email}"
}

output "ftp_password_secrets" {
  description = "Secret Manager ids to load the FTP passwords into, by identity."
  value       = { for k, v in google_secret_manager_secret.ftp_password : k => v.secret_id }
}
