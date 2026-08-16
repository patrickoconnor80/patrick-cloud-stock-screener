resource "aws_iam_role" "stock_screener_lambda" {
  name        = "${local.prefix}-stock-screener-lambda-role"
  description = "Execution role for the stock screener Lambda"
  tags        = local.tags

  assume_role_policy = <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "lambda.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF
}

resource "aws_iam_role_policy_attachment" "stock_screener_lambda_logs" {
  role       = aws_iam_role.stock_screener_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_policy" "stock_screener_lambda" {
  name        = "${local.prefix}-stock-screener-lambda-policy"
  description = "Allows the stock screener Lambda to write results and invalidate CloudFront"
  policy      = data.aws_iam_policy_document.stock_screener_lambda.json
}

data "aws_iam_policy_document" "stock_screener_lambda" {
  statement {
    sid    = "WriteResults"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:GetObject"
    ]
    resources = ["arn:aws:s3:::${var.website_bucket}/${var.results_key}"]
  }

  statement {
    sid    = "InvalidateCache"
    effect = "Allow"
    actions = [
      "cloudfront:CreateInvalidation"
    ]
    resources = ["arn:aws:cloudfront::${data.aws_caller_identity.current.account_id}:distribution/${var.cloudfront_distribution_id}"]
  }
}

resource "aws_iam_role_policy_attachment" "stock_screener_lambda_app" {
  role       = aws_iam_role.stock_screener_lambda.name
  policy_arn = aws_iam_policy.stock_screener_lambda.arn
}

resource "aws_lambda_function" "stock_screener" {
  function_name = "${local.prefix}-stock-screener"
  description   = "Daily stock screener: 200 SMA + 8 EMA + market cap, ranked by EPS"
  role          = aws_iam_role.stock_screener_lambda.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.stock_screener.repository_url}:latest"

  timeout     = 900
  memory_size = 1024

  environment {
    variables = {
      RESULTS_BUCKET             = var.website_bucket
      RESULTS_KEY                = var.results_key
      CLOUDFRONT_DISTRIBUTION_ID = var.cloudfront_distribution_id
      TOP_N                      = "5"
      MIN_MARKETCAP              = "1e9"
    }
  }

  tags = local.tags

  lifecycle {
    ignore_changes = [image_uri] # code deploys update this out-of-band via `aws lambda update-function-code`
  }
}
