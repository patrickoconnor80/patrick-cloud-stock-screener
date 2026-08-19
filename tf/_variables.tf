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
