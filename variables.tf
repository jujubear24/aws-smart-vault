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

# LAMBDA CONFIGURATION ---

variable "retention_days" {
  description = "The number of days to retain EBS snapshots."
  type        = number
  default     = 7
}

variable "backup_tag_key" {
  description = "The tag key used to identify EC2 instances for backup."
  type        = string
  default     = "Backup-Tier"
}

variable "backup_tag_value" {
  description = "The tag value used to identify EC2 instances for backup."
  type        = string
  default     = "Gold"
}



