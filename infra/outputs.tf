output "api_url" {
  description = "Set PUBLIC_BASE_URL to this and re-apply, until a real domain exists."
  value       = google_cloud_run_v2_service.api.uri
}

output "database_connection_name" {
  value = google_sql_database_instance.main.connection_name
}

output "overlay_bucket" {
  value = google_storage_bucket.overlays.name
}

output "archive_bucket" {
  value = google_storage_bucket.archives.name
}

output "metrics_token_secret" {
  description = "Read it with: gcloud secrets versions access latest --secret=rainalert-metrics-token"
  value       = google_secret_manager_secret.this["metrics-token"].secret_id
}

output "ingest_service_account" {
  value = google_service_account.ingest.email
}
