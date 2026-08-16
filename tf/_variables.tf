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
    default     = "cron(0 1 ? * TUE-SAT *)" # ~9pm ET Mon-Fri, after market close
}
