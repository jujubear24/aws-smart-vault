# This file contains the core infrastructure resources for our Smart Vault project.

# =================================================================================
# Data Source to Package Lambda Code
# =================================================================================
# This data source creates a zip archive of our Python source code, which is
# required for deploying it to AWS Lambda.
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/src"
  output_path = "${path.module}/dist/lambda_function.zip"
}

# =================================================================================
# AWS Lambda Function
# =================================================================================
# This is the serverless function that will execute our backup logic.
resource "aws_lambda_function" "smart_vault_lambda" {
  filename      = data.archive_file.lambda_zip.output_path
  function_name = "${var.project_name}-Backup-Function"
  role          = aws_iam_role.smart_vault_lambda_exec_role.arn
  handler       = "lambda_function.lambda_handler" # File name.function name
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  runtime = "python3.9"
  timeout = 300 # 5 minutes

  environment {
    variables = {
      RETENTION_DAYS   = var.retention_days
      BACKUP_TAG_KEY   = var.backup_tag_key
      BACKUP_TAG_VALUE = var.backup_tag_value
      DR_REGION        = var.dr_aws_region
      SNS_TOPIC_ARN    = aws_sns_topic.smart_vault_notifications.arn
    }
  }

  tags = {
    Project = var.project_name
  }
}

# =================================================================================
# Amazon SNS for Notifications
# =================================================================================
# This SNS topic will receive success or failure notifications from our Lambda.
resource "aws_sns_topic" "smart_vault_notifications" {
  name = "${var.project_name}-Notifications"

  tags = {
    Project = var.project_name
  }
}

# =================================================================================
# Amazon EventBridge (CloudWatch Events) for Scheduling
# =================================================================================
# This rule triggers our Lambda function on a schedule.
# The default is "rate(1 day)", but you can change it to a cron expression
# for more specific timing, e.g., "cron(0 5 * * ? *)" for 5 AM UTC daily.
resource "aws_cloudwatch_event_rule" "lambda_scheduler" {
  name                = "${var.project_name}-Daily-Scheduler"
  description         = "Triggers the Smart Vault backup Lambda daily."
  schedule_expression = "rate(1 day)"
}

# This target connects the EventBridge rule to our Lambda function.
resource "aws_cloudwatch_event_target" "lambda_target" {
  rule      = aws_cloudwatch_event_rule.lambda_scheduler.name
  arn       = aws_lambda_function.smart_vault_lambda.arn
}

# This permission allows EventBridge to invoke our Lambda function.
resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.smart_vault_lambda.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.lambda_scheduler.arn
}

# =================================================================================
# IAM Role and Policy (Defined in Step 2)
# =================================================================================
# We will keep the IAM role and policy definitions from the previous step.
# They are required for the Lambda function to have the correct permissions.

resource "aws_iam_role" "smart_vault_lambda_exec_role" {
  name = "${var.project_name}-Lambda-ExecRole"

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

  tags = {
    Project = var.project_name
  }
}

resource "aws_iam_role_policy" "smart_vault_lambda_policy" {
  name = "${var.project_name}-Lambda-Policy"
  role = aws_iam_role.smart_vault_lambda_exec_role.id

  # SECURITY UPDATE: We are now locking down the SNS Publish action to the
  # specific topic we are creating in this file. This is a critical best practice.
  policy = jsonencode({
    Version   = "2012-10-17",
    Statement = [
      {
        Effect   = "Allow",
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Effect   = "Allow",
        Action   = ["ec2:DescribeInstances", "ec2:CreateSnapshot", "ec2:CreateSnapshots", "ec2:DeleteSnapshot", "ec2:DescribeSnapshots", "ec2:CopySnapshot"],
        Resource = "*"
      },
      {
        Effect   = "Allow",
        Action   = ["ec2:CreateTags", "ec2:DeleteTags"],
        Resource = "arn:aws:ec2:*:*:*",
        Condition = {
          StringEquals = {
            "ec2:CreateAction" = ["CreateSnapshot", "CopySnapshot"]
          }
        }
      },
      {
        # This is now more secure!
        Effect   = "Allow",
        Action   = "sns:Publish",
        Resource = aws_sns_topic.smart_vault_notifications.arn
      }
    ]
  })
}
