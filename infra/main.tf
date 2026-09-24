# ---------------------------------------------------------------------------------------------
# APIs
# ---------------------------------------------------------------------------------------------
# This resource cannot bootstrap itself: enabling an API is a call *to* the Service Usage API,
# and reading which are enabled is a call to Cloud Resource Manager. On a project where those two
# have never been used, the first apply fails with SERVICE_DISABLED on every entry here, having
# already created the service accounts and buckets. They are listed anyway - so that `terraform
# destroy` does not turn them off, and so the dependency is written down rather than learned from
# a failed apply - but they must already be on. See the runbook's step 1.
resource "google_project_service" "enabled" {
  for_each = toset([
    "serviceusage.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "cloudbuild.googleapis.com",
    "run.googleapis.com",
    "sqladmin.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudscheduler.googleapis.com",
    "artifactregistry.googleapis.com",
    "monitoring.googleapis.com",
    "logging.googleapis.com",
  ])
  service            = each.key
  disable_on_destroy = false
}

# ---------------------------------------------------------------------------------------------
# Identities. Two, not one: the web tier must not be able to do what the job can, and the job's
# database access must survive whatever the web tier does to itself (F-6).
# ---------------------------------------------------------------------------------------------
resource "google_service_account" "api" {
  account_id   = "rainalert-api"
  display_name = "RainAlert API service"
}

resource "google_service_account" "ingest" {
  account_id   = "rainalert-ingest"
  display_name = "RainAlert ingest job"
}

resource "google_service_account" "scheduler" {
  account_id   = "rainalert-scheduler"
  display_name = "Invokes the ingest job on a schedule"
}

# ---------------------------------------------------------------------------------------------
# Storage. Two buckets on purpose (F-9): the overlays are served to browsers, the raw DWD archives
# are not. Side by side, one "make the overlays public" step publishes everything next to them.
# ---------------------------------------------------------------------------------------------
resource "google_storage_bucket" "archives" {
  name                        = "${var.project_id}-rainalert-archives"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  lifecycle_rule {
    condition { age = 2 } # days; D-7 says 48 h
    action { type = "Delete" }
  }
}

resource "google_storage_bucket" "overlays" {
  name                        = "${var.project_id}-rainalert-overlays"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true

  # Observed frames feed the 12 h timeline; only the newest cycle's forecast is ever shown (D-7).
  lifecycle_rule {
    condition {
      age                = 1
      matches_prefix     = ["obs/"]
    }
    action { type = "Delete" }
  }
  lifecycle_rule {
    condition {
      age            = 1
      matches_prefix = ["fc/"]
    }
    action { type = "Delete" }
  }

  cors {
    origin          = [var.public_base_url]
    method          = ["GET", "HEAD"]
    response_header = ["Content-Type"]
    max_age_seconds = 3600
  }
}

# Public read on the overlays only. This is radar imagery DWD publishes anyway; it carries nothing
# subscriber-specific.
resource "google_storage_bucket_iam_member" "overlays_public" {
  bucket = google_storage_bucket.overlays.name
  role   = "roles/storage.objectViewer"
  member = "allUsers"
}

resource "google_storage_bucket_iam_member" "ingest_writes_archives" {
  bucket = google_storage_bucket.archives.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.ingest.email}"
}

resource "google_storage_bucket_iam_member" "ingest_writes_overlays" {
  bucket = google_storage_bucket.overlays.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.ingest.email}"
}

# ---------------------------------------------------------------------------------------------
# Database (D-24)
# ---------------------------------------------------------------------------------------------
resource "google_sql_database_instance" "main" {
  name             = "rainalert"
  database_version = "POSTGRES_16"
  region           = var.region

  settings {
    # Pinned, not left to the default: Cloud SQL now creates new instances as ENTERPRISE_PLUS,
    # whose tier list is the db-perf-optimized-N-* machines - none of which is a shared core, and
    # the cheapest of which costs several times this whole stack. The shared-core tiers only
    # exist in ENTERPRISE. Changing this later replaces the instance, so it is set explicitly
    # rather than inherited from whatever the API defaults to next.
    edition           = "ENTERPRISE"
    tier              = "db-f1-micro"
    availability_type = "ZONAL"
    disk_size         = 10
    disk_autoresize   = true

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      start_time                     = "02:00"
    }

    ip_configuration {
      ipv4_enabled = false
      # Cloud Run reaches this over the Cloud SQL connector, not the public internet.
      private_network = null
    }
  }

  # Losing the subscriber table means friends silently stop getting warnings, with no way to know.
  deletion_protection = true
}

resource "google_sql_database" "main" {
  name     = "rainalert"
  instance = google_sql_database_instance.main.name
}

resource "random_password" "api_db" {
  length  = 32
  special = false
}

resource "random_password" "ingest_db" {
  length  = 32
  special = false
}

# Separate database users so the job's connections are not competing with, or exhaustible by, the
# web tier (F-6).
resource "google_sql_user" "api" {
  name     = "rainalert_api"
  instance = google_sql_database_instance.main.name
  password = random_password.api_db.result
}

resource "google_sql_user" "ingest" {
  name     = "rainalert_ingest"
  instance = google_sql_database_instance.main.name
  password = random_password.ingest_db.result
}
