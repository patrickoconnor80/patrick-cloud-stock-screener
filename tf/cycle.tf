# Daily market-cycle readout (cycle.py): score, phase, breadth, beta and macro
# panels for data.html, plus washout/overheated/turn alerts to the GEX alert
# subscribers. Same image and role as the screener; image_config picks the handler.

resource "aws_lambda_function" "cycle" {
  function_name = "${local.prefix}-market-cycle"
  description   = "Daily market cycle readout, written to the website after the close"
  role          = aws_iam_role.stock_screener_lambda.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.stock_screener.repository_url}:latest"

  image_config {
    command = ["cycle_handler.lambda_handler"]
  }

  # ~3,900 member price histories; more memory = more CPU for the downloads.
  timeout     = 900
  memory_size = 3008

  environment {
    variables = {
      RESULTS_BUCKET             = var.website_bucket
      CYCLE_KEY                  = var.cycle_key
      CLOUDFRONT_DISTRIBUTION_ID = var.cloudfront_distribution_id
      ALERT_TOPIC_ARN            = aws_sns_topic.gex_alerts.arn
      SES_FROM                   = "Market Alerts <alerts@${var.ses_domain}>"
    }
  }

  tags = local.tags

  lifecycle {
    ignore_changes = [image_uri] # code deploys update this via `aws lambda update-function-code`
  }
}

resource "aws_cloudwatch_event_rule" "cycle" {
  name                = "${local.prefix}-market-cycle-daily"
  description         = "Triggers the market cycle Lambda each weekday evening"
  schedule_expression = var.cycle_schedule_expression
  tags                = local.tags
}

resource "aws_cloudwatch_event_target" "cycle" {
  rule      = aws_cloudwatch_event_rule.cycle.name
  target_id = "market-cycle-lambda"
  arn       = aws_lambda_function.cycle.arn
}

resource "aws_lambda_permission" "cycle_allow_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.cycle.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.cycle.arn
}
