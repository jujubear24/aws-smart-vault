import json
import boto3
import os
import logging
from typing import Dict, Any
from botocore.exceptions import ClientError, ParamValidationError

# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    This Lambda function restores an EBS volume from a given snapshot ID.
    It is designed to be triggered by Amazon API Gateway.

    Expected JSON payload in the request body:
    {
        "snapshot_id": "snap-0123456789abcdef",
        "availability_zone": "us-east-1a"
    }
    """
    try:
        # Get the region from environment variables
        region = os.environ.get("AWS_REGION", "us-east-1")
        ec2_client = boto3.client("ec2", region_name=region)

        # --- 1. Validate Input ---
        logger.info("Received event: %s", event)
        body = json.loads(event.get("body", "{}"))

        snapshot_id = body.get("snapshot_id")
        az = body.get("availability_zone")

        if not snapshot_id or not az:
            logger.error(
                "Validation Error: snapshot_id and availability_zone are required."
            )
            return {
                "statusCode": 400,
                "body": json.dumps(
                    {
                        "message": "Missing required parameters: snapshot_id and availability_zone"
                    }
                ),
            }

        logger.info(
            "Attempting to restore snapshot %s into Availability Zone %s",
            snapshot_id,
            az,
        )

        # --- 2. Verify Snapshot Exists ---
        try:
            snapshots = ec2_client.describe_snapshots(SnapshotIds=[snapshot_id])
            if not snapshots["Snapshots"]:
                raise ValueError(
                    "Snapshot not found"
                )  # This case is unlikely if describe_snapshots doesn't error
        except ClientError as e:
            if e.response["Error"]["Code"] == "InvalidSnapshot.NotFound":
                logger.error("Snapshot %s not found.", snapshot_id)
                return {
                    "statusCode": 404,
                    "body": json.dumps(
                        {"message": f"Snapshot '{snapshot_id}' not found."}
                    ),
                }
            raise  # Re-raise other client errors

        # --- 3. Create Volume from Snapshot ---
        response = ec2_client.create_volume(
            SnapshotId=snapshot_id,
            AvailabilityZone=az,
            TagSpecifications=[
                {
                    "ResourceType": "volume",
                    "Tags": [
                        {"Key": "Name", "Value": f"Restored from {snapshot_id}"},
                        {"Key": "CreatedBy", "Value": "SmartVaultRestoreLambda"},
                    ],
                }
            ],
        )

        new_volume_id = response["VolumeId"]
        logger.info("Successfully initiated volume creation: %s", new_volume_id)

        # --- 4. Return Success Response ---
        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "message": "Volume restore initiated successfully.",
                    "volume_id": new_volume_id,
                    "snapshot_id": snapshot_id,
                    "availability_zone": az,
                }
            ),
        }

    except json.JSONDecodeError:
        logger.error("Invalid JSON in request body.")
        return {
            "statusCode": 400,
            "body": json.dumps({"message": "Invalid JSON format in request body."}),
        }
    except (ClientError, ParamValidationError) as e:
        logger.error("Boto3 client error: %s", str(e))
        return {
            "statusCode": 500,
            "body": json.dumps({"message": f"An AWS service error occurred: {str(e)}"}),
        }
    except Exception as e:
        logger.error("An unexpected error occurred: %s", str(e))
        return {
            "statusCode": 500,
            "body": json.dumps({"message": f"An unexpected error occurred: {str(e)}"}),
        }
