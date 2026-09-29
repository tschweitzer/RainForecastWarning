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

# A log-based metric does not exist for Cloud Monitoring the moment the Logging API returns. The
# metric descriptor is published asynchronously, and an alert policy that names a descriptor which
# has not appeared yet is refused outright:
#
#   Error 404: Cannot find metric(s) that match type =
#   "logging.googleapis.com/user/rainalert_unknown_push_service". If a metric was created
#   recently, it could take up to 10 minutes to become available.
#
# Terraform's own graph cannot see this: the metric resource is complete, so the policy starts
# immediately and fails. The apply is then half-done - metric created, policy missing - and the
# only recovery is to notice the error and re-run.
#
# `time_sleep` makes the wait part of the graph. It only sleeps when it is created, which happens
# when a metric it depends on is created, so this costs five minutes once on a new project and
# nothing on every apply after. Five, not ten: the observed delay is well under a minute, and the
# re-run is still there if a project is unlucky.
resource "time_sleep" "metric_descriptors" {
  depends_on = [
    google_logging_metric.ingestion_halted,
    google_logging_metric.unknown_push_service,
  ]
  create_duration = "300s"

  # Without this the sleep is created once and never again - adding a third metric later would
  # reintroduce the race for that metric. Keying it on the set of metric names means a new metric
  # replaces the sleep, and the wait happens again.
  triggers = {
    metrics = join(",", [
      google_logging_metric.ingestion_halted.name,
      google_logging_metric.unknown_push_service.name,
    ])
  }
}

resource "google_monitoring_alert_policy" "ingestion_halted" {
  display_name = "RainAlert: ingestion halted or blast radius tripped"
  combiner     = "OR"
  depends_on   = [time_sleep.metric_descriptors]

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

# A browser this service does not recognise, which is a silently broken signup for everyone using it.
#
# Deliberately narrow: only the "is not a known push service" refusal, not every `EndpointRefused`.
# The others - not https, userinfo, wrong port, not printable ASCII - are malformed input from bots
# and scanners, they arrive constantly, and alerting on them would be noise that also lets anyone
# ring this bell on demand. An unknown *host* is the one that means a real browser, with a real
# person behind it, cannot sign up.
#
# This exists because of what it would have caught. Chrome hands out `jmt<n>.google.com` and the
# allowlist did not have it: every Chrome subscriber was refused, the browser half of their signup
# succeeded so their own browser listed the site as subscribed, and the page told them to check
# input that was already correct. Nothing was logged, nothing was measured, and it surfaced a week
# later as "works in Firefox, Edge and Opera but not Chrome". That is the hardest shape of bug
# report to act on, and this alert turns it into an email within five minutes.
resource "google_logging_metric" "unknown_push_service" {
  name   = "rainalert_unknown_push_service"
  filter = <<-EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name="${google_cloud_run_v2_service.api.name}"
    (textPayload:"is not a known push service" OR jsonPayload.msg:"is not a known push service")
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

# No label extractor for the host, deliberately. A label would put the offending hostname straight
# into the alert, which is what an operator wants - but the hostname comes from an unauthenticated
# request body, so anyone could mint unbounded distinct label values and turn a metric into a bill.
# The alert says that it happened; RUNBOOK.md section 3 has the one-line grep that says which host.
resource "google_monitoring_alert_policy" "unknown_push_service" {
  display_name = "RainAlert: a browser used a push service we do not allow"
  combiner     = "OR"
  depends_on   = [time_sleep.metric_descriptors]

  conditions {
    display_name = "unknown push host refused"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.unknown_push_service.name}\" AND resource.type=\"cloud_run_revision\""
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
  # Closes itself once a deploy has added the host and the refusals stop. Long enough that a slow
  # afternoon of one subscriber per hour does not re-page repeatedly.
  alert_strategy { auto_close = "3600s" }

  documentation {
    content = <<-EOT
      A browser produced a push endpoint on a host that `ALLOWED_PUSH_HOSTS` in
      `rainalert/notify/webpush.py` does not list, so that subscriber could not sign up.

      Find the host:

          gcloud run services logs read rainalert-api --region europe-west3 --limit 200 \
            | grep "subscribe refused"

      Then add the host, or its family if the name carries a shard number, and deploy. Add the
      specific host - never a bare domain suffix. See RUNBOOK.md section 3.
    EOT
  }
}

# NOT DEFINED HERE, on purpose: the cycle-age SLI. It lives in the database, which Cloud Monitoring
# cannot see. Getting it onto a dashboard needs something to scrape /metrics (Managed Service for
# Prometheus, or a tiny scheduled job that reads it and writes a custom metric). The two alerts
# above cover the same failure from the outside - a halted or failing job stops producing cycles -
# so this is a gap in observability, not in safety. See RUNBOOK.md.
