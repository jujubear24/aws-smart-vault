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
    This is the ASYNCHRONOUS worker Lambda. It is triggered by the API Handler.
    It performs the long-running tasks of restoring a volume and launching an instance.
    """
    sns_client = boto3.client("sns")
    sns_topic_arn = os.environ.get("SNS_TOPIC_ARN", "not-set")

    body = event

    try:
        region = os.environ.get("AWS_REGION", "us-east-1")
        ec2_client = boto3.client("ec2", region_name=region)

        logger.info("Worker received event: %s", event)
        snapshot_id = body.get("snapshot_id")

        az = body.get("availability_zone")
        subnet_id = body.get("subnet_id")
        launch_instance = body.get("launch_instance", False)

        if launch_instance:
            if not subnet_id:
                raise ValueError("subnet_id is required when launch_instance is true.")
            logger.info("Deriving Availability Zone from subnet %s.", subnet_id)
            az = _get_az_from_subnet(ec2_client, subnet_id)
        elif not az:
            raise ValueError(
                "availability_zone is required when only restoring a volume."
            )

        if not snapshot_id:
            raise ValueError("Missing required parameter: snapshot_id")

        logger.info(
            "Attempting to restore snapshot %s into Availability Zone %s",
            snapshot_id,
            az,
        )
        _verify_snapshot_exists(ec2_client, snapshot_id)
        new_volume_id = _create_volume(ec2_client, snapshot_id, az)

        if launch_instance:
            instance_type = body.get("instance_type", "t3.micro")
            ami_id = body.get("ami_id")

            instance_params = {
                "instance_type": instance_type,
                "ami_id": ami_id,
                "subnet_id": subnet_id,
            }
            if not all([ami_id, subnet_id]):
                raise ValueError(
                    "Missing required parameters for instance launch: ami_id and subnet_id are required."
                )

            waiter = ec2_client.get_waiter("volume_available")
            waiter.wait(VolumeIds=[new_volume_id])
            new_instance_id = _launch_instance(ec2_client, instance_params, az)

            waiter = ec2_client.get_waiter("instance_running")
            waiter.wait(InstanceIds=[new_instance_id])

            device_name = body.get("device_name", "/dev/sdf")
            _attach_volume(ec2_client, new_instance_id, new_volume_id, device_name)

            success_message = f"Smart Vault Restore SUCCEEDED.\n\nSuccessfully launched instance {new_instance_id} and attached restored volume {new_volume_id} from snapshot {snapshot_id}."
            _send_sns_notification(
                sns_client,
                sns_topic_arn,
                "Smart Vault Restore SUCCEEDED",
                success_message,
            )

        else:
            success_message = f"Smart Vault Restore SUCCEEDED.\n\nSuccessfully restored volume {new_volume_id} from snapshot {snapshot_id}."
            _send_sns_notification(
                sns_client,
                sns_topic_arn,
                "Smart Vault Restore SUCCEEDED",
                success_message,
            )

        return {"status": "success"}

    except Exception as e:
        error_message = f"Smart Vault Restore FAILED.\n\nError processing request for snapshot '{body.get('snapshot_id', 'N/A')}'.\n\nReason: {str(e)}"
        logger.error(error_message, exc_info=True)
        _send_sns_notification(
            sns_client, sns_topic_arn, "Smart Vault Restore FAILED", error_message
        )
        return {"status": "failed", "reason": str(e)}


# --- Helper Functions ---
def _send_sns_notification(
    sns_client: Any, topic_arn: str, subject: str, message: str
) -> None:
    if topic_arn == "not-set":
        logger.warning("SNS_TOPIC_ARN not set. Cannot send notification.")
        return
    try:
        sns_client.publish(TopicArn=topic_arn, Subject=subject, Message=message)
    except Exception as e:
        logger.error("Failed to send SNS notification: %s", str(e))


def _get_az_from_subnet(ec2_client: Any, subnet_id: str) -> str:
    try:
        response = ec2_client.describe_subnets(SubnetIds=[subnet_id])
        if not response["Subnets"]:
            raise ValueError(f"Subnet '{subnet_id}' not found.")
        az = response["Subnets"][0]["AvailabilityZone"]
        logger.info("Found AZ '%s' for subnet '%s'", az, subnet_id)
        return az
    except ClientError as e:
        if e.response["Error"]["Code"] == "InvalidSubnetID.NotFound":
            raise ValueError(f"Subnet '{subnet_id}' not found.")
        raise


def _verify_snapshot_exists(ec2_client: Any, snapshot_id: str) -> None:
    try:
        ec2_client.describe_snapshots(SnapshotIds=[snapshot_id])
    except ClientError as e:
        if e.response["Error"]["Code"] == "InvalidSnapshot.NotFound":
            raise ValueError(f"Snapshot '{snapshot_id}' not found.")
        raise


def _create_volume(ec2_client: Any, snapshot_id: str, az: str) -> str:
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
    ec2_client.attach_volume(
        Device=device_name, InstanceId=instance_id, VolumeId=volume_id
    )
    logger.info(
        "Successfully initiated attachment of volume %s to instance %s as %s",
        volume_id,
        instance_id,
        device_name,
    )
