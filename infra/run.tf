locals {
  # A deployment with push working and no mail provider yet. It decides three things together,
  # so they cannot drift apart: whether the signup page offers email, whether the API accepts it,
  # and whether the SMTP password secret is mounted at all.
  email_enabled = var.smtp_host != ""

  # Settings both workloads share. Anything secret comes from Secret Manager instead.
  common_env = {
    PUBLIC_BASE_URL = var.public_base_url
    MAIL_FROM       = var.mail_from
    # `auto` routes each message by its channel: push endpoints to the web push transport,
    # addresses to SMTP. Not one transport for everything - that is how a mailbox ended up
    # published as a public ntfy topic, which is the failure notify/routing.py exists for.
    NOTIFIER                = "auto"
    EMAIL_CHANNEL_ENABLED   = tostring(local.email_enabled)
    SMTP_HOST               = var.smtp_host
    SMTP_PORT               = tostring(var.smtp_port)
    SMTP_USERNAME           = var.smtp_username
    # Falls back to alert_email so a deployment cannot end up signing with the placeholder in
    # config.py, which some push services accept and others refuse.
    VAPID_SUBJECT           = var.vapid_subject != "" ? var.vapid_subject : "mailto:${var.alert_email}"
    MAP_TILE_URL            = var.map_tile_url
    MAP_TILE_ATTRIBUTION    = var.map_tile_attribution
    VECTOR_TILE_URL         = var.vector_tile_url
    CONTACT_EMAIL           = var.contact_email

    # Confirmation and settings-link mails per rolling day, across all requests (D-63).
    TRANSACTIONAL_MAIL_CAP_PER_DAY = tostring(var.transactional_mail_cap_per_day)

    # Device keys for the settings page (D-64). The kill switch: false falls back to the push link
    # and the cookie session for everyone, losing nothing - see RUNBOOK before turning it back on.
    DEVICE_KEY_LOGIN_ENABLED = tostring(var.device_key_login_enabled)

    OVERLAY_BUCKET          = google_storage_bucket.overlays.name
    OVERLAY_PUBLIC_BASE_URL = "https://storage.googleapis.com/${google_storage_bucket.overlays.name}"
    LOG_LEVEL               = "INFO"
    # How many proxies in front of us are ours. Without this the client picks its own identity out
    # of X-Forwarded-For and every rate limit is decorative (F-5).
    #
    # A variable rather than the literal "1" it was, because the right answer changes with the
    # deployment and the change has to land *with* the cutover, not before it: Cloud Run alone is
    # one hop, Firebase Hosting or a load balancer in front makes it two. Setting 2 while nothing
    # is in front is the same outage as setting 1 once something is - see variables.tf.
    TRUSTED_PROXY_HOPS = tostring(var.trusted_proxy_hops)
  }
}

resource "google_cloud_run_v2_service" "api" {
  name     = "rainalert-api"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  # The provider defaults this to true, which blocks Terraform from ever replacing this. That
  # guard is for resources holding something a re-create would lose; a Cloud Run service or job
  # holds nothing - the image is in Artifact Registry, the configuration is this file, and the
  # data is in Cloud SQL and GCS, which keep their own protection. What it does here is strand a
  # failed deploy: a resource whose creation failed is tainted, the next apply must destroy it to
  # try again, and deletion_protection refuses.
  deletion_protection = false

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
        # With min instances at 0, the first visitor after ~15 quiet minutes waits for a cold
        # start, and most of that is Python importing the app on one vCPU. The boost doubles the
        # CPU for the startup and ~10 s after, and only then. It is billed at the normal CPU rate
        # for those seconds - not free, but a few dozen cold starts a day stays inside the free
        # tier (D-51).
        startup_cpu_boost = true
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
      # The web push signing key. Mounted on the API service because that is what serves the
      # `applicationServerKey` the browser subscribes with, and on the ingest job because that is
      # what sends the warnings. Both derive the public half from it, so they cannot disagree.
      env {
        name = "VAPID_PRIVATE_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.this["vapid-private-key"].secret_id
            version = "latest"
          }
        }
      }

      # No request reaches a new instance until this passes, so its spacing is part of every cold
      # start. At 3 s + every 5 s, an app ready at 3.5 s waited until 8 s. Probing every second
      # makes that wait at most one second. /healthz touches nothing, so probing it often costs
      # nothing, and probes are not billed. A probe may not take longer than its period, which
      # caps each one at 1 s; /healthz answers in milliseconds, and there are 30 tries. The
      # failures before the app is up are expected and are not alerted on.
      #
      # The budget stays about where it was (30 s now, 33 s before): long enough that a slow
      # start still comes up, short enough that a broken image fails its deploy (D-51).
      startup_probe {
        http_get { path = "/healthz" }
        initial_delay_seconds = 0
        period_seconds        = 1
        timeout_seconds       = 1
        failure_threshold     = 30
      }
    }
  }

  # The *versions*, not just the secrets. A container mounts `versions/latest`, which does not
  # exist until the version resource is created - and nothing in the configuration links the two,
  # because the env block references the secret's id. Terraform therefore created these in
  # parallel with the versions and Cloud Run refused them: "Secret .../versions/latest was not
  # found". An explicit edge is the fix; there is no attribute to reference instead.
  depends_on = [
    google_project_service.enabled,
    google_secret_manager_secret_version.api_database_url,
    google_secret_manager_secret_version.ingest_database_url,
    google_secret_manager_secret_version.secret_key,
    google_secret_manager_secret_version.metrics_token,
    google_secret_manager_secret_version.vapid_private_key,
  ]
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

  # The provider defaults this to true, which blocks Terraform from ever replacing this. That
  # guard is for resources holding something a re-create would lose; a Cloud Run service or job
  # holds nothing - the image is in Artifact Registry, the configuration is this file, and the
  # data is in Cloud SQL and GCS, which keep their own protection. What it does here is strand a
  # failed deploy: a resource whose creation failed is tainted, the next apply must destroy it to
  # try again, and deletion_protection refuses.
  deletion_protection = false

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
        env {
          name = "VAPID_PRIVATE_KEY"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.this["vapid-private-key"].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  # The *versions*, not just the secrets. A container mounts `versions/latest`, which does not
  # exist until the version resource is created - and nothing in the configuration links the two,
  # because the env block references the secret's id. Terraform therefore created these in
  # parallel with the versions and Cloud Run refused them: "Secret .../versions/latest was not
  # found". An explicit edge is the fix; there is no attribute to reference instead.
  depends_on = [
    google_project_service.enabled,
    google_secret_manager_secret_version.api_database_url,
    google_secret_manager_secret_version.ingest_database_url,
    google_secret_manager_secret_version.secret_key,
    google_secret_manager_secret_version.metrics_token,
    google_secret_manager_secret_version.vapid_private_key,
  ]
}

# Migrations run as their own job, invoked by hand. Running them on container start means a
# rollback can find a schema from the future - the deploy that "worked" is the one that broke you.
resource "google_cloud_run_v2_job" "migrate" {
  name     = "rainalert-migrate"
  location = var.region

  # The provider defaults this to true, which blocks Terraform from ever replacing this. That
  # guard is for resources holding something a re-create would lose; a Cloud Run service or job
  # holds nothing - the image is in Artifact Registry, the configuration is this file, and the
  # data is in Cloud SQL and GCS, which keep their own protection. What it does here is strand a
  # failed deploy: a resource whose creation failed is tainted, the next apply must destroy it to
  # try again, and deletion_protection refuses.
  deletion_protection = false

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

  # The *versions*, not just the secrets. A container mounts `versions/latest`, which does not
  # exist until the version resource is created - and nothing in the configuration links the two,
  # because the env block references the secret's id. Terraform therefore created these in
  # parallel with the versions and Cloud Run refused them: "Secret .../versions/latest was not
  # found". An explicit edge is the fix; there is no attribute to reference instead.
  depends_on = [
    google_project_service.enabled,
    google_secret_manager_secret_version.api_database_url,
    google_secret_manager_secret_version.ingest_database_url,
    google_secret_manager_secret_version.secret_key,
    google_secret_manager_secret_version.metrics_token,
  ]
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

# The liveness notification (D-46), weekly. Reuses the ingest service account and its database
# role: it reads subscribers, sends notifications and deletes rows, which is exactly what the
# alerting path already does - a third identity would be a third thing to keep in step for no
# additional isolation.
resource "google_cloud_run_v2_job" "liveness" {
  name                = "rainalert-liveness"
  location            = var.region
  deletion_protection = false

  template {
    parallelism = 1
    task_count  = 1

    template {
      service_account = google_service_account.ingest.email
      # Not retried. A failed run costs at most a month's delay on a housekeeping task, and a retry
      # that partially succeeded would send a second notification to everyone it already reached.
      max_retries = 0
      timeout     = "600s"

      volumes {
        name = "cloudsql"
        cloud_sql_instance { instances = [google_sql_database_instance.main.connection_name] }
      }

      containers {
        image   = var.image
        command = ["python", "-m", "rainalert.cli"]
        args    = ["liveness"]

        # No radar decoding here, so none of the ingest job's headroom is needed.
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
        env {
          name = "VAPID_PRIVATE_KEY"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.this["vapid-private-key"].secret_id
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

  depends_on = [
    google_project_service.enabled,
    google_secret_manager_secret_version.ingest_database_url,
    google_secret_manager_secret_version.secret_key,
    google_secret_manager_secret_version.vapid_private_key,
  ]
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_runs_liveness" {
  name     = google_cloud_run_v2_job.liveness.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "liveness" {
  name        = "rainalert-liveness"
  region      = var.region
  description = "Weekly. Sends one notification to push subscribers who have heard nothing for WEBPUSH_LIVENESS_DAYS, and deletes the ones whose subscription the push service reports as gone - which is the only way we ever learn that somebody cleared their browser data (DESIGN.md D-46)."
  # Weekly, not monthly, even though the threshold is 30 days. A monthly run does not bound silence
  # at 30 days: somebody who goes quiet the day after a run is not yet 30 days silent when the next
  # one fires, so the first run that can see them is the one after that - about 60 days, while
  # privacy.html tells them 30. Running weekly makes the worst case ~37 days, and costs nothing: the
  # threshold does the selecting, so three runs in four find nobody due.
  #
  # 07:19 on Wednesdays, Berlin time. Not midnight and not on the hour - most schedules run at :00,
  # so a run placed there waits behind them.
  schedule  = "19 7 * * 3"
  time_zone = "Europe/Berlin"

  retry_config {
    retry_count = 0 # see max_retries on the job: a retry re-notifies whoever already got one
  }

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/${google_cloud_run_v2_job.liveness.name}:run"
    oauth_token { service_account_email = google_service_account.scheduler.email }
  }

  depends_on = [google_project_service.enabled]
}
