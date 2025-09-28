import json
import boto3
import os
import logging
from typing import Dict, Any
from botocore.exceptions import ClientError

# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    API Gateway handler for Smart Vault restore operations.
    Validates requests and asynchronously invokes the worker Lambda.
    """
    try:
        logger.info("=== API HANDLER START ===")

        worker_lambda_arn = os.environ.get("WORKER_LAMBDA_ARN")
        if not worker_lambda_arn:
            logger.error("FATAL: WORKER_LAMBDA_ARN environment variable is not set")
            # FIX: Added the 'message' key to this error response to match our tests.
            return {
                "statusCode": 500,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(
                    {
                        "error": "Server configuration error",
                        "message": "WORKER_LAMBDA_ARN not set",
                    }
                ),
            }

        request_body = event.get("body")
        if not request_body:
            logger.error("Validation Error: Request body is required")
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(
                    {"error": "Bad Request", "message": "Request body is required"}
                ),
            }

        try:
            payload = json.loads(request_body)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in request body: {e}")
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(
                    {
                        "error": "Bad Request",
                        "message": f"Invalid JSON format: {str(e)}",
                    }
                ),
            }

        if "snapshot_id" not in payload:
            logger.error("Validation Error: 'snapshot_id' is missing from payload")
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(
                    {
                        "error": "Bad Request",
                        "message": "Missing required field: snapshot_id",
                    }
                ),
            }

        lambda_client = boto3.client("lambda")

        logger.info(f"Invoking worker Lambda: {worker_lambda_arn}")
        lambda_client.invoke(
            FunctionName=worker_lambda_arn,
            InvocationType="Event",
            Payload=json.dumps(payload),
        )

        logger.info("=== API HANDLER SUCCESS ===")
        return {
            "statusCode": 202,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(
                {
                    "message": "Restore request accepted and is being processed asynchronously. A notification will be sent upon completion."
                }
            ),
        }

    except Exception as e:
        logger.exception(f"Unexpected error in API handler: {str(e)}")
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "Internal Server Error", "message": str(e)}),
        }
    
