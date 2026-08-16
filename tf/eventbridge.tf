resource "aws_cloudwatch_event_rule" "stock_screener_daily" {
  name                = "${local.prefix}-stock-screener-daily"
  description         = "Triggers the stock screener Lambda daily after market close"
  schedule_expression = var.schedule_expression
  tags                = local.tags
}

resource "aws_cloudwatch_event_target" "stock_screener_daily" {
  rule      = aws_cloudwatch_event_rule.stock_screener_daily.name
  target_id = "stock-screener-lambda"
  arn       = aws_lambda_function.stock_screener.arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.stock_screener.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.stock_screener_daily.arn
}
