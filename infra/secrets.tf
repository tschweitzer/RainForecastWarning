# Secrets. Terraform state holds the generated passwords, which is why versions.tf pushes you
# towards a GCS backend rather than a file on a laptop.

locals {
  # The unix-socket DSN Cloud Run uses with the Cloud SQL connector.
  socket = "/cloudsql/${google_sql_database_instance.main.connection_name}"

  api_database_url = "postgresql+psycopg://${google_sql_user.api.name}:${random_password.api_db.result}@/${google_sql_database.main.name}?host=${local.socket}"

  ingest_database_url = "postgresql+psycopg://${google_sql_user.ingest.name}:${random_password.ingest_db.result}@/${google_sql_database.main.name}?host=${local.socket}"
}

resource "random_password" "secret_key" {
  length  = 48
  special = false
}

resource "random_password" "metrics_token" {
  length  = 32
  special = false
}

resource "google_secret_manager_secret" "this" {
  for_each  = toset(["api-database-url", "ingest-database-url", "secret-key", "smtp-password", "metrics-token"])
  secret_id = "rainalert-${each.key}"
  replication {
    user_managed {
      replicas { location = var.region }
    }
  }
  depends_on = [google_project_service.enabled]
}

resource "google_secret_manager_secret_version" "api_database_url" {
  secret      = google_secret_manager_secret.this["api-database-url"].id
  secret_data = local.api_database_url
}

resource "google_secret_manager_secret_version" "ingest_database_url" {
  secret      = google_secret_manager_secret.this["ingest-database-url"].id
  secret_data = local.ingest_database_url
}

resource "google_secret_manager_secret_version" "secret_key" {
  secret = google_secret_manager_secret.this["secret-key"].id
  # Rotating this invalidates every unsubscribe link, since they are signed rather than stored.
  secret_data = random_password.secret_key.result
}

resource "google_secret_manager_secret_version" "metrics_token" {
  secret      = google_secret_manager_secret.this["metrics-token"].id
  secret_data = random_password.metrics_token.result
}

# The SMTP password is the one secret Terraform must not generate. Set it by hand:
#   echo -n 'the-password' | gcloud secrets versions add rainalert-smtp-password --data-file=-

resource "google_secret_manager_secret_iam_member" "api_reads" {
  for_each  = toset(["api-database-url", "secret-key", "smtp-password"])
  secret_id = google_secret_manager_secret.this[each.key].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}

resource "google_secret_manager_secret_iam_member" "api_reads_metrics_token" {
  secret_id = google_secret_manager_secret.this["metrics-token"].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}

resource "google_secret_manager_secret_iam_member" "ingest_reads" {
  for_each  = toset(["ingest-database-url", "secret-key", "smtp-password"])
  secret_id = google_secret_manager_secret.this[each.key].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.ingest.email}"
}

resource "google_project_iam_member" "sql_client" {
  for_each = toset([google_service_account.api.email, google_service_account.ingest.email])
  project  = var.project_id
  role     = "roles/cloudsql.client"
  member   = "serviceAccount:${each.key}"
}
