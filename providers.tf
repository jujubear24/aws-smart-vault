# This block configures Terraform itself, including the required provider versions
# and the location of the state file.


terraform {
  required_version = ">= 1.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # This is the crucial part for a professional setup.
  # We are telling Terraform to store its state file in the S3 bucket we created.

  backend "s3" {
    bucket         = "smart-vault-tfstate-jb-sept11-2025"
    key            = "global/terraform.tfstate" # The path to the state file in the bucket
    region         = "us-east-1"
    dynamodb_table = "terraform-state-lock"     # The DynamoDB table for state locking
    encrypt        = true
  }
}

# This block configures the AWS provider.
# We define a primary provider and an alias for our disaster recovery region.
# This allows us to manage resources in two different regions from the same codebase.

provider "aws" {
  region = var.primary_aws_region
}


provider "aws" {
  alias  = "dr" # dr stands for Disaster Recovery
  region = var.dr_aws_region
}

