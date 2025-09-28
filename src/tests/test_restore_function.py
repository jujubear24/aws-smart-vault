import unittest
from unittest.mock import patch, MagicMock
import os
import boto3
from moto import mock_aws
import json
from typing import Any, Dict

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

        # Create mock clients
        self.ec2 = boto3.client("ec2", region_name="us-east-1")
        self.sns = boto3.client("sns", region_name="us-east-1")
        self.iam = boto3.client("iam", region_name="us-east-1")
        self.lambda_client = boto3.client("lambda", region_name="us-east-1")

        # Create common mock resources
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

        # Create a mock IAM role for the worker lambda
        role = self.iam.create_role(
            RoleName="mock-role",
            AssumeRolePolicyDocument=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "lambda.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                }
            ),
        )
        self.mock_role_arn = role["Role"]["Arn"]

        # Create a mock worker Lambda function for the handler to find and invoke
        self.worker_function = self.lambda_client.create_function(
            FunctionName="mock-worker",
            Runtime="python3.9",
            Role=self.mock_role_arn,
            Handler="lambda_function.lambda_handler",
            Code={"ZipFile": b"bytes"},
        )
        self.worker_arn = self.worker_function["FunctionArn"]

    def tearDown(self) -> None:
        self.mock_env.stop()

    def _create_api_event(
        self, body: Dict[str, Any] = None, body_str: str = None
    ) -> Dict[str, Any]:
        if body is not None:
            return {"body": json.dumps(body)}
        if body_str is not None:
            return {"body": body_str}
        return {"body": None}

    # --- API Handler Tests ---

    def test_api_handler_success(self) -> None:
        """Test the API handler successfully invokes the worker."""
        with patch.dict(os.environ, {"WORKER_LAMBDA_ARN": self.worker_arn}):
            original_boto3_client = boto3.client
            with patch("boto3.client") as mock_boto_client:
                mock_lambda = MagicMock()

                def side_effect(service_name: str, **kwargs: Any) -> Any:
                    if service_name == "lambda":
                        mock_lambda.get_function.return_value = {
                            "Configuration": self.worker_function
                        }
                        mock_lambda.invoke.return_value = {"StatusCode": 202}
                        return mock_lambda
                    return original_boto3_client(service_name, **kwargs)

                mock_boto_client.side_effect = side_effect

                payload = {"snapshot_id": self.snapshot_id}
                event = self._create_api_event(body=payload)

                response = api_handler.lambda_handler(event, {})

                self.assertEqual(response["statusCode"], 202)
                mock_lambda.invoke.assert_called_once_with(
                    FunctionName=self.worker_arn,
                    InvocationType="Event",
                    Payload=json.dumps(payload),
                )

    def test_api_handler_no_worker_arn(self) -> None:
        """Test failure when WORKER_LAMBDA_ARN is not set."""
        if "WORKER_LAMBDA_ARN" in os.environ:
            del os.environ["WORKER_LAMBDA_ARN"]

        response = api_handler.lambda_handler(
            self._create_api_event(body={"snapshot_id": "snap-123"}), {}
        )
        self.assertEqual(response["statusCode"], 500)
        # FIX: Assert against the correct error message from the robust handler
        self.assertIn(
            "WORKER_LAMBDA_ARN not set", json.loads(response["body"])["message"]
        )

    def test_api_handler_no_body(self) -> None:
        """Test failure when the request has no body."""
        with patch.dict(os.environ, {"WORKER_LAMBDA_ARN": self.worker_arn}):
            response = api_handler.lambda_handler({"body": None}, {})
            self.assertEqual(response["statusCode"], 400)
            self.assertIn(
                "Request body is required", json.loads(response["body"])["message"]
            )

    def test_api_handler_invalid_json(self) -> None:
        """Test API handler fails with malformed JSON."""
        with patch.dict(os.environ, {"WORKER_LAMBDA_ARN": self.worker_arn}):
            event = self._create_api_event(body_str='{"key": "value",}')
            response = api_handler.lambda_handler(event, {})
            self.assertEqual(response["statusCode"], 400)
            self.assertIn(
                "Invalid JSON format", json.loads(response["body"])["message"]
            )

    def test_api_handler_missing_snapshot_id(self) -> None:
        """Test API handler fails when snapshot_id is missing from payload."""
        with patch.dict(os.environ, {"WORKER_LAMBDA_ARN": self.worker_arn}):
            event = self._create_api_event(body={})
            response = api_handler.lambda_handler(event, {})
            self.assertEqual(response["statusCode"], 400)
            # FIX: Assert against the correct, cleaner error message string
            self.assertIn(
                "Missing required field: snapshot_id",
                json.loads(response["body"])["message"],
            )

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
        event = {"snapshot_id": self.snapshot_id, "launch_instance": True}

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
