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

        # Create a mock volume to create a snapshot from
        self.volume = self.ec2.create_volume(AvailabilityZone="us-east-1a", Size=8)
        self.volume_id = self.volume["VolumeId"]

        # Create a mock snapshot that we can "restore"
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

    def test_successful_restore(self) -> None:
        """Test the end-to-end success path for restoring a volume."""
        event = self._create_api_event(
            {"snapshot_id": self.snapshot_id, "availability_zone": "us-east-1a"}
        )

        response = restore_function.handler(event, {})

        self.assertEqual(response["statusCode"], 200)

        response_body = json.loads(response["body"])
        self.assertEqual(
            response_body["message"], "Volume restore initiated successfully."
        )
        self.assertIn("vol-", response_body["volume_id"])

        # Verify the volume was actually created with the correct tags
        created_volumes = self.ec2.describe_volumes(
            VolumeIds=[response_body["volume_id"]]
        )["Volumes"]
        self.assertEqual(len(created_volumes), 1)

        tags = {tag["Key"]: tag["Value"] for tag in created_volumes[0]["Tags"]}
        self.assertEqual(tags["CreatedBy"], "SmartVaultRestoreLambda")
        self.assertEqual(tags["Name"], f"Restored from {self.snapshot_id}")

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
            {
                "snapshot_id": "snap-ffffffff",  # A non-existent snapshot
                "availability_zone": "us-east-1a",
            }
        )

        response = restore_function.handler(event, {})

        self.assertEqual(response["statusCode"], 404)
        self.assertIn("not found", json.loads(response["body"])["message"])

    def test_invalid_json_body(self) -> None:
        """Test failure when the request body is not valid JSON."""
        event = {
            "body": '{"snapshot_id": "snap-123", "availability_zone": }'
        }  # Malformed JSON
        response = restore_function.handler(event, {})

        self.assertEqual(response["statusCode"], 400)
        self.assertIn("Invalid JSON format", json.loads(response["body"])["message"])


if __name__ == "__main__":
    unittest.main()
