terraform {
    backend "s3" {}
    required_providers {
        aws = {
            source  = "hashicorp/aws"
            version = ">= 4"
        }
    }
}

provider "aws" {
    region = "us-east-1"
}

