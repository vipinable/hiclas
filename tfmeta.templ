#Set default region
provider "aws" {
  region = "us-east-1"
}

data "aws_caller_identity" "current" {}
data "aws_availability_zones" "available" { state = "available" }


##remote backend configuration
terraform {
  required_providers {
    aws = {
      source  = "registry.terraform.io/hashicorp/aws"
      version = "~> 4.19.0"
    }
 }
backend "s3" {
    dynamodb_table = "awstfbackend-tfstatefilestorelock"
    bucket         = "awstfbackend-tfstatefilestore-731685434595"
    key            = "<appname>/<envname>/terraform.tfstate"
  }
}
