locals {
  # A deployment with push working and no mail provider yet. It decides three things together,
  # so they cannot drift apart: whether the signup page offers email, whether the API accepts it,
  # and whether the SMTP password secret is mounted at all.
  email_enabled = var.smtp_host != ""

  # Settings both workloads share. Anything secret comes from Secret Manager instead.
  common_env = {
    PUBLIC_BASE_URL = var.public_base_url
    MAIL_FROM       = var.mail_from
    # `auto` routes each message by its channel: topics to ntfy, addresses to SMTP. Not one
    # transport for everything - that is how a mailbox ends up published as an ntfy topic
    # (notify/routing.py).
    NOTIFIER                = "auto"
    EMAIL_CHANNEL_ENABLED   = tostring(local.email_enabled)
    SMTP_HOST               = var.smtp_host
    SMTP_PORT               = tostring(var.smtp_port)
    SMTP_USERNAME           = var.smtp_username
    NTFY_SERVER             = var.ntfy_server
    OVERLAY_BUCKET          = google_storage_bucket.overlays.name
    OVERLAY_PUBLIC_BASE_URL = "https://storage.googleapis.com/${google_storage_bucket.overlays.name}"
    LOG_LEVEL               = "INFO"
    # Cloud Run sits in front of us, so exactly one hop is ours. Without this the client picks its
    # own identity out of X-Forwarded-For and every rate limit is decorative (F-5).
    TRUSTED_PROXY_HOPS = "1"
  }
}

resource "google_cloud_run_v2_service" "api" {
  name     = "rainalert-api"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.api.email
    # See var.api_max_instances - this is a security control.
    scaling {
      min_instance_count = 0
      max_instance_count = var.api_max_instances
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance { instances = [google_sql_database_instance.main.connection_name] }
    }

    containers {
      image = var.image

      resources {
        limits = { cpu = "1", memory = "512Mi" }
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }

      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.key
          value = env.value
        }
      }

      env {
        name = "DATABASE_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.this["api-database-url"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "SECRET_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.this["secret-key"].secret_id
            version = "latest"
          }
        }
      }
      # Mounted only when there is a provider. A secret with no version fails the container at
      # startup, so on a push-only deployment this must not be here at all - the secret itself
      # stays, empty, so turning email on later is one `gcloud secrets versions add`.
      dynamic "env" {
        for_each = local.email_enabled ? [1] : []
        content {
          name = "SMTP_PASSWORD"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.this["smtp-password"].secret_id
              version = "latest"
            }
          }
        }
      }
      env {
        name = "METRICS_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.this["metrics-token"].secret_id
            version = "latest"
          }
        }
      }

      startup_probe {
        http_get { path = "/healthz" }
        initial_delay_seconds = 3
        period_seconds        = 5
        failure_threshold     = 6
      }
    }
  }

  depends_on = [google_project_service.enabled]
}

# The web UI is for anyone with the link; the API authenticates per request.
resource "google_cloud_run_v2_service_iam_member" "public" {
  name     = google_cloud_run_v2_service.api.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_job" "ingest" {
  name     = "rainalert-ingest"
  location = var.region

  template {
    # Exactly one task at a time. Combined with the advisory lock and the unique nominal_time,
    # a retried execution cannot double-process a cycle.
    parallelism = 1
    task_count  = 1

    template {
      service_account = google_service_account.ingest.email
      max_retries     = 1
      timeout         = "240s"

      volumes {
        name = "cloudsql"
        cloud_sql_instance { instances = [google_sql_database_instance.main.connection_name] }
      }

      containers {
        image   = var.image
        command = ["python", "-m", "rainalert.cli"]
        args    = ["ingest", "--prune"]

        # Measured: a complete 25-frame cycle is 165 MB as float32 plus masks, before the
        # renderer's buffers. Sized from that, not from the raw uint16 figure.
        resources {
          limits = { cpu = "1", memory = "2Gi" }
        }

        volume_mounts {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }

        dynamic "env" {
          for_each = merge(local.common_env, {
            ARCHIVE_DIR     = ""
            GCS_BUCKET      = google_storage_bucket.archives.name
            DWD_USER_AGENT  = "RainAlert/0.1 (+${var.public_base_url}; contact: ${var.alert_email})"
          })
          content {
            name  = env.key
            value = env.value
          }
        }

        env {
          name = "DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.this["ingest-database-url"].secret_id
              version = "latest"
            }
          }
        }
        env {
          name = "SECRET_KEY"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.this["secret-key"].secret_id
              version = "latest"
            }
          }
        }
        dynamic "env" {
          for_each = local.email_enabled ? [1] : []
          content {
            name = "SMTP_PASSWORD"
            value_source {
              secret_key_ref {
                secret  = google_secret_manager_secret.this["smtp-password"].secret_id
                version = "latest"
              }
            }
          }
        }
      }
    }
  }

  depends_on = [google_project_service.enabled]
}

# Migrations run as their own job, invoked by hand. Running them on container start means a
# rollback can find a schema from the future - the deploy that "worked" is the one that broke you.
resource "google_cloud_run_v2_job" "migrate" {
  name     = "rainalert-migrate"
  location = var.region

  template {
    template {
      service_account = google_service_account.ingest.email
      max_retries     = 0
      timeout         = "300s"

      volumes {
        name = "cloudsql"
        cloud_sql_instance { instances = [google_sql_database_instance.main.connection_name] }
      }

      containers {
        image   = var.image
        command = ["alembic"]
        args    = ["upgrade", "head"]

        volume_mounts {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }

        env {
          name = "DATABASE_URL"
          value_source {
            secret_key_ref {
              # Migrations need DDL, so they use the ingest role rather than the web tier's.
              secret  = google_secret_manager_secret.this["ingest-database-url"].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  depends_on = [google_project_service.enabled]
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_runs_it" {
  name     = google_cloud_run_v2_job.ingest.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "tick" {
  name        = "rainalert-tick"
  region      = var.region
  description = "Fires the ingest job. Minute 4 is measured, not guessed: RV publishes 3-5 minutes after nominal time, so firing at 3 lands before publication and burns a retry every cycle (DESIGN.md 4.4)."
  schedule    = "4-59/5 * * * *"
  time_zone   = "UTC"

  retry_config {
    retry_count = 0 # the next cycle is five minutes away; retrying here only risks hammering DWD
  }

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/${google_cloud_run_v2_job.ingest.name}:run"
    oauth_token { service_account_email = google_service_account.scheduler.email }
  }

  depends_on = [google_project_service.enabled]
}
