# =================================================================================
# IAM Role for the Smart Vault Lambda Function
# =================================================================================
# This role grants our Lambda function the specific permissions it needs to operate,
# following the principle of least privilege.

resource "aws_iam_role" "smart_vault_lambda_exec_role" {
  name = "${var.project_name}-Lambda-ExecRole"

  # This policy allows the AWS Lambda service to assume this role.
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

# =================================================================================
# IAM Policy for the Smart Vault Lambda Function
# =================================================================================
# This policy is attached to the IAM role and defines what actions the Lambda
# function is allowed to perform on which resources.

resource "aws_iam_role_policy" "smart_vault_lambda_policy" {
  name = "${var.project_name}-Lambda-Policy"
  role = aws_iam_role.smart_vault_lambda_exec_role.id

  policy = jsonencode({
    Version   = "2012-10-17",
    Statement = [
      {
        # Permissions for logging to CloudWatch
        Effect   = "Allow",
        Action   = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ],
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        # Permissions for managing EC2 Snapshots
        Effect   = "Allow",
        Action   = [
          "ec2:DescribeInstances",
          "ec2:CreateSnapshot",
          "ec2:CreateSnapshots",
          "ec2:DeleteSnapshot",
          "ec2:DescribeSnapshots",
          "ec2:CopySnapshot"
        ],
        Resource = "*" # Snapshots and Instances are not easily resource-constrained by ARN
      },
      {
        # Permissions for creating and deleting tags on snapshots
        Effect = "Allow",
        Action = [
          "ec2:CreateTags",
          "ec2:DeleteTags"
        ],
        # This condition ensures the Lambda can only tag/untag resources
        # that are related to EC2, which is a good security practice.
        Resource = "arn:aws:ec2:*:*:*",
        Condition = {
          StringEquals = {
            "ec2:CreateAction" = [
              "CreateSnapshot",
              "CopySnapshot"
            ]
          }
        }
      },
      {
        # Permissions to publish notifications to our SNS topic (which we will create later)
        Effect   = "Allow",
        Action   = "sns:Publish",
        Resource = "*" # We will lock this down to a specific SNS topic ARN later
      }
    ]
  })
}
