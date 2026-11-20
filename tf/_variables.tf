variable "env" {
    description = "Environment"
    type        = string
}

variable "website_bucket" {
    description = "S3 bucket the results.json file gets written to"
    type        = string
    default     = "patrick-cloud.com"
}

variable "results_key" {
    description = "S3 key (within website_bucket) the Lambda writes results to"
    type        = string
    default     = "stock-screener/results.json"
}

variable "cloudfront_distribution_id" {
    description = "CloudFront distribution to invalidate after each run"
    type        = string
}

variable "schedule_expression" {
    description = "EventBridge schedule for the daily run (UTC)"
    type        = string
    # 4:01pm ET Mon-Fri, right after the 4:00pm market close. Classic EventBridge
    # Rules schedule_expression is UTC-only (no DST awareness), so this is pinned
    # to EDT (UTC-4); during EST (Nov-Mar) it'll actually fire at 3:01pm ET. Move
    # to aws_scheduler_schedule with schedule_expression_timezone = "America/New_York"
    # if true DST-correctness is needed.
    default     = "cron(1 20 ? * MON-FRI *)"
}

variable "gex_key" {
    description = "S3 key (within website_bucket) the GEX Lambda writes its snapshot to"
    type        = string
    default     = "stock-screener/gex.json"
}

variable "gex_schedule_expression" {
    description = "EventBridge schedule for the GEX snapshot (UTC)"
    type        = string
    # Every 15 min, 13:00-21:45 UTC on weekdays: covers 9:30-16:15 ET in both EST
    # and EDT. The handler skips runs outside market hours itself.
    default     = "cron(0/15 13-21 ? * MON-FRI *)"
}

variable "ses_domain" {
  description = "Domain verified in SES as the alert email sender (hosted in Route 53)"
  type        = string
  default     = "patrick-cloud.com"
}

variable "cycle_key" {
    description = "S3 key (within website_bucket) the market cycle Lambda writes to"
    type        = string
    default     = "stock-screener/cycle.json"
}

variable "cycle_schedule_expression" {
    description = "EventBridge schedule for the market cycle readout (UTC)"
    type        = string
    # 22:30 UTC = 5:30pm EST / 6:30pm EDT, after Yahoo has the day's closes.
    default     = "cron(30 22 ? * MON-FRI *)"
}
