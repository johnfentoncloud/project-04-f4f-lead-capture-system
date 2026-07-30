import importlib.util
import json
import os
from pathlib import Path
import sys
import types
import unittest


os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ["DYNAMODB_TABLE"] = "test-leads"
os.environ["OWNER_EMAIL"] = "owner@example.com"
os.environ["SES_FROM_EMAIL"] = "sender@example.com"
os.environ["SNS_TOPIC_ARN"] = "test-topic-arn"

sys.modules.setdefault(
    "boto3",
    types.SimpleNamespace(
        resource=lambda *args, **kwargs: types.SimpleNamespace(
            Table=lambda name: object()
        ),
        client=lambda *args, **kwargs: object(),
    ),
)

MODULE_PATH = Path(__file__).parents[1] / "lambda" / "lambda_function.py"
SPEC = importlib.util.spec_from_file_location("lambda_function", MODULE_PATH)
LAMBDA = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAMBDA)


class DuplicateWrite(Exception):
    response = {
        "Error": {
            "Code": "ConditionalCheckFailedException",
        }
    }


class FakeTable:
    def __init__(self):
        self.items = {}
        self.fail = False

    def put_item(self, Item, ConditionExpression):
        if self.fail:
            raise RuntimeError("storage unavailable")
        lead_id = Item["leadId"]
        if lead_id in self.items:
            raise DuplicateWrite()
        self.items[lead_id] = Item


class FakeSES:
    def __init__(self):
        self.calls = []

    def send_email(self, **kwargs):
        self.calls.append(kwargs)


class FakeSNS:
    def __init__(self):
        self.calls = []
        self.fail = False

    def publish(self, **kwargs):
        if self.fail:
            raise RuntimeError("SNS unavailable")
        self.calls.append(kwargs)


class LeadCaptureTests(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable()
        self.ses = FakeSES()
        self.sns = FakeSNS()
        self.sheet_calls = []
        LAMBDA.TABLE = self.table
        LAMBDA.SES = self.ses
        LAMBDA.SNS = self.sns
        LAMBDA.SNS_TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]
        LAMBDA._post_to_google_sheets = self._post_to_sheets

    def _post_to_sheets(self, submission):
        self.sheet_calls.append(submission)
        return "sent"

    def _event(self, body, request_id="request-123"):
        return {
            "version": "2.0",
            "requestContext": {"requestId": request_id},
            "headers": {"origin": "https://fenton4fitness.com"},
            "body": json.dumps(body),
        }

    def _valid_body(self):
        return {
            "submissionType": "lead",
            "leadType": "adult-personal-training",
            "name": "Alex Athlete",
            "email": "alex@example.com",
            "phone": "555-0100",
        }

    def test_valid_training_lead_publishes_one_sms_and_preserves_delivery(self):
        response = LAMBDA.lambda_handler(self._event(self._valid_body()), None)
        body = json.loads(response["body"])

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(len(self.sns.calls), 1)
        self.assertEqual(len(self.table.items), 1)
        self.assertEqual(len(self.sheet_calls), 1)
        self.assertEqual(len(self.ses.calls), 2)
        self.assertEqual(body["delivery"]["smsNotification"], "sent")
        call = self.sns.calls[0]
        self.assertEqual(call["TopicArn"], LAMBDA.SNS_TOPIC_ARN)
        self.assertIn("Alex Athlete", call["Message"])
        self.assertIn("Adult training", call["Message"])
        self.assertIn("555-0100", call["Message"])
        self.assertLessEqual(len(call["Message"]), 160)
        self.assertEqual(
            call["MessageAttributes"]["AWS.SNS.SMS.SMSType"]["StringValue"],
            "Transactional",
        )

    def test_invalid_submission_publishes_no_sms(self):
        body = self._valid_body()
        body["email"] = "invalid"

        response = LAMBDA.lambda_handler(self._event(body), None)

        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(self.sns.calls, [])
        self.assertEqual(self.table.items, {})

    def test_honeypot_submission_publishes_no_sms(self):
        body = self._valid_body()
        body["website"] = "https://bot.invalid"

        response = LAMBDA.lambda_handler(self._event(body), None)

        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(self.sns.calls, [])

    def test_dynamodb_failure_publishes_no_sms(self):
        self.table.fail = True

        response = LAMBDA.lambda_handler(self._event(self._valid_body()), None)

        self.assertEqual(response["statusCode"], 500)
        self.assertEqual(self.sns.calls, [])
        self.assertEqual(self.sheet_calls, [])
        self.assertEqual(self.ses.calls, [])

    def test_duplicate_event_does_not_repeat_sms_or_other_delivery(self):
        event = self._event(self._valid_body(), request_id="stable-request-id")

        first = LAMBDA.lambda_handler(event, None)
        second = LAMBDA.lambda_handler(event, None)
        second_body = json.loads(second["body"])

        self.assertEqual(first["statusCode"], 200)
        self.assertEqual(second["statusCode"], 200)
        self.assertTrue(second_body["duplicate"])
        self.assertEqual(len(self.sns.calls), 1)
        self.assertEqual(len(self.sheet_calls), 1)
        self.assertEqual(len(self.ses.calls), 2)
        self.assertEqual(len(self.table.items), 1)

    def test_sns_failure_is_logged_without_personal_information(self):
        self.sns.fail = True

        with self.assertLogs(LAMBDA.LOGGER, level="WARNING") as logs:
            response = LAMBDA.lambda_handler(self._event(self._valid_body()), None)
        body = json.loads(response["body"])
        rendered_logs = "\n".join(logs.output)

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(body["delivery"]["smsNotification"], "failed")
        self.assertIn("service=smsNotification", rendered_logs)
        self.assertIn("errorType=RuntimeError", rendered_logs)
        self.assertNotIn("Alex Athlete", rendered_logs)
        self.assertNotIn("555-0100", rendered_logs)

    def test_non_training_submission_does_not_publish_sms(self):
        body = self._valid_body()
        body.update({
            "submissionType": "website-service-inquiry",
            "leadType": "business-website",
        })

        response = LAMBDA.lambda_handler(self._event(body), None)
        response_body = json.loads(response["body"])

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(self.sns.calls, [])
        self.assertEqual(
            response_body["delivery"]["smsNotification"],
            "not_applicable",
        )


if __name__ == "__main__":
    unittest.main()
