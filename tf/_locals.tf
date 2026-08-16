locals {
    prefix = "patrick-cloud-${var.env}"
    tags = {
        env        = var.env
        project    = "patrick-cloud"
        deployment = "terraform"
        repo       = "https://github.com/patrickoconnor80/patrick-cloud-stock-screener/tree/main/tf"
    }
}
