import boto3
import os
import datetime
import logging
from typing import List, Dict, Any, Optional
from botocore.exceptions import ClientError

# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    This Lambda function automates the creation, cross-region copying, and cleanup of EBS snapshots.
    It is triggered by an Amazon EventBridge (CloudWatch Events) rule.
    """
    sns_client = boto3.client("sns")
    sns_topic_arn: str = os.environ.get("SNS_TOPIC_ARN", "not-set")

    try:
        # Get environment variables
        retention_days: int = int(os.environ["RETENTION_DAYS"])
        backup_tag_key: str = os.environ["BACKUP_TAG_KEY"]
        backup_tag_value: str = os.environ["BACKUP_TAG_VALUE"]
        dr_region: str = os.environ["DR_REGION"]
        # Get the dedicated KMS key for the DR region
        dr_kms_key_arn: str = os.environ["DR_KMS_KEY_ARN"]

        primary_region: str = os.environ["AWS_REGION"]

        ec2_primary = boto3.client("ec2", region_name=primary_region)
        ec2_dr = boto3.client("ec2", region_name=dr_region)

        instances_to_backup: List[str] = find_instances_by_tag(
            ec2_primary, backup_tag_key, backup_tag_value
        )
        if not instances_to_backup:
            logger.info(
                "No instances found with tag %s=%s. Exiting.",
                backup_tag_key,
                backup_tag_value,
            )
            return {"statusCode": 200, "body": "No instances to backup."}

        logger.info("Found instances to backup: %s", instances_to_backup)

        created_snapshots: List[str] = create_snapshots(
            ec2_primary, instances_to_backup
        )
        if not created_snapshots:
            raise Exception("Snapshot creation failed. Check previous logs.")

        logger.info(
            "Successfully created snapshots in %s: %s",
            primary_region,
            created_snapshots,
        )

        # Pass the KMS key to the copy function with enhanced error handling
        copied_snapshots: List[str] = copy_snapshots_to_dr(
            ec2_primary, ec2_dr, dr_region, created_snapshots, dr_kms_key_arn
        )
        logger.info(
            "Successfully initiated copy for snapshots to %s: %s",
            dr_region,
            copied_snapshots,
        )

        cleanup_snapshots(ec2_primary, retention_days, "Primary Region")
        cleanup_snapshots(ec2_dr, retention_days, "DR Region")

        success_message = (
            f"Smart Vault Backup SUCCEEDED at {datetime.datetime.now()}.\n\n"
        )
        success_message += (
            f"Created {len(created_snapshots)} snapshots in {primary_region}.\n"
        )
        success_message += (
            f"Initiated copy for {len(copied_snapshots)} snapshots to {dr_region}.\n"
        )
        send_sns_notification(
            sns_client, sns_topic_arn, "Smart Vault Backup SUCCEEDED", success_message
        )

        return {"statusCode": 200, "body": "Backup process completed successfully."}

    except Exception as e:
        error_message = f"Smart Vault Backup FAILED at {datetime.datetime.now()}.\n\nError: {str(e)}"
        logger.error(error_message, exc_info=True)
        send_sns_notification(
            sns_client, sns_topic_arn, "Smart Vault Backup FAILED", error_message
        )
        return {"statusCode": 500, "body": f"An error occurred: {str(e)}"}


# --- Helper Functions ---


def find_instances_by_tag(ec2_client, tag_key: str, tag_value: str) -> List[str]:
    """Find EC2 instances by tag key-value pair."""
    paginator = ec2_client.get_paginator("describe_instances")
    pages = paginator.paginate(
        Filters=[
            {"Name": f"tag:{tag_key}", "Values": [tag_value]},
            {"Name": "instance-state-name", "Values": ["running", "stopped"]},
        ]
    )
    instance_ids: List[str] = []
    for page in pages:
        for reservation in page["Reservations"]:
            for instance in reservation["Instances"]:
                instance_ids.append(instance["InstanceId"])
    return instance_ids


def create_snapshots(ec2_client, instance_ids: List[str]) -> List[str]:
    """Create snapshots for all volumes attached to the given instances."""
    if not instance_ids:
        return []

    created_snapshot_ids: List[str] = []
    for instance_id in instance_ids:
        try:
            volumes = ec2_client.describe_volumes(
                Filters=[{"Name": "attachment.instance-id", "Values": [instance_id]}]
            )["Volumes"]

            if not volumes:
                logger.warning("No volumes found for instance %s", instance_id)
                continue

            for vol in volumes:
                vol_id = vol["VolumeId"]
                response = ec2_client.create_snapshot(
                    VolumeId=vol_id,
                    Description=f"SmartVault Backup for {vol_id} from {instance_id}",
                    TagSpecifications=[
                        {
                            "ResourceType": "snapshot",
                            "Tags": [
                                {"Key": "CreatedBy", "Value": "SmartVaultLambda"},
                                {"Key": "SourceInstance", "Value": instance_id},
                                {
                                    "Key": "BackupDate",
                                    "Value": datetime.datetime.now().strftime(
                                        "%Y-%m-%d"
                                    ),
                                },
                            ],
                        }
                    ],
                )
                created_snapshot_ids.append(response["SnapshotId"])
                logger.info(
                    f"Created snapshot {response['SnapshotId']} for volume {vol_id}"
                )

        except ClientError as e:
            logger.error(
                f"Failed to create snapshot for instance {instance_id}: {str(e)}"
            )
            continue

    return created_snapshot_ids


def copy_snapshots_to_dr(
    ec2_primary, ec2_dr, dr_region: str, snapshot_ids: List[str], dr_kms_key_arn: str
) -> List[str]:
    """Copy snapshots to DR region with enhanced error handling and validation."""
    if not snapshot_ids:
        return []

    # Validate KMS key exists and is accessible
    try:
        kms_dr = boto3.client("kms", region_name=dr_region)
        kms_dr.describe_key(KeyId=dr_kms_key_arn)
        logger.info(f"Validated KMS key access: {dr_kms_key_arn}")
    except ClientError as e:
        logger.error(f"KMS key validation failed: {str(e)}")
        raise Exception(
            f"Cannot access KMS key {dr_kms_key_arn} in region {dr_region}: {str(e)}"
        )

    # Wait for snapshots to complete
    logger.info("Waiting for snapshots to complete before copying...")
    waiter = ec2_primary.get_waiter("snapshot_completed")

    try:
        waiter.wait(
            SnapshotIds=snapshot_ids, WaiterConfig={"Delay": 15, "MaxAttempts": 40}
        )
        logger.info("All snapshots completed successfully")
    except Exception as e:
        logger.error(f"Timeout waiting for snapshots to complete: {str(e)}")
        raise Exception(f"Snapshots did not complete within expected time: {str(e)}")

    copied_snapshot_ids: List[str] = []

    for snap_id in snapshot_ids:
        try:
            logger.info(
                "Attempting to copy snapshot %s to region %s using KMS key %s",
                snap_id,
                dr_region,
                dr_kms_key_arn,
            )

            # Get source snapshot details
            source_snapshot = ec2_primary.describe_snapshots(SnapshotIds=[snap_id])[
                "Snapshots"
            ][0]

            instance_id = "UnknownInstance"
            for tag in source_snapshot.get("Tags", []):
                if tag["Key"] == "SourceInstance":
                    instance_id = tag["Value"]
                    break

            # Perform the copy operation
            copy_response = ec2_dr.copy_snapshot(
                SourceRegion=ec2_primary.meta.region_name,
                SourceSnapshotId=snap_id,
                Description=f"DR Copy of {snap_id} from {instance_id}",
                Encrypted=True,
                KmsKeyId=dr_kms_key_arn,  # Use the dedicated DR KMS key
                TagSpecifications=[
                    {
                        "ResourceType": "snapshot",
                        "Tags": [
                            {"Key": "Name", "Value": f"SmartVault-DR-{instance_id}"},
                            {"Key": "CreatedBy", "Value": "SmartVaultLambda"},
                            {"Key": "SourceSnapshot", "Value": snap_id},
                            {
                                "Key": "SourceRegion",
                                "Value": ec2_primary.meta.region_name,
                            },
                            {
                                "Key": "BackupDate",
                                "Value": datetime.datetime.now().strftime("%Y-%m-%d"),
                            },
                        ],
                    }
                ],
            )

            copied_snapshot_id = copy_response["SnapshotId"]
            copied_snapshot_ids.append(copied_snapshot_id)

            logger.info(
                f"Successfully initiated copy: {snap_id} -> {copied_snapshot_id}"
            )

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_message = e.response.get("Error", {}).get("Message", str(e))

            logger.error(
                f"Failed to copy snapshot {snap_id} to {dr_region}. "
                f"Error Code: {error_code}, Message: {error_message}"
            )

            # Log specific common errors
            if error_code == "InvalidParameter":
                logger.error(
                    "This might be a KMS key permission issue or invalid parameter"
                )
            elif error_code == "AccessDenied":
                logger.error("Access denied - check IAM permissions and KMS key policy")
            elif error_code == "KMSKeyNotAccessibleFault":
                logger.error(
                    "KMS key is not accessible - check key policy and permissions"
                )

            # Continue with other snapshots instead of failing completely
            continue

        except Exception as e:
            logger.error(f"Unexpected error copying snapshot {snap_id}: {str(e)}")
            continue

    if not copied_snapshot_ids:
        raise Exception("Failed to copy any snapshots to DR region")

    return copied_snapshot_ids


def cleanup_snapshots(ec2_client, retention_days: int, region_name: str) -> None:
    """Clean up old snapshots based on retention policy."""
    logger.info("Starting cleanup of old snapshots in %s.", region_name)
    retention_date = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=retention_days
    )

    paginator = ec2_client.get_paginator("describe_snapshots")
    pages = paginator.paginate(
        Filters=[{"Name": "tag:CreatedBy", "Values": ["SmartVaultLambda"]}],
        OwnerIds=["self"],
    )

    snapshots_deleted = 0
    for page in pages:
        for snapshot in page["Snapshots"]:
            if snapshot["StartTime"] < retention_date:
                try:
                    logger.info(
                        "Deleting snapshot %s created on %s",
                        snapshot["SnapshotId"],
                        snapshot["StartTime"],
                    )
                    ec2_client.delete_snapshot(SnapshotId=snapshot["SnapshotId"])
                    snapshots_deleted += 1
                except ClientError as e:
                    logger.error(
                        "Could not delete snapshot %s: %s",
                        snapshot["SnapshotId"],
                        str(e),
                    )

    logger.info("Deleted %d snapshots from %s.", snapshots_deleted, region_name)


def send_sns_notification(
    sns_client, topic_arn: str, subject: str, message: str
) -> None:
    """Send SNS notification."""
    if topic_arn == "not-set":
        logger.error(
            "SNS_TOPIC_ARN environment variable not set. Cannot send notification."
        )
        return
    try:
        sns_client.publish(TopicArn=topic_arn, Subject=subject, Message=message)
        logger.info("SNS notification sent successfully")
    except ClientError as e:
        logger.error("Failed to send SNS notification: %s", str(e))
