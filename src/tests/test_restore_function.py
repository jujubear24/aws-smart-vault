import unittest
from unittest.mock import patch, MagicMock
import os
import boto3
from moto import mock_aws
import json
from typing import Any, Dict, List

# Add the source directories to our system path for imports
import sys

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "restore_handler"))
)
sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "restore_api_handler")
    ),
)

# Now we can import both handlers
import restore_function
import handler as api_handler


@mock_aws
class TestRestoreLambdas(unittest.TestCase):
    """Unit tests for the entire restore workflow (API Handler and Worker)."""

    def setUp(self) -> None:
        """Set up mock AWS environment before each test."""
        self.mock_env = patch.dict(
            os.environ,
            {
                "AWS_REGION": "us-east-1",
                "SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:SmartVault-Notifications",
            },
        )
        self.mock_env.start()

        self.ec2 = boto3.client("ec2", region_name="us-east-1")
        self.sns = boto3.client("sns", region_name="us-east-1")

        self.sns.create_topic(Name="SmartVault-Notifications")
        self.vpc = self.ec2.create_vpc(CidrBlock="10.0.0.0/16")
        self.subnet = self.ec2.create_subnet(
            VpcId=self.vpc["Vpc"]["VpcId"],
            CidrBlock="10.0.1.0/24",
            AvailabilityZone="us-east-1a",
        )
        self.subnet_id = self.subnet["Subnet"]["SubnetId"]
        self.volume = self.ec2.create_volume(AvailabilityZone="us-east-1a", Size=8)
        self.snapshot = self.ec2.create_snapshot(VolumeId=self.volume["VolumeId"])
        self.snapshot_id = self.snapshot["SnapshotId"]

    def tearDown(self) -> None:
        self.mock_env.stop()

    def _create_api_event(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return {"body": json.dumps(body)}

    # --- API Handler Tests ---

    def test_api_handler_success(self) -> None:
        """Test the API handler successfully invokes the worker."""
        worker_arn = "arn:aws:lambda:us-east-1:123456789012:function:worker"
        with patch.dict(os.environ, {"WORKER_LAMBDA_ARN": worker_arn}):
            with patch("boto3.client") as mock_boto_client:
                mock_lambda = MagicMock()
                mock_boto_client.return_value = mock_lambda

                payload = {"snapshot_id": self.snapshot_id}
                event = self._create_api_event(payload)

                response = api_handler.lambda_handler(event, {})

                self.assertEqual(response["statusCode"], 202)
                mock_lambda.invoke.assert_called_once_with(
                    FunctionName=worker_arn,
                    InvocationType="Event",
                    Payload=json.dumps(payload),
                )

    def test_api_handler_invalid_json(self) -> None:
        """Test API handler fails with malformed JSON."""
        # FIX: Add the required environment variable patch to this test.
        # This allows the handler to run without a KeyError, so it can correctly
        # find and handle the json.JSONDecodeError.
        worker_arn = "arn:aws:lambda:us-east-1:123456789012:function:worker"
        with patch.dict(os.environ, {"WORKER_LAMBDA_ARN": worker_arn}):
            with patch(
                "boto3.client"
            ):  # Still need to mock boto so it doesn't try a real call
                event = {"body": '{"key": "value",}'}  # Malformed
                response = api_handler.lambda_handler(event, {})
                self.assertEqual(response["statusCode"], 400)

    # --- Worker Function Tests ---

    def test_worker_successful_full_restore(self) -> None:
        """Test the worker successfully restores an instance and volume."""
        event = {
            "snapshot_id": self.snapshot_id,
            "launch_instance": True,
            "instance_type": "t2.micro",
            "ami_id": "ami-12345678",
            "subnet_id": self.subnet_id,
        }

        original_boto3_client = boto3.client
        with patch("restore_function.boto3.client") as mock_boto_client:
            mock_sns_client = MagicMock()

            def side_effect(service_name: str, **kwargs: Any) -> Any:
                if service_name == "sns":
                    return mock_sns_client
                return original_boto3_client(service_name, **kwargs)

            mock_boto_client.side_effect = side_effect

            result = restore_function.handler(event, {})
            self.assertEqual(result["status"], "success")

            mock_sns_client.publish.assert_called_once()
            call_kwargs = mock_sns_client.publish.call_args.kwargs
            self.assertIn("SUCCEEDED", call_kwargs["Subject"])
            self.assertIn("Successfully launched instance", call_kwargs["Message"])

    def test_worker_fails_on_missing_params(self) -> None:
        """Test the worker fails gracefully and sends a notification."""
        event = {
            "snapshot_id": self.snapshot_id,
            "launch_instance": True,
        }  # Missing subnet_id

        original_boto3_client = boto3.client
        with patch("restore_function.boto3.client") as mock_boto_client:
            mock_sns_client = MagicMock()

            def side_effect(service_name: str, **kwargs: Any) -> Any:
                if service_name == "sns":
                    return mock_sns_client
                return original_boto3_client(service_name, **kwargs)

            mock_boto_client.side_effect = side_effect

            result = restore_function.handler(event, {})
            self.assertEqual(result["status"], "failed")

            mock_sns_client.publish.assert_called_once()
            call_kwargs = mock_sns_client.publish.call_args.kwargs
            self.assertIn("FAILED", call_kwargs["Subject"])
            self.assertIn("subnet_id is required", call_kwargs["Message"])


if __name__ == "__main__":
    unittest.main()
