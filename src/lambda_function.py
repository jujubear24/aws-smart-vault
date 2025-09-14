import os
import boto3
import datetime
import logging

# =================================================================================
# Setup Logging
# =================================================================================
# It's a best practice to use the logging module for clear and structured logs.
logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    """
    Main handler for the Lambda function.
    Triggered by EventBridge, this function manages EBS snapshot creation,
    cross-region copying, and cleanup of old snapshots for tagged EC2 instances.
    """
    logger.info("Smart Vault backup job started.")

    # =================================================================================
    # Read Configuration from Environment Variables
    # =================================================================================
    # This makes our function configurable without changing code.
    try:
        retention_days = int(os.environ["RETENTION_DAYS"])
        backup_tag_key = os.environ["BACKUP_TAG_KEY"]
        backup_tag_value = os.environ["BACKUP_TAG_VALUE"]
        dr_region = os.environ["DR_REGION"]
        sns_topic_arn = os.environ["SNS_TOPIC_ARN"]
        account_id = context.invoked_function_arn.split(":")[4]
        primary_region = os.environ["AWS_REGION"]
    except KeyError as e:
        logger.error(f"Missing environment variable: {e}")
        # Send a failure notification
        send_sns_notification(
            sns_topic_arn,
            "Smart Vault FAILED",
            f"Configuration error: Missing environment variable {e}",
        )
        return {
            "statusCode": 500,
            "body": f"Configuration error: Missing environment variable {e}",
        }

    # Initialize AWS clients
    ec2_client = boto3.client("ec2")
    sns_client = boto3.client("sns")

    success_messages = []
    failure_messages = []

    try:
        # =================================================================================
        # 1. Find Instances and Create Snapshots
        # =================================================================================
        instances_to_backup = find_instances_by_tag(
            ec2_client, backup_tag_key, backup_tag_value
        )
        logger.info(f"Found {len(instances_to_backup)} instances to back up.")

        for instance in instances_to_backup:
            instance_id = instance["InstanceId"]
            logger.info(f"Processing instance: {instance_id}")

            for device in instance["BlockDeviceMappings"]:
                if "Ebs" in device:
                    volume_id = device["Ebs"]["VolumeId"]
                    logger.info(
                        f"Creating snapshot for volume {volume_id} on instance {instance_id}"
                    )

                    description = f"SmartVault Backup for {instance_id} ({volume_id}) on {datetime.date.today()}"

                    # Create the snapshot
                    snapshot = ec2_client.create_snapshot(
                        VolumeId=volume_id,
                        Description=description,
                        TagSpecifications=[
                            {
                                "ResourceType": "snapshot",
                                "Tags": [
                                    {
                                        "Key": "Name",
                                        "Value": f"SmartVault-{instance_id}",
                                    },
                                    {"Key": "CreatedBy", "Value": "SmartVaultLambda"},
                                    {"Key": "InstanceId", "Value": instance_id},
                                    {"Key": "VolumeId", "Value": volume_id},
                                ],
                            }
                        ],
                    )
                    snapshot_id = snapshot["SnapshotId"]
                    success_messages.append(
                        f"Successfully created snapshot {snapshot_id} for volume {volume_id}."
                    )

                    # =================================================================================
                    # 2. Copy Snapshot to Disaster Recovery Region
                    # =================================================================================
                    logger.info(
                        f"Copying snapshot {snapshot_id} to DR region {dr_region}"
                    )
                    ec2_dr_client = boto3.client("ec2", region_name=dr_region)
                    ec2_dr_client.copy_snapshot(
                        SourceRegion=primary_region,
                        SourceSnapshotId=snapshot_id,
                        Description=f"DR Copy of {snapshot_id}",
                        Encrypted=True,
                        TagSpecifications=[
                            {
                                "ResourceType": "snapshot",
                                "Tags": [
                                    {
                                        "Key": "Name",
                                        "Value": f"SmartVault-DR-{instance_id}",
                                    },
                                    {"Key": "CreatedBy", "Value": "SmartVaultLambda"},
                                ],
                            }
                        ],
                    )
                    success_messages.append(
                        f"Successfully started copy of {snapshot_id} to {dr_region}."
                    )

        # =================================================================================
        # 3. Clean Up Old Snapshots in Both Regions
        # =================================================================================
        cleanup_snapshots(
            ec2_client,
            retention_days,
            account_id,
            "primary region",
            success_messages,
            failure_messages,
        )
        cleanup_snapshots(
            ec2_dr_client,
            retention_days,
            account_id,
            f"DR region ({dr_region})",
            success_messages,
            failure_messages,
        )

    except Exception as e:
        logger.error(f"An unexpected error occurred: {str(e)}")
        failure_messages.append(
            f"An unexpected error occurred during the backup process: {str(e)}"
        )

    # =================================================================================
    # 4. Send Final Report via SNS
    # =================================================================================
    if failure_messages:
        subject = f"Smart Vault Backup FAILED for Account {account_id}"
        message = "Smart Vault Backup Job finished with errors:\n\n"
        message += "Failures:\n" + "\n".join(failure_messages) + "\n\n"
        if success_messages:
            message += "Successes:\n" + "\n".join(success_messages)
    else:
        subject = f"Smart Vault Backup SUCCEEDED for Account {account_id}"
        message = "Smart Vault Backup Job finished successfully:\n\n"
        message += "\n".join(success_messages)

    send_sns_notification(sns_client, sns_topic_arn, subject, message)
    logger.info("Smart Vault backup job finished.")

    return {"statusCode": 200, "body": "Backup job completed."}


def find_instances_by_tag(ec2_client, tag_key, tag_value):
    """Finds all EC2 instances that have a specific tag."""
    response = ec2_client.describe_instances(
        Filters=[
            {"Name": f"tag:{tag_key}", "Values": [tag_value]},
            {"Name": "instance-state-name", "Values": ["running", "stopped"]},
        ]
    )
    instances = []
    for reservation in response["Reservations"]:
        instances.extend(reservation["Instances"])
    return instances


def cleanup_snapshots(
    ec2_client, retention_days, account_id, region_name, success_list, failure_list
):
    """Finds and deletes snapshots older than the retention period."""
    logger.info(f"Starting snapshot cleanup in {region_name}.")
    try:
        snapshots = ec2_client.describe_snapshots(
            OwnerIds=[account_id],
            Filters=[{"Name": "tag:CreatedBy", "Values": ["SmartVaultLambda"]}],
        )

        retention_date = datetime.datetime.now(
            datetime.timezone.utc
        ) - datetime.timedelta(days=retention_days)

        for snapshot in snapshots["Snapshots"]:
            snapshot_id = snapshot["SnapshotId"]
            start_time = snapshot["StartTime"]

            if start_time < retention_date:
                logger.info(
                    f"Deleting old snapshot {snapshot_id} from {region_name} (created on {start_time})."
                )
                try:
                    ec2_client.delete_snapshot(SnapshotId=snapshot_id)
                    success_list.append(
                        f"Successfully deleted old snapshot {snapshot_id} from {region_name}."
                    )
                except Exception as e:
                    error_msg = f"Could not delete snapshot {snapshot_id} in {region_name}: {str(e)}"
                    logger.error(error_msg)
                    failure_list.append(error_msg)

    except Exception as e:
        error_msg = f"Error during snapshot cleanup in {region_name}: {str(e)}"
        logger.error(error_msg)
        failure_list.append(error_msg)


def send_sns_notification(sns_client, topic_arn, subject, message):
    """Sends a notification message to an SNS topic."""
    try:
        sns_client.publish(TopicArn=topic_arn, Subject=subject, Message=message)
        logger.info(f"Successfully sent notification to SNS topic {topic_arn}.")
    except Exception as e:
        logger.error(f"Failed to send SNS notification: {str(e)}")
