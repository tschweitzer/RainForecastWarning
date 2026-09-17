terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
  # Terraform state contains generated database passwords. Keep it in a bucket with versioning,
  # not on a laptop. Create the bucket by hand once, then uncomment.
  # backend "gcs" {
  #   bucket = "rainchecker-tfstate"
  #   prefix = "rainalert"
  # }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
