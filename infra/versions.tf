terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Bucket is created out-of-band (see README). Prefix keeps this repo's state
  # separate from anything else that ever lands in the same bucket.
  backend "gcs" {
    bucket = "consus-tf-state-elexon-settlement"
    prefix = "settlement/test"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
