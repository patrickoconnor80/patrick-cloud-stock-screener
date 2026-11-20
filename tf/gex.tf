# Dealer-gamma (GEX) snapshot for SPY/QQQ. Reuses the screener's image and
# execution role; image_config points the container at gex_handler instead.

resource "aws_lambda_function" "gex" {
  function_name = "${local.prefix}-gex"
  description   = "SPY/QQQ dealer gamma snapshot, written to the website every 15 min"
  role          = aws_iam_role.stock_screener_lambda.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.stock_screener.repository_url}:latest"

  image_config {
    command = ["gex_handler.lambda_handler"]
  }

  timeout     = 120
  memory_size = 512

  environment {
    variables = {
      RESULTS_BUCKET             = var.website_bucket
      GEX_KEY                    = var.gex_key
      CLOUDFRONT_DISTRIBUTION_ID = var.cloudfront_distribution_id
      ALERT_TOPIC_ARN            = aws_sns_topic.gex_alerts.arn
      SES_FROM                   = "GEX Alerts <alerts@${var.ses_domain}>"
      # SecureString created by hand (not in Terraform, so the token stays out of
      # state). Absent = the Lambda uses Yahoo SPY/QQQ instead of Tradier SPX/NDX.
      TRADIER_TOKEN_PARAM = local.tradier_token_param
    }
  }

  tags = local.tags

  lifecycle {
    ignore_changes = [image_uri] # code deploys update this via `aws lambda update-function-code`
  }
}

resource "aws_cloudwatch_event_rule" "gex" {
  name                = "${local.prefix}-gex-intraday"
  description         = "Triggers the GEX snapshot Lambda every 15 min on weekdays"
  schedule_expression = var.gex_schedule_expression
  tags                = local.tags
}

resource "aws_cloudwatch_event_target" "gex" {
  rule      = aws_cloudwatch_event_rule.gex.name
  target_id = "gex-lambda"
  arn       = aws_lambda_function.gex.arn
}

resource "aws_lambda_permission" "gex_allow_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.gex.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.gex.arn
}

# Email alerts for pin setups and upcoming events. Subscriptions are managed
# outside Terraform (email endpoints need a confirmation click anyway):
#   aws sns subscribe --topic-arn <arn> --protocol email --notification-endpoint <address>
resource "aws_sns_topic" "gex_alerts" {
  name = "${local.prefix}-gex-alerts"
  tags = local.tags
}

# sns:Publish for the Lambda lives in the shared managed policy in lambda.tf.

output "gex_alert_topic_arn" {
  value = aws_sns_topic.gex_alerts.arn
}

# ---- HTML alert emails via SES (the heatmap needs HTML; SNS email is text only).
# Sender domain is verified through Route 53 (TXT + Easy DKIM). Recipients are
# the topic's confirmed email subscribers; while SES is in sandbox each one must
# also be verified once by hand (it is an address, so it stays out of Terraform):
#   aws sesv2 create-email-identity --email-identity <address>
data "aws_route53_zone" "site" {
  name = var.ses_domain
}

resource "aws_ses_domain_identity" "alerts" {
  domain = var.ses_domain
}

resource "aws_route53_record" "ses_verification" {
  zone_id = data.aws_route53_zone.site.zone_id
  name    = "_amazonses.${var.ses_domain}"
  type    = "TXT"
  ttl     = 600
  records = [aws_ses_domain_identity.alerts.verification_token]
}

resource "aws_ses_domain_dkim" "alerts" {
  domain = aws_ses_domain_identity.alerts.domain
}

resource "aws_route53_record" "ses_dkim" {
  count   = 3
  zone_id = data.aws_route53_zone.site.zone_id
  name    = "${aws_ses_domain_dkim.alerts.dkim_tokens[count.index]}._domainkey.${var.ses_domain}"
  type    = "CNAME"
  ttl     = 600
  records = ["${aws_ses_domain_dkim.alerts.dkim_tokens[count.index]}.dkim.amazonses.com"]
}
