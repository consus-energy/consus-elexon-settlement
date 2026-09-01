output "egress_ip" {
  description = "Static egress address to give Elexon for whitelisting."
  value       = google_compute_address.egress.address
}

output "archive_bucket" {
  value = google_storage_bucket.archive.name
}

output "service_account" {
  value = google_service_account.gateway.email
}

output "vpc_subnet" {
  description = "Attach Cloud Run direct VPC egress to this."
  value       = google_compute_subnetwork.subnet.id
}

output "db_instance" {
  value = google_sql_database_instance.main.name
}

output "db_private_ip" {
  value = google_sql_database_instance.main.private_ip_address
}

output "db_dsn_secret" {
  description = "Secret Manager secret holding the psycopg DSN."
  value       = google_secret_manager_secret.db_dsn.secret_id
}
