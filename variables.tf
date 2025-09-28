# -----------------------------------------------------------------------------
# AWS PROVIDER VARIABLES
# -----------------------------------------------------------------------------
variable "primary_aws_region" {
  description = "The primary AWS region for deploying resources."
  type        = string
  default     = "us-east-1"
}

variable "dr_aws_region" {
  description = "The disaster recovery AWS region."
  type        = string
  default     = "us-west-2"
}

# -----------------------------------------------------------------------------
# BACKUP CONFIGURATION VARIABLES
# -----------------------------------------------------------------------------
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

# -----------------------------------------------------------------------------
# NEW: COST MONITORING VARIABLES
# -----------------------------------------------------------------------------
variable "billing_alarm_threshold" {
  description = "The threshold in USD for the billing alarm. An alert will be sent if estimated charges exceed this value."
  type        = number
  default     = 5
}





