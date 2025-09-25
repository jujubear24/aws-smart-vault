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
    If instance parameters are provided, it also launches a new EC2 instance
    and attaches the restored volume.

    Expected JSON payload:
    {
        "snapshot_id": "snap-0123456789abcdef",
        "availability_zone": "us-east-1a",
        "launch_instance": true, // optional
        "instance_type": "t2.micro", // required if launch_instance is true
        "ami_id": "ami-0c55b159cbfafe1f0", // required if launch_instance is true
        "subnet_id": "subnet-xxxxxxxx", // required if launch_instance is true
        "device_name": "/dev/sdf" // optional, defaults to /dev/sdf
    }
    """
    try:
        region = os.environ.get("AWS_REGION", "us-east-1")
        ec2_client = boto3.client("ec2", region_name=region)

        logger.info("Received event: %s", event)
        body = json.loads(event.get("body", "{}"))

        snapshot_id = body.get("snapshot_id")
        az = body.get("availability_zone")

        if not snapshot_id or not az:
            return _create_error_response(
                400, "Missing required parameters: snapshot_id and availability_zone"
            )

        logger.info(
            "Attempting to restore snapshot %s into Availability Zone %s",
            snapshot_id,
            az,
        )

        _verify_snapshot_exists(ec2_client, snapshot_id)

        new_volume_id = _create_volume(ec2_client, snapshot_id, az)

        # --- ENHANCEMENT: Launch instance if requested ---
        if body.get("launch_instance"):
            instance_params = {
                "instance_type": body.get("instance_type"),
                "ami_id": body.get("ami_id"),
                "subnet_id": body.get("subnet_id"),
            }
            # Validate required parameters for instance launch
            if not all(instance_params.values()):
                return _create_error_response(
                    400,
                    "Missing required parameters for instance launch: instance_type, ami_id, and subnet_id are required.",
                )

            logger.info(
                "Instance launch requested. Waiting for volume %s to become available.",
                new_volume_id,
            )

            # Wait for volume to be available before launching instance
            waiter = ec2_client.get_waiter("volume_available")
            waiter.wait(VolumeIds=[new_volume_id])
            logger.info("Volume %s is now available.", new_volume_id)

            new_instance_id = _launch_instance(ec2_client, instance_params, az)

            logger.info(
                "Instance %s launched. Waiting for it to be in 'running' state.",
                new_instance_id,
            )
            waiter = ec2_client.get_waiter("instance_running")
            waiter.wait(InstanceIds=[new_instance_id])
            logger.info(
                "Instance %s is running. Attaching volume %s.",
                new_instance_id,
                new_volume_id,
            )

            device_name = body.get("device_name", "/dev/sdf")
            _attach_volume(ec2_client, new_instance_id, new_volume_id, device_name)

            return {
                "statusCode": 200,
                "body": json.dumps(
                    {
                        "message": "Instance launch and volume attachment initiated successfully.",
                        "instance_id": new_instance_id,
                        "volume_id": new_volume_id,
                    }
                ),
            }
        else:
            return {
                "statusCode": 200,
                "body": json.dumps(
                    {
                        "message": "Volume restore initiated successfully.",
                        "volume_id": new_volume_id,
                    }
                ),
            }

    except json.JSONDecodeError:
        return _create_error_response(400, "Invalid JSON format in request body.")
    except (ClientError, ParamValidationError) as e:
        logger.error("Boto3 client error: %s", str(e))
        return _create_error_response(500, f"An AWS service error occurred: {str(e)}")
    except Exception as e:
        logger.error("An unexpected error occurred: %s", str(e))
        return _create_error_response(500, f"An unexpected error occurred: {str(e)}")


# --- Helper Functions ---


def _create_error_response(status_code: int, message: str) -> Dict[str, Any]:
    """Creates a standardized error response for API Gateway."""
    logger.error("Error Response %d: %s", status_code, message)
    return {
        "statusCode": status_code,
        "body": json.dumps({"message": message}),
    }


def _verify_snapshot_exists(ec2_client: Any, snapshot_id: str) -> None:
    """Verifies a snapshot exists, raising a ClientError if not found."""
    try:
        ec2_client.describe_snapshots(SnapshotIds=[snapshot_id])
    except ClientError as e:
        if e.response["Error"]["Code"] == "InvalidSnapshot.NotFound":
            raise ClientError(
                {
                    "Error": {
                        "Code": "404",
                        "Message": f"Snapshot '{snapshot_id}' not found.",
                    }
                },
                "DescribeSnapshots",
            )
        raise


def _create_volume(ec2_client: Any, snapshot_id: str, az: str) -> str:
    """Creates an EBS volume from a snapshot and returns the new volume ID."""
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
    return new_volume_id


def _launch_instance(ec2_client: Any, params: Dict[str, str], az: str) -> str:
    """Launches a new EC2 instance."""
    response = ec2_client.run_instances(
        ImageId=params["ami_id"],
        InstanceType=params["instance_type"],
        SubnetId=params["subnet_id"],
        Placement={"AvailabilityZone": az},
        MinCount=1,
        MaxCount=1,
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Name", "Value": "Restored by SmartVault"},
                    {"Key": "CreatedBy", "Value": "SmartVaultRestoreLambda"},
                ],
            }
        ],
    )
    new_instance_id = response["Instances"][0]["InstanceId"]
    logger.info("Successfully initiated instance launch: %s", new_instance_id)
    return new_instance_id


def _attach_volume(
    ec2_client: Any, instance_id: str, volume_id: str, device_name: str
) -> None:
    """Attaches an EBS volume to an EC2 instance."""
    ec2_client.attach_volume(
        Device=device_name, InstanceId=instance_id, VolumeId=volume_id
    )
    logger.info(
        "Successfully initiated attachment of volume %s to instance %s as %s",
        volume_id,
        instance_id,
        device_name,
    )
