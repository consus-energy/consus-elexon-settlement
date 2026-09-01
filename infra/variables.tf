variable "project_id" {
  description = "GCP project ID for this environment"
  type        = string
}

variable "region" {
  description = "GCP region for all regional resources"
  type        = string
  default     = "europe-west2"
}

variable "env" {
  description = "Short environment name, used in resource names. Not the same as the IDD test data flag."
  type        = string
}

variable "name_prefix" {
  description = "Prefix for all resource names"
  type        = string
  default     = "settlement"
}

variable "subnet_cidr" {
  description = "Primary CIDR for the single subnet. Only Cloud Run direct VPC egress uses it."
  type        = string
  default     = "10.20.0.0/24"
}

variable "archive_retention_days" {
  description = <<-EOT
    Retention period in days for the raw file archive, or null for none.
    Leave null during qualification: a retention policy blocks object deletion
    even when unlocked, which makes clearing test junk painful. Set to 1220
    and lock the bucket before go-live.
  EOT
  type        = number
  default     = null
}

variable "archive_retention_locked" {
  description = "Irreversibly lock the retention policy. Cannot be undone. Leave false until go-live."
  type        = bool
  default     = false
}

variable "db_tier" {
  description = "Cloud SQL machine type. db-f1-micro is fine for qualification; size up for live."
  type        = string
  default     = "db-f1-micro"
}

variable "db_availability_type" {
  description = "ZONAL or REGIONAL. REGIONAL for live: an outage during a gate-closure window is unrecoverable."
  type        = string
  default     = "ZONAL"
}

variable "db_deletion_protection" {
  description = "Block deletion of the Cloud SQL instance."
  type        = bool
  default     = true
}

variable "image_tag" {
  description = "Container image tag to deploy. A digest is better than 'latest': a Job that silently picks up a new image is a change nobody assessed."
  type        = string
  default     = "latest"
}

variable "settlement_environment" {
  description = "IDD test data flag. 'OPER' for operational, otherwise a four character test phase value. Defaults to a test value: a missing setting must never mean 'send to live settlement'."
  type        = string
  default     = "TST1"

  validation {
    condition     = length(var.settlement_environment) <= 4
    error_message = "The test data flag is text(4) in the IDD header."
  }
}

variable "vtp_participant_id" {
  description = "Our Party Id, used with role code VT for WMAN and the SVAA flows"
  type        = string
}

variable "ecvna_participant_id" {
  description = "Our ECVNA Id, used with role code EN for ECVNs"
  type        = string
}

variable "log_level" {
  type    = string
  default = "INFO"
}
