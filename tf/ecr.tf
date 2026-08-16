resource "aws_ecr_repository" "stock_screener" {
  name                 = "${local.prefix}-stock-screener"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = local.tags
}
