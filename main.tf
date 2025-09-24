# -----------------------------------------------------------------------------
# DATA SOURCES
# -----------------------------------------------------------------------------
# This fetches information about the AWS account running the Terraform command,
# which we need for the KMS policy.
data "aws_caller_identity" "current" {}

# This zips up our Python source code for deployment to Lambda.
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/src"
  output_path = "${path.module}/dist/lambda_function.zip"
}

# -----------------------------------------------------------------------------
# IAM (Security) RESOURCES
# -----------------------------------------------------------------------------
resource "aws_iam_role" "smart_vault_lambda_exec_role" {
  name = "SmartVault-Lambda-ExecRole"

  assume_role_policy = jsonencode({
    Version   = "2012-10-17",
    Statement = [
      {
        Action    = "sts:AssumeRole",
        Effect    = "Allow",
        Sid       = "",
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })
}

# Enhanced Lambda IAM policy with additional KMS permissions

resource "aws_iam_role_policy" "smart_vault_lambda_policy" {
  name = "SmartVault-Lambda-Policy"
  role = aws_iam_role.smart_vault_lambda_exec_role.id

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ],
        Effect   = "Allow",
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Action = [
          "ec2:CreateSnapshot",
          "ec2:CreateSnapshots",
          "ec2:DeleteSnapshot",
          "ec2:DescribeInstances",
          "ec2:DescribeSnapshots",
          "ec2:CopySnapshot",
          "ec2:DescribeVolumes",
          "ec2:CreateTags"
        ],
        Effect   = "Allow",
        Resource = "*"
      },
      {
        Action = [
          "kms:Decrypt",
          "kms:Encrypt",
          "kms:GenerateDataKey",
          "kms:GenerateDataKeyWithoutPlaintext",
          "kms:DescribeKey",
          "kms:CreateGrant",
          "kms:ListGrants",
          "kms:RevokeGrant",
          "kms:GetKeyPolicy"
        ],
        Effect   = "Allow",
        Resource = [
          "*",
          aws_kms_key.dr_snapshot_key.arn
        ]
      },
      {
        Action   = "sns:Publish",
        Effect   = "Allow",
        Resource = aws_sns_topic.smart_vault_notifications.arn
      }
    ]
  })
}

# Output the KMS key ARN for verification
output "dr_kms_key_arn" {
  description = "ARN of the DR region KMS key"
  value       = aws_kms_key.dr_snapshot_key.arn
}

# -----------------------------------------------------------------------------
# APPLICATION RESOURCES
# -----------------------------------------------------------------------------
resource "aws_sns_topic" "smart_vault_notifications" {
  name = "SmartVault-Notifications"
}

resource "aws_cloudwatch_event_rule" "lambda_scheduler" {
  name                = "SmartVault-Daily-Backup-Trigger"
  description         = "Triggers the Smart Vault backup Lambda daily"
  schedule_expression = "rate(1 day)"
}

resource "aws_cloudwatch_event_target" "lambda_target" {
  rule      = aws_cloudwatch_event_rule.lambda_scheduler.name
  arn       = aws_lambda_function.smart_vault_lambda.arn
  target_id = "TriggerLambda"
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.smart_vault_lambda.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.lambda_scheduler.arn
}

# -----------------------------------------------------------------------------
# Dedicated KMS Key for Disaster Recovery Encryption
# -----------------------------------------------------------------------------
resource "aws_kms_key" "dr_snapshot_key" {
  provider                = aws.dr
  description             = "KMS key for encrypting DR snapshots for Smart Vault"
  deletion_window_in_days = 10
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17",
    Id      = "dr-key-policy",
    Statement = [
      {
        Sid       = "Enable IAM User Permissions",
        Effect    = "Allow",
        Principal = { "AWS" : "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" },
        Action    = "kms:*",
        Resource  = "*"
      },
      {
        Sid = "Allow SmartVault Lambda to use this key for encryption",
        Effect = "Allow",
        Principal = {
          "AWS" : aws_iam_role.smart_vault_lambda_exec_role.arn
        },
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey",
          "kms:CreateGrant",
          "kms:ListGrants",
          "kms:RevokeGrant"
        ],
        Resource = "*"
      },
      {
        Sid = "Allow EC2 service to use the key for snapshot operations",
        Effect = "Allow",
        Principal = {
          "Service": "ec2.amazonaws.com"
        },
        Action = [
          "kms:CreateGrant",
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ],
        Resource = "*",
        Condition = {
          "StringEquals": {
            "kms:CallerAccount": data.aws_caller_identity.current.account_id
          }
        }
      },
      # Additional statement for cross-region operations
      {
        Sid = "Allow cross-region snapshot operations",
        Effect = "Allow",
        Principal = {
          "AWS" : aws_iam_role.smart_vault_lambda_exec_role.arn
        },
        Action = [
          "kms:CreateGrant"
        ],
        Resource = "*",
        Condition = {
          "Bool": {
            "kms:GrantIsForAWSResource": "true"
          },
          "StringEquals": {
            "kms:CallerAccount": data.aws_caller_identity.current.account_id
          }
        }
      }
    ]
  })

  tags = {
    Name        = "SmartVault-DR-KMS-Key"
    Environment = "Production"
    ManagedBy   = "Terraform"
  }
}
 

resource "aws_kms_alias" "dr_snapshot_key_alias" {
  provider      = aws.dr
  name          = "alias/smart-vault-dr-key"
  target_key_id = aws_kms_key.dr_snapshot_key.key_id
}

# -----------------------------------------------------------------------------
# Lambda Function with KMS Key Awareness
# -----------------------------------------------------------------------------
resource "aws_lambda_function" "smart_vault_lambda" {
  filename         = data.archive_file.lambda_zip.output_path
  function_name    = "SmartVault-Backup-Function"
  role             = aws_iam_role.smart_vault_lambda_exec_role.arn
  handler          = "lambda_function.lambda_handler"
  runtime          = "python3.9"
  timeout          = 300
  memory_size      = 128
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  environment {
    variables = {
      RETENTION_DAYS   = var.retention_days
      BACKUP_TAG_KEY   = var.backup_tag_key
      BACKUP_TAG_VALUE = var.backup_tag_value
      DR_REGION        = var.dr_aws_region
      SNS_TOPIC_ARN    = aws_sns_topic.smart_vault_notifications.arn
      DR_KMS_KEY_ARN   = aws_kms_key.dr_snapshot_key.arn
    }
  }
}




