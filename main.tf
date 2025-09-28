# -----------------------------------------------------------------------------
# DATA SOURCES
# -----------------------------------------------------------------------------
data "aws_caller_identity" "current" {}

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/src"
  output_path = "${path.module}/dist/lambda_function.zip"
}

# -----------------------------------------------------------------------------
# IAM (Security) RESOURCES FOR BACKUP FUNCTION
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
      },
      {
        Action = [
          "xray:PutTraceSegments",
          "xray:PutTelemetryRecords"
        ],
        Effect   = "Allow",
        Resource = "*"
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# APPLICATION RESOURCES FOR BACKUP
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
                "kms:CallerAccount": data.aws_caller_identity.current.account_id,
                "kms:ViaService": "ec2.${var.dr_aws_region}.amazonaws.com"
            }
        }
      },
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
# BACKUP LAMBDA FUNCTION
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

  tracing_config {
    mode = "Active"
  }
}

# -----------------------------------------------------------------------------
# IAM RESOURCES FOR RESTORE FUNCTIONALITY
# -----------------------------------------------------------------------------
resource "aws_iam_role" "smart_vault_restore_lambda_role" {
  name = "SmartVault-Restore-Lambda-Role"

  assume_role_policy = jsonencode({
    Version   = "2012-10-17",
    Statement = [
      {
        Action    = "sts:AssumeRole",
        Effect    = "Allow",
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "smart_vault_restore_lambda_policy" {
  name = "SmartVault-Restore-Lambda-Policy"
  role = aws_iam_role.smart_vault_restore_lambda_role.id

  policy = jsonencode({
    Version   = "2012-10-17",
    Statement = [
      {
        Action = [
          "ec2:DescribeSnapshots",
          "ec2:CreateVolume",
          "ec2:CreateTags",
          "ec2:RunInstances",
          "ec2:AttachVolume",
          "ec2:DescribeVolumes",
          "ec2:DescribeSubnets",
          "ec2:DescribeInstances"
        ],
        Effect   = "Allow",
        Resource = "*"
      },
      {
        Action   = "sns:Publish",
        Effect   = "Allow",
        Resource = aws_sns_topic.smart_vault_notifications.arn
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "restore_worker_logs" {
  role       = aws_iam_role.smart_vault_restore_lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "restore_worker_xray" {
  role       = aws_iam_role.smart_vault_restore_lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

# -----------------------------------------------------------------------------
# RESTORE LAMBDA (ASYNCHRONOUS WORKER)
# -----------------------------------------------------------------------------
data "archive_file" "restore_lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/src/restore_handler"
  output_path = "${path.module}/dist/restore_function.zip"
}

resource "aws_lambda_function" "smart_vault_restore_worker_lambda" {
  filename         = data.archive_file.restore_lambda_zip.output_path
  function_name    = "SmartVault-Restore-Worker-Function"
  role             = aws_iam_role.smart_vault_restore_lambda_role.arn
  handler          = "restore_function.handler"
  runtime          = "python3.9"
  timeout          = 300
  memory_size      = 128
  source_code_hash = data.archive_file.restore_lambda_zip.output_base64sha256

  environment {
    variables = {
      SNS_TOPIC_ARN = aws_sns_topic.smart_vault_notifications.arn
    }
  }

  tracing_config {
    mode = "Active"
  }
}

# -----------------------------------------------------------------------------
# API HANDLER LAMBDA (SYNCHRONOUS)
# -----------------------------------------------------------------------------
data "archive_file" "restore_api_handler_zip" {
  type        = "zip"
  source_dir  = "${path.module}/src/restore_api_handler"
  output_path = "${path.module}/dist/restore_api_handler.zip"
}

resource "aws_iam_role" "smart_vault_restore_api_handler_role" {
  name = "SmartVault-Restore-API-Handler-Role"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17",
    Statement = [{
      Action    = "sts:AssumeRole",
      Effect    = "Allow",
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "api_handler_lambda_logs" {
  role       = aws_iam_role.smart_vault_restore_api_handler_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "api_handler_xray" {
  role       = aws_iam_role.smart_vault_restore_api_handler_role.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

resource "aws_iam_role_policy" "api_handler_invoke_worker_policy" {
  name = "SmartVault-API-Handler-Invoke-Worker-Policy"
  role = aws_iam_role.smart_vault_restore_api_handler_role.id
  policy = jsonencode({
    Version   = "2012-10-17",
    Statement = [
      {
        Action   = "lambda:InvokeFunction",
        Effect   = "Allow",
        Resource = aws_lambda_function.smart_vault_restore_worker_lambda.arn
      }
    ]
  })
}

resource "aws_lambda_function" "smart_vault_restore_api_handler_lambda" {
  filename         = data.archive_file.restore_api_handler_zip.output_path
  function_name    = "SmartVault-Restore-API-Handler-Function"
  role             = aws_iam_role.smart_vault_restore_api_handler_role.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.9"
  timeout          = 20
  memory_size      = 128
  source_code_hash = data.archive_file.restore_api_handler_zip.output_base64sha256

  environment {
    variables = {
      WORKER_LAMBDA_ARN = aws_lambda_function.smart_vault_restore_worker_lambda.arn
    }
  }

  tracing_config {
    mode = "Active"
  }

  depends_on = [
    aws_iam_role_policy_attachment.api_handler_lambda_logs,
    aws_iam_role_policy_attachment.api_handler_xray,
    aws_iam_role_policy.api_handler_invoke_worker_policy
  ]
}

# -----------------------------------------------------------------------------
# API GATEWAY FOR RESTORE FUNCTIONALITY
# -----------------------------------------------------------------------------
resource "aws_api_gateway_rest_api" "smart_vault_api" {
  name        = "SmartVaultAPI"
  description = "API for Smart Vault restore operations"
}

resource "aws_api_gateway_resource" "restore_resource" {
  rest_api_id = aws_api_gateway_rest_api.smart_vault_api.id
  parent_id   = aws_api_gateway_rest_api.smart_vault_api.root_resource_id
  path_part   = "restore"
}

resource "aws_api_gateway_method" "restore_method" {
  rest_api_id      = aws_api_gateway_rest_api.smart_vault_api.id
  resource_id      = aws_api_gateway_resource.restore_resource.id
  http_method      = "POST"
  authorization    = "NONE"
  api_key_required = true
}

resource "aws_api_gateway_integration" "lambda_integration" {
  rest_api_id             = aws_api_gateway_rest_api.smart_vault_api.id
  resource_id             = aws_api_gateway_resource.restore_resource.id
  http_method             = aws_api_gateway_method.restore_method.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.smart_vault_restore_api_handler_lambda.invoke_arn
}

resource "aws_lambda_permission" "api_gateway_permission" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.smart_vault_restore_api_handler_lambda.function_name
  principal     = "apigateway.amazonaws.com"

  # DEFINITIVE FIX: Use a slightly broader but very common source ARN format.
  # This grants permission for any method on any resource within our specific API,
  # which is secure and eliminates any subtle interpolation issues that were
  # causing the integration to fail.
  source_arn = "${aws_api_gateway_rest_api.smart_vault_api.execution_arn}/*/*"
}

resource "aws_api_gateway_deployment" "api_deployment" {
  rest_api_id = aws_api_gateway_rest_api.smart_vault_api.id

  # DEFINITIVE FIX: Add a 'triggers' block. This creates a unique hash of our
  # API's configuration. If any part of the API changes (like its integration),
  # the hash changes, which forces Terraform to create a fresh deployment.
  # This eliminates any chance of a stale deployment causing permission issues.
  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_resource.restore_resource.id,
      aws_api_gateway_method.restore_method.id,
      aws_api_gateway_integration.lambda_integration.id,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [aws_api_gateway_integration.lambda_integration]
}

resource "aws_api_gateway_stage" "api_stage" {
  deployment_id = aws_api_gateway_deployment.api_deployment.id
  rest_api_id   = aws_api_gateway_rest_api.smart_vault_api.id
  stage_name    = "v1"
}

# -----------------------------------------------------------------------------
# API KEY AND USAGE PLAN FOR SECURITY
# -----------------------------------------------------------------------------
resource "aws_api_gateway_api_key" "smart_vault_api_key" {
  name = "SmartVault-Client-Key"
}

resource "aws_api_gateway_usage_plan" "api_usage_plan" {
  name = "SmartVaultUsagePlan"
  api_stages {
    api_id = aws_api_gateway_rest_api.smart_vault_api.id
    stage  = aws_api_gateway_stage.api_stage.stage_name
  }
}

resource "aws_api_gateway_usage_plan_key" "main" {
  key_id        = aws_api_gateway_api_key.smart_vault_api_key.id
  key_type      = "API_KEY"
  usage_plan_id = aws_api_gateway_usage_plan.api_usage_plan.id
}

# -----------------------------------------------------------------------------
# TERRAFORM OUTPUTS
# -----------------------------------------------------------------------------
output "dr_kms_key_arn" {
  description = "ARN of the DR region KMS key"
  value       = aws_kms_key.dr_snapshot_key.arn
}

output "restore_api_invoke_url" {
  description = "The invoke URL for the restore API."
  value       = aws_api_gateway_stage.api_stage.invoke_url
}

output "api_key_value" {
  description = "The value of the API key for authenticating requests."
  value       = aws_api_gateway_api_key.smart_vault_api_key.value
  sensitive   = true
}


# -----------------------------------------------------------------------------
# FINAL FEATURE: COST MONITORING & ALERTING
# -----------------------------------------------------------------------------
# This creates a CloudWatch alarm that monitors the total estimated charges
# for the entire AWS account. If the estimated charges exceed the threshold,
# it will send a notification to our existing SNS topic.
# NOTE: This requires that "Billing Alerts" have been manually enabled in the
# AWS Account's billing preferences.
resource "aws_cloudwatch_metric_alarm" "billing_alarm" {
  # This provider block is crucial. Billing metrics are always published in us-east-1.
  provider = aws

  alarm_name          = "SmartVault-Total-Account-Billing-Alarm"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "EstimatedCharges"
  namespace           = "AWS/Billing"
  period              = 21600 # Check every 6 hours
  statistic           = "Maximum"
  threshold           = var.billing_alarm_threshold # Defaults to 5 USD
  alarm_description   = "This alarm triggers if the AWS account's estimated charges exceed the configured threshold."

  dimensions = {
    Currency = "USD"
  }

  # Send the notification to our existing SNS topic
  alarm_actions = [aws_sns_topic.smart_vault_notifications.arn]
}





