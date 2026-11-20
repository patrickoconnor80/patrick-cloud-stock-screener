locals {
    prefix = "patrick-cloud-${var.env}"
    tradier_token_param = "/patrick-cloud/${var.env}/tradier-token"
    tags = {
        env        = var.env
        project    = "patrick-cloud"
        deployment = "terraform"
        repo       = "https://github.com/patrickoconnor80/patrick-cloud-stock-screener/tree/main/tf"
    }
}
