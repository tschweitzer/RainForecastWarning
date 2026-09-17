# Monitoring. The thing that must never happen quietly is "nobody gets warned" - so the alerts are
# about the pipeline still running, not about CPU.

resource "google_monitoring_notification_channel" "operator" {
  display_name = "RainAlert operator"
  type         = "email"
  labels       = { email_address = var.alert_email }
}

# The log lines the application emits when it has stopped talking to DWD, or when a cycle was
# refused. Both mean warnings are not going out.
resource "google_logging_metric" "ingestion_halted" {
  name   = "rainalert_ingestion_halted"
  filter = <<-EOT
    resource.type="cloud_run_job"
    resource.labels.job_name="${google_cloud_run_v2_job.ingest.name}"
    (textPayload:"ingestion halted" OR jsonPayload.msg:"ingestion halted"
     OR textPayload:"circuit breaker opened" OR jsonPayload.msg:"circuit breaker opened"
     OR textPayload:"blast radius" OR jsonPayload.msg:"blast radius")
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_monitoring_alert_policy" "ingestion_halted" {
  display_name = "RainAlert: ingestion halted or blast radius tripped"
  combiner     = "OR"

  conditions {
    display_name = "halt logged"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.ingestion_halted.name}\" AND resource.type=\"cloud_run_job\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_DELTA"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.operator.id]
  alert_strategy { auto_close = "3600s" }
}

# A job that stops succeeding is the same outcome as a job that logs a failure, and more likely.
resource "google_monitoring_alert_policy" "job_not_completing" {
  display_name = "RainAlert: ingest job has not completed recently"
  combiner     = "OR"

  conditions {
    display_name = "no completed executions in 30 minutes"
    condition_threshold {
      filter = join(" AND ", [
        "metric.type=\"run.googleapis.com/job/completed_task_attempt_count\"",
        "resource.type=\"cloud_run_job\"",
        "resource.label.job_name=\"${google_cloud_run_v2_job.ingest.name}\"",
      ])
      comparison      = "COMPARISON_LT"
      threshold_value = 1
      duration        = "1800s"
      aggregations {
        alignment_period   = "600s"
        per_series_aligner = "ALIGN_DELTA"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.operator.id]
}

# NOT DEFINED HERE, on purpose: the cycle-age SLI. It lives in the database, which Cloud Monitoring
# cannot see. Getting it onto a dashboard needs something to scrape /metrics (Managed Service for
# Prometheus, or a tiny scheduled job that reads it and writes a custom metric). The two alerts
# above cover the same failure from the outside - a halted or failing job stops producing cycles -
# so this is a gap in observability, not in safety. See RUNBOOK.md.
