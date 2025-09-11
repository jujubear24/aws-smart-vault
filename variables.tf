# This file defines the input variables for our Terraform project.
# Using variables makes our code reusable and easy to configure without
# changing the core logic.

variable "primary_aws_region" {
  description = "The primary AWS region where the main infrastructure will be deployed."
  type        = string
  default     = "us-east-1"
}

variable "dr_aws_region" {
  description = "The disaster recovery AWS region for cross-region backups."
  type        = string
  default     = "us-west-2"
}

variable "project_name" {
  description = "The name of the project, used for tagging resources."
  type        = string
  default     = "SmartVault"
}

