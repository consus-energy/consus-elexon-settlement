# Alerting.
#
# The gateway already knows when something is wrong: sweep exits non-zero when
# a submission is at or past Gate Closure, and collect exits non-zero when a
# file could not be handled. What was missing is anything that tells a human.
#
# Three things are watched, and the third is the one that matters most:
#
#   1. a Job failed             -- something went wrong and said so
#   2. a submission is critical -- gate closure is close and nothing has been
#                                  acknowledged
#   3. a Job stopped running    -- nothing went wrong because nothing happened
#
# The third is the failure mode that looks like success. A scheduler that
# stops firing, a Job that cannot start, a deploy that broke the image -- all
# produce silence, and silence from a sweep is indistinguishable from a sweep
# that found nothing. The absence policy is the only one that catches it.

resource "google_project_service" "monitoring" {
  project            = var.project_id
  service            = "monitoring.googleapis.com"
  disable_on_destroy = false
}

resource "google_monitoring_notification_channel" "email" {
  for_each = toset(var.alert_emails)

  display_name = "Settlement alerts: ${each.key}"
  type         = "email"

  labels = {
    email_address = each.key
  }

  depends_on = [google_project_service.monitoring]
}

locals {
  channels = [for c in google_monitoring_notification_channel.email : c.id]
}

# --- 1. a Job failed --------------------------------------------------------

resource "google_monitoring_alert_policy" "job_failed" {
  display_name = "${local.prefix}: job failed"
  combiner     = "OR"

  documentation {
    content   = <<-EOT
      A settlement Job exited non-zero.

      collect fails when a file could not be parsed, handled or acknowledged.
      The file is archived either way, so nothing is lost, but a rejection we
      have not read is a rejection we have not acted on.

      sweep fails when a submission is at or past Gate Closure. That is the
      urgent case: see the manual fallback runbook.

      Logs:
        gcloud logging read 'resource.type="cloud_run_job"' --limit 50
    EOT
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "job completed with failure"

    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"cloud_run_job\"",
        "metric.type = \"run.googleapis.com/job/completed_task_attempt_count\"",
        "metric.label.result = \"failed\"",
      ])

      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  notification_channels = local.channels

  alert_strategy {
    # Close after an hour of no further failures. collect runs every five
    # minutes, so an hour of success is a genuine recovery rather than a gap
    # between attempts.
    auto_close = "3600s"
  }

  depends_on = [google_project_service.monitoring]
}

# --- 2. a submission is critical -------------------------------------------

resource "google_logging_metric" "gate_closure_critical" {
  name = "${local.prefix}-gate-closure-critical"
  filter = join(" AND ", [
    "resource.type = \"cloud_run_job\"",
    "severity = \"ERROR\"",
    "(textPayload:\"[CRITICAL]\" OR textPayload:\"[MISSED]\")",
  ])

  description = <<-EOT
    Submissions within fifteen minutes of Gate Closure, or past it, with
    nothing acknowledged. Fifteen minutes is roughly how long a manual
    submission through the central system web interface takes, so escalating
    later would be escalating too late to act on.
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }

  depends_on = [google_project_service.monitoring]
}

resource "google_monitoring_alert_policy" "gate_closure" {
  display_name = "${local.prefix}: submission at gate closure"
  combiner     = "OR"

  documentation {
    content   = <<-EOT
      A submission is within fifteen minutes of Gate Closure, or past it, and
      has not been acknowledged.

      This is the one that needs a person. Gate Closure cannot be extended and
      an unhedged position is cashed out at the imbalance price.

      Act on it: submit manually through the central system web interface. The
      procedure is in the manual fallback runbook, and it is deliberately
      executable by either director without the gateway.

      Which submission:
        gcloud logging read 'resource.type="cloud_run_job" AND severity=ERROR' \
          --limit 20 --format="value(textPayload)"
    EOT
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "critical or missed submission logged"

    condition_threshold {
      filter = "metric.type = \"logging.googleapis.com/user/${google_logging_metric.gate_closure_critical.name}\" AND resource.type = \"cloud_run_job\""

      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  notification_channels = local.channels

  # No auto_close. A missed Gate Closure does not resolve itself, and an alert
  # that closes on its own invites the assumption that it was handled.
  depends_on = [google_project_service.monitoring]
}

# --- 3. a Job stopped running ----------------------------------------------

resource "google_monitoring_alert_policy" "collect_not_running" {
  display_name = "${local.prefix}: collect has stopped running"
  combiner     = "OR"
  enabled      = var.absence_alerts_enabled

  documentation {
    content   = <<-EOT
      collect has not completed for thirty minutes. It is scheduled every five.

      Nothing has failed -- that is the point. A Job that cannot start, a
      scheduler that stopped firing, an image that will not pull: all produce
      silence, and silence from collect looks exactly like an empty inbox.

      Meanwhile rejections from ECVAA are arriving and not being read.

      Check the scheduler and the Job:
        gcloud scheduler jobs describe ${local.prefix}-collect --location ${var.region}
        gcloud run jobs executions list --job ${local.prefix}-collect --region ${var.region} --limit 5
    EOT
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "no completed execution in thirty minutes"

    condition_absent {
      filter = join(" AND ", [
        "resource.type = \"cloud_run_job\"",
        "resource.label.job_name = \"${local.prefix}-collect\"",
        "metric.type = \"run.googleapis.com/job/completed_task_attempt_count\"",
      ])

      # Six missed runs. Long enough not to fire on a slow execution, short
      # enough to leave time before the next Gate Closure.
      duration = "1800s"

      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  notification_channels = local.channels

  alert_strategy {
    auto_close = "3600s"
  }

  depends_on = [google_project_service.monitoring]
}

resource "google_monitoring_alert_policy" "sweep_not_running" {
  display_name = "${local.prefix}: sweep has stopped running"
  combiner     = "OR"
  enabled      = var.absence_alerts_enabled

  documentation {
    content   = <<-EOT
      sweep has not completed for ninety minutes. It is scheduled every
      fifteen.

      sweep is the control that notices unacknowledged submissions. A sweep
      that is not running is not a quiet system, it is an unmonitored one --
      and the thing it monitors is whether we have missed a deadline.

        gcloud run jobs executions list --job ${local.prefix}-sweep --region ${var.region} --limit 5
    EOT
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "no completed execution in ninety minutes"

    condition_absent {
      filter = join(" AND ", [
        "resource.type = \"cloud_run_job\"",
        "resource.label.job_name = \"${local.prefix}-sweep\"",
        "metric.type = \"run.googleapis.com/job/completed_task_attempt_count\"",
      ])

      duration = "5400s"

      aggregations {
        alignment_period   = "900s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  notification_channels = local.channels

  alert_strategy {
    auto_close = "7200s"
  }

  depends_on = [google_project_service.monitoring]
}

# --- unencrypted sending ----------------------------------------------------
#
# Not an operational alert so much as an assurance one. cli logs a warning when
# no keyring is configured, which is correct in development and wrong anywhere
# else. If it ever appears in a deployed environment, someone needs to know
# immediately rather than at the next audit.

resource "google_logging_metric" "unencrypted" {
  name = "${local.prefix}-unencrypted"
  filter = join(" AND ", [
    "resource.type = \"cloud_run_job\"",
    "textPayload:\"UNENCRYPTED\"",
  ])

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }

  depends_on = [google_project_service.monitoring]
}

resource "google_monitoring_alert_policy" "unencrypted" {
  display_name = "${local.prefix}: sending unencrypted"
  combiner     = "OR"

  documentation {
    content   = <<-EOT
      The gateway started without a keyring and would send files unencrypted.

      Every file exchanged with central systems must be signed and encrypted.
      This means CONSUS_GNUPGHOME is unset, or the secret mount failed.

      Nothing may be sent until it is fixed.
    EOT
    mime_type = "text/markdown"
  }

  conditions {
    display_name = "unencrypted warning logged"

    condition_threshold {
      filter = "metric.type = \"logging.googleapis.com/user/${google_logging_metric.unencrypted.name}\" AND resource.type = \"cloud_run_job\""

      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  notification_channels = local.channels

  depends_on = [google_project_service.monitoring]
}