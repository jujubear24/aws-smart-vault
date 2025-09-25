import unittest
from unittest.mock import patch, MagicMock
import os
import boto3
from moto import mock_aws
import datetime
from freezegun import freeze_time
from typing import Any, Dict, List

# Import the Lambda function handler and helper functions
# We assume the lambda_function.py is in the same directory or accessible via python path.
# For this structure, we need to add the parent directory to the path.
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import lambda_function

# --- Test Class ---


@mock_aws
class TestSmartVaultLambda(unittest.TestCase):
    """
    Unit tests for the Smart Vault Lambda function.
    Uses the 'moto' library to mock AWS services.
    """

    # -------------------------------------------------------------------------
    # Setup and Teardown
    # -------------------------------------------------------------------------

    def setUp(self) -> None:
        """Set up mock AWS environment before each test."""
        # --- Environment Variables ---
        self.mock_env = patch.dict(
            os.environ,
            {
                "AWS_REGION": "us-east-1",
                "RETENTION_DAYS": "7",
                "BACKUP_TAG_KEY": "Backup-Tier",
                "BACKUP_TAG_VALUE": "Gold",
                "DR_REGION": "us-west-2",
                "SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:SmartVault-Notifications",
                "DR_KMS_KEY_ARN": "arn:aws:kms:us-west-2:123456789012:key/mock-key-id",
            },
        )
        self.mock_env.start()

        # --- Mock AWS Resources ---
        self.primary_region: str = os.environ["AWS_REGION"]
        self.dr_region: str = os.environ["DR_REGION"]

        self.ec2_primary: Any = boto3.client("ec2", region_name=self.primary_region)
        self.ec2_dr: Any = boto3.client("ec2", region_name=self.dr_region)
        self.kms_dr: Any = boto3.client("kms", region_name=self.dr_region)
        self.sns_primary: Any = boto3.client("sns", region_name=self.primary_region)

        kms_key: Dict[str, Any] = self.kms_dr.create_key(Description="Mock DR Key")
        self.kms_key_arn: str = kms_key["KeyMetadata"]["Arn"]
        os.environ["DR_KMS_KEY_ARN"] = self.kms_key_arn

        self.sns_primary.create_topic(Name="SmartVault-Notifications")

        vpc: Dict[str, Any] = self.ec2_primary.create_vpc(CidrBlock="10.0.0.0/16")
        subnet: Dict[str, Any] = self.ec2_primary.create_subnet(
            VpcId=vpc["Vpc"]["VpcId"], CidrBlock="10.0.1.0/24"
        )

        instance_response: Dict[str, Any] = self.ec2_primary.run_instances(
            ImageId="ami-12345678",
            InstanceType="t2.micro",
            MinCount=1,
            MaxCount=1,
            SubnetId=subnet["Subnet"]["SubnetId"],
        )
        self.instance_id: str = instance_response["Instances"][0]["InstanceId"]

        self.ec2_primary.create_tags(
            Resources=[self.instance_id],
            Tags=[
                {"Key": "Backup-Tier", "Value": "Gold"},
                {"Key": "Name", "Value": "TestServer"},
            ],
        )

        volumes: List[Dict[str, Any]] = self.ec2_primary.describe_volumes(
            Filters=[{"Name": "attachment.instance-id", "Values": [self.instance_id]}]
        )["Volumes"]
        self.volume_id: str = volumes[0]["VolumeId"]

    def tearDown(self) -> None:
        """Stop patching environment variables after each test."""
        self.mock_env.stop()

    # -------------------------------------------------------------------------
    # Test Cases for Helper Functions
    # -------------------------------------------------------------------------

    def test_find_instances_by_tag_success(self) -> None:
        """Test that it correctly finds an instance with the specified tag."""
        instances: List[str] = lambda_function.find_instances_by_tag(
            self.ec2_primary, "Backup-Tier", "Gold"
        )
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0], self.instance_id)

    def test_find_instances_by_tag_no_match(self) -> None:
        """Test that it returns an empty list if no instances match the tag."""
        instances: List[str] = lambda_function.find_instances_by_tag(
            self.ec2_primary, "Backup-Tier", "Silver"
        )
        self.assertEqual(len(instances), 0)

    def test_create_snapshots_success(self) -> None:
        """Test that it creates a snapshot for an instance's volume."""
        snapshot_ids: List[str] = lambda_function.create_snapshots(
            self.ec2_primary, [self.instance_id]
        )
        self.assertEqual(len(snapshot_ids), 1)

        snapshots: List[Dict[str, Any]] = self.ec2_primary.describe_snapshots(
            SnapshotIds=snapshot_ids
        )["Snapshots"]
        self.assertEqual(snapshots[0]["VolumeId"], self.volume_id)
        self.assertIn("SmartVault Backup", snapshots[0]["Description"])

    def test_copy_snapshots_to_dr_success(self) -> None:
        """Test that it successfully initiates a copy of a snapshot to the DR region."""
        source_snapshot: Dict[str, Any] = self.ec2_primary.create_snapshot(
            VolumeId=self.volume_id,
            TagSpecifications=[
                {
                    "ResourceType": "snapshot",
                    "Tags": [{"Key": "CreatedBy", "Value": "SmartVaultLambda"}],
                }
            ],
        )
        source_snapshot_id: str = source_snapshot["SnapshotId"]

        copied_ids: List[str] = lambda_function.copy_snapshots_to_dr(
            self.ec2_primary,
            self.ec2_dr,
            self.dr_region,
            [source_snapshot_id],
            self.kms_key_arn,
        )
        self.assertEqual(len(copied_ids), 1)

        # FIX: Use specific filter to find the copied snapshot
        dr_snapshots: List[Dict[str, Any]] = self.ec2_dr.describe_snapshots(
            Filters=[{"Name": "tag:CreatedBy", "Values": ["SmartVaultLambda"]}],
            OwnerIds=["self"],
        )["Snapshots"]
        self.assertEqual(len(dr_snapshots), 1)
        self.assertTrue(dr_snapshots[0]["Encrypted"])
        self.assertEqual(dr_snapshots[0]["KmsKeyId"], self.kms_key_arn)

    @freeze_time("2025-01-10 12:00:00")
    def test_cleanup_snapshots(self) -> None:
        """Test that old snapshots are deleted and new ones are kept."""
        with freeze_time("2025-01-01 12:00:00"):
            self.ec2_primary.create_snapshot(
                VolumeId=self.volume_id,
                TagSpecifications=[
                    {
                        "ResourceType": "snapshot",
                        "Tags": [{"Key": "CreatedBy", "Value": "SmartVaultLambda"}],
                    }
                ],
            )

        with freeze_time("2025-01-05 12:00:00"):
            self.ec2_primary.create_snapshot(
                VolumeId=self.volume_id,
                TagSpecifications=[
                    {
                        "ResourceType": "snapshot",
                        "Tags": [{"Key": "CreatedBy", "Value": "SmartVaultLambda"}],
                    }
                ],
            )

        lambda_function.cleanup_snapshots(self.ec2_primary, 7, "Primary Region")

        # FIX: Use specific filter to find remaining snapshots
        remaining_snapshots: List[Dict[str, Any]] = self.ec2_primary.describe_snapshots(
            Filters=[{"Name": "tag:CreatedBy", "Values": ["SmartVaultLambda"]}],
            OwnerIds=["self"],
        )["Snapshots"]
        self.assertEqual(len(remaining_snapshots), 1)
        self.assertEqual(
            remaining_snapshots[0]["StartTime"].strftime("%Y-%m-%d"), "2025-01-05"
        )

    # -------------------------------------------------------------------------
    # Test Case for the Main Lambda Handler
    # -------------------------------------------------------------------------

    def test_lambda_handler_full_success_path(self) -> None:
        """Test the entire lambda_handler for a successful execution."""
        original_boto3_client = boto3.client

        with patch("lambda_function.boto3.client") as mock_boto_client:
            mock_sns = MagicMock()

            def side_effect(service_name: str, **kwargs: Any) -> Any:
                if service_name == "sns":
                    return mock_sns
                return original_boto3_client(service_name, **kwargs)

            mock_boto_client.side_effect = side_effect

            result: Dict[str, Any] = lambda_function.lambda_handler({}, {})

            self.assertEqual(result["statusCode"], 200)

            # FIX: Use specific filter for primary snapshots
            primary_snapshots: List[Dict[str, Any]] = (
                self.ec2_primary.describe_snapshots(
                    Filters=[{"Name": "tag:CreatedBy", "Values": ["SmartVaultLambda"]}],
                    OwnerIds=["self"],
                )["Snapshots"]
            )
            self.assertEqual(len(primary_snapshots), 1)

            # FIX: Use specific filter for DR snapshots
            dr_snapshots: List[Dict[str, Any]] = self.ec2_dr.describe_snapshots(
                Filters=[{"Name": "tag:CreatedBy", "Values": ["SmartVaultLambda"]}],
                OwnerIds=["self"],
            )["Snapshots"]
            self.assertEqual(len(dr_snapshots), 1)
            self.assertTrue(dr_snapshots[0]["Encrypted"])

            mock_sns.publish.assert_called_once()
            _call_args, call_kwargs = mock_sns.publish.call_args
            self.assertIn("SUCCEEDED", call_kwargs["Subject"])


if __name__ == "__main__":
    unittest.main()
