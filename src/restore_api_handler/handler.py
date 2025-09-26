import json
import boto3
import os
import logging
from typing import Dict, Any

# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    This is a synchronous Lambda function triggered by API Gateway.
    Its sole purpose is to validate the incoming request and then
    asynchronously invoke the long-running worker Lambda.
    This ensures the API Gateway receives a response within its 29-second timeout.
    """
    try:
        # Get the ARN of the worker function from an environment variable
        worker_lambda_arn = os.environ["WORKER_LAMBDA_ARN"]
        lambda_client = boto3.client("lambda")

        logger.info("API Handler received event: %s", event)

        # The original request body is passed directly to the worker
        request_body = event.get("body", "{}")

        # Basic validation: ensure the body is valid JSON.
        # The worker will perform the detailed business logic validation.
        payload = json.loads(request_body)

        logger.info("Invoking worker Lambda '%s' with payload.", worker_lambda_arn)

        # Asynchronously invoke the worker Lambda
        lambda_client.invoke(
            FunctionName=worker_lambda_arn,
            InvocationType="Event",  # 'Event' means asynchronous invocation
            Payload=json.dumps(payload),
        )

        return {
            "statusCode": 202,  # 202 Accepted is the standard for async operations
            "body": json.dumps(
                {
                    "message": "Restore request accepted and is being processed asynchronously. A notification will be sent upon completion."
                }
            ),
        }

    except json.JSONDecodeError:
        logger.error("Invalid JSON in request body.")
        return {
            "statusCode": 400,
            "body": json.dumps({"message": "Invalid JSON format in request body."}),
        }
    except KeyError:
        logger.error("WORKER_LAMBDA_ARN environment variable is not set.")
        return {
            "statusCode": 500,
            "body": json.dumps({"message": "Server configuration error."}),
        }
    except Exception as e:
        logger.error("An unexpected error occurred in the API handler: %s", str(e))
        return {
            "statusCode": 500,
            "body": json.dumps({"message": f"An unexpected error occurred: {str(e)}"}),
        }
