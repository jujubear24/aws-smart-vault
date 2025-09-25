import unittest
from unittest.mock import patch
import os
import boto3
from moto import mock_aws
import json
from typing import Any, Dict, List

# Add the restore handler's path to our system path for import
import sys

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "restore_handler"))
)
import restore_function


@mock_aws
class TestRestoreLambda(unittest.TestCase):
    """Unit tests for the restore_function Lambda."""

    def setUp(self) -> None:
        """Set up mock AWS environment before each test."""
        self.mock_env = patch.dict(os.environ, {"AWS_REGION": "us-east-1"})
        self.mock_env.start()

        self.ec2 = boto3.client("ec2", region_name="us-east-1")

        # Create mock networking for instance launch
        self.vpc = self.ec2.create_vpc(CidrBlock="10.0.0.0/16")
        self.subnet = self.ec2.create_subnet(
            VpcId=self.vpc["Vpc"]["VpcId"], CidrBlock="10.0.1.0/24"
        )

        self.volume = self.ec2.create_volume(AvailabilityZone="us-east-1a", Size=8)
        self.volume_id = self.volume["VolumeId"]

        self.snapshot = self.ec2.create_snapshot(
            VolumeId=self.volume_id, Description="Test snapshot"
        )
        self.snapshot_id = self.snapshot["SnapshotId"]

    def tearDown(self) -> None:
        """Stop patching environment variables after each test."""
        self.mock_env.stop()

    def _create_api_event(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Helper to create a mock API Gateway event."""
        return {"body": json.dumps(body)}

    def test_successful_volume_restore_only(self) -> None:
        """Test the success path for restoring only a volume."""
        event = self._create_api_event(
            {"snapshot_id": self.snapshot_id, "availability_zone": "us-east-1a"}
        )

        response = restore_function.handler(event, {})

        self.assertEqual(response["statusCode"], 200)
        response_body = json.loads(response["body"])
        self.assertEqual(
            response_body["message"], "Volume restore initiated successfully."
        )
        self.assertNotIn(
            "instance_id", response_body
        )  # Ensure instance ID is not in response

    def test_successful_full_instance_restore(self) -> None:
        """Test the success path for restoring a volume and launching an instance."""
        event = self._create_api_event(
            {
                "snapshot_id": self.snapshot_id,
                "availability_zone": "us-east-1a",
                "launch_instance": True,
                "instance_type": "t2.micro",
                "ami_id": "ami-12345678",
                "subnet_id": self.subnet["Subnet"]["SubnetId"],
            }
        )

        response = restore_function.handler(event, {})

        self.assertEqual(response["statusCode"], 200)
        response_body = json.loads(response["body"])
        self.assertEqual(
            response_body["message"],
            "Instance launch and volume attachment initiated successfully.",
        )
        self.assertIn("vol-", response_body["volume_id"])
        self.assertIn("i-", response_body["instance_id"])

        # Verify the instance and attachment
        instance_id = response_body["instance_id"]
        volume_id = response_body["volume_id"]

        reservations = self.ec2.describe_instances(InstanceIds=[instance_id])[
            "Reservations"
        ]
        self.assertEqual(len(reservations), 1)
        instance = reservations[0]["Instances"][0]

        # FIX: Check if our restored volume ID is present in any of the attached block devices.
        attached_volume_ids = [
            mapping["Ebs"]["VolumeId"]
            for mapping in instance.get("BlockDeviceMappings", [])
        ]
        self.assertIn(volume_id, attached_volume_ids)

    def test_missing_instance_launch_params(self) -> None:
        """Test failure when launch_instance is true but params are missing."""
        event = self._create_api_event(
            {
                "snapshot_id": self.snapshot_id,
                "availability_zone": "us-east-1a",
                "launch_instance": True,
                # Missing instance_type, ami_id, subnet_id
            }
        )

        response = restore_function.handler(event, {})
        self.assertEqual(response["statusCode"], 400)
        self.assertIn(
            "Missing required parameters for instance launch",
            json.loads(response["body"])["message"],
        )

    def test_missing_snapshot_id(self) -> None:
        """Test failure when snapshot_id is missing from the payload."""
        event = self._create_api_event({"availability_zone": "us-east-1a"})
        response = restore_function.handler(event, {})

        self.assertEqual(response["statusCode"], 400)
        self.assertIn(
            "Missing required parameters", json.loads(response["body"])["message"]
        )

    def test_snapshot_not_found(self) -> None:
        """Test failure when the requested snapshot does not exist."""
        event = self._create_api_event(
            {"snapshot_id": "snap-ffffffff", "availability_zone": "us-east-1a"}
        )

        response = restore_function.handler(event, {})

        self.assertEqual(
            response["statusCode"], 500
        )  # The helper now raises a client error
        self.assertIn("not found", json.loads(response["body"])["message"])

    def test_invalid_json_body(self) -> None:
        """Test failure when the request body is not valid JSON."""
        event = {"body": '{"snapshot_id": "snap-123", }'}  # Malformed JSON
        response = restore_function.handler(event, {})

        self.assertEqual(response["statusCode"], 400)
        self.assertIn("Invalid JSON format", json.loads(response["body"])["message"])


if __name__ == "__main__":
    unittest.main()
