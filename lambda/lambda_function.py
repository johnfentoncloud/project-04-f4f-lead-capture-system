import base64
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3


LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
DYNAMODB_TABLE = os.environ["DYNAMODB_TABLE"]
GOOGLE_SHEETS_WEBHOOK_URL = os.environ.get("GOOGLE_SHEETS_WEBHOOK_URL", "")
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "")
SES_FROM_EMAIL = os.environ.get("SES_FROM_EMAIL", "")
ALLOWED_ORIGINS = {
    origin.strip()
    for origin in os.environ.get("ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
}

TABLE = boto3.resource("dynamodb", region_name=AWS_REGION).Table(DYNAMODB_TABLE)
SES = boto3.client("ses", region_name=AWS_REGION)

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SUPPORTED_FIELDS = {
    "submissionType",
    "leadType",
    "clientType",
    "name",
    "email",
    "phone",
    "message",
    "coachPreference",
    "coachRelationship",
    "athleteName",
    "athleteAge",
    "primarySport",
    "sport",
    "team",
    "programType",
    "preferredTraining",
    "experienceLevel",
    "availability",
    "parentName",
    "parentEmail",
    "parentPhone",
    "athleteGoals",
    "injuryHistory",
    "trainingHistory",
    "otherInterests",
    "businessName",
    "currentWebsite",
    "businessType",
    "businessOffer",
    "requestedHelp",
    "expectedPages",
    "pageCount",
    "siteStatus",
    "projectType",
    "contactFormNeeded",
    "leadFormNeeded",
    "automationInterest",
    "automation",
    "launchTimeframe",
    "budgetRange",
    "budget",
    "referralSource",
    "permissionToPublish",
    "permissionToUseFullName",
    "permissionToUsePhoto",
    "consentToContact",
    "relationship",
    "program",
    "recommend",
    "businessOffer",
    "additionalInformation",
    "additionalDetails",
    "helpNeeded",
    "contactConsent",
}


def _origin_for(event):
    headers = event.get("headers") or {}
    origin = headers.get("origin") or headers.get("Origin") or ""
    if origin in ALLOWED_ORIGINS:
        return origin
    if origin.startswith("http://localhost:") or origin.startswith("http://127.0.0.1:"):
        return origin
    return next(iter(ALLOWED_ORIGINS), "")


def _response(event, status_code, body):
    origin = _origin_for(event)
    headers = {
        "Content-Type": "application/json",
        "Access-Control-Allow-Methods": "POST,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Vary": "Origin",
    }
    if origin:
        headers["Access-Control-Allow-Origin"] = origin
    return {
        "statusCode": status_code,
        "headers": headers,
        "body": json.dumps(body, default=str),
    }


def _parse_body(event):
    if not isinstance(event, dict):
        raise ValueError("Request event must be an object.")

    if "body" not in event:
        return event

    body = event.get("body")
    if event.get("isBase64Encoded") and isinstance(body, str):
        body = base64.b64decode(body).decode("utf-8")

    if isinstance(body, str):
        if not body.strip():
            raise ValueError("Request body is empty.")
        body = json.loads(body, parse_float=Decimal)

    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object.")
    return body


def _clean_value(value):
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {str(key): _clean_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean_value(item) for item in value]
    if value is None:
        return ""
    return value


def _normalized_submission(body):
    submission = {
        key: _clean_value(body[key])
        for key in SUPPORTED_FIELDS
        if key in body
    }

    submission_type = str(submission.get("submissionType") or "lead")
    lead_type = str(submission.get("leadType") or "general-inquiry")
    customer_email = str(
        submission.get("email") or submission.get("parentEmail") or ""
    ).strip()
    customer_name = str(
        submission.get("name") or submission.get("parentName") or "there"
    ).strip()

    if not customer_email or not EMAIL_PATTERN.match(customer_email):
        raise ValueError("A valid email address is required.")

    now = datetime.now(timezone.utc).isoformat()
    submission.update(
        {
            "leadId": str(uuid.uuid4()),
            "submittedAt": str(submission.get("submittedAt") or now),
            "submissionType": submission_type,
            "leadType": lead_type,
            "email": customer_email,
            "name": customer_name,
        }
    )

    # Preserve the original Lambda/Sheets field contract.
    submission.setdefault("athleteName", submission.get("name", ""))
    submission.setdefault("athleteAge", "")
    submission.setdefault("primarySport", submission.get("sport", ""))
    submission.setdefault("parentName", submission.get("name", ""))
    submission.setdefault("parentEmail", customer_email)
    submission.setdefault("parentPhone", submission.get("phone", ""))
    submission.setdefault("athleteGoals", submission.get("message", ""))
    submission.setdefault("injuryHistory", "Not provided")
    submission.setdefault("trainingHistory", submission.get("experienceLevel", ""))
    submission.setdefault("otherInterests", submission.get("additionalDetails", ""))
    return submission


def _post_to_google_sheets(submission):
    if not GOOGLE_SHEETS_WEBHOOK_URL:
        return "not_configured"
    last_error = None
    for attempt in range(2):
        request = urllib.request.Request(
            GOOGLE_SHEETS_WEBHOOK_URL,
            data=json.dumps(submission, default=str).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError(
                        f"Google Sheets returned HTTP {response.status}"
                    )
            return "sent"
        except (TimeoutError, urllib.error.URLError, RuntimeError) as error:
            last_error = error
            if attempt == 0:
                time.sleep(0.5)
    raise last_error


def _confirmation_copy(lead_type):
    messages = {
        "youth-athlete": "We received your youth athlete inquiry.",
        "adult-personal-training": "We received your adult personal-training inquiry.",
        "team-training": "We received your team-training inquiry.",
        "general-inquiry": "We received your general inquiry.",
        "business-website": "We received your business website inquiry.",
        "testimonial": "We received your testimonial for private review. It will never be published automatically.",
    }
    return messages.get(lead_type, messages["general-inquiry"])


def _send_owner_notification(submission):
    if not OWNER_EMAIL or not SES_FROM_EMAIL:
        return "not_configured"

    summary_fields = (
        "leadId",
        "submittedAt",
        "submissionType",
        "leadType",
        "name",
        "email",
        "phone",
        "athleteName",
        "primarySport",
        "team",
        "businessName",
        "message",
    )
    summary = "\n".join(
        f"{key}: {submission.get(key, '')}" for key in summary_fields
    )
    SES.send_email(
        Source=SES_FROM_EMAIL,
        Destination={"ToAddresses": [OWNER_EMAIL]},
        Message={
            "Subject": {
                "Data": f"New Fenton4Fitness submission: {submission['leadType']}"
            },
            "Body": {"Text": {"Data": summary}},
        },
    )
    return "sent"


def _send_customer_confirmation(submission):
    if not SES_FROM_EMAIL:
        return "not_configured"

    recipient = submission["email"]
    message = (
        f"Hi {submission['name']},\n\n"
        f"{_confirmation_copy(submission['leadType'])}\n\n"
        "Thanks for contacting Fenton4Fitness. John or Jess will review your "
        "information and follow up as soon as possible.\n\n"
        "Fenton4Fitness"
    )
    SES.send_email(
        Source=SES_FROM_EMAIL,
        Destination={"ToAddresses": [recipient]},
        Message={
            "Subject": {"Data": "We received your Fenton4Fitness submission"},
            "Body": {"Text": {"Data": message}},
        },
    )
    return "sent"


def _attempt_delivery(name, operation, results):
    try:
        results[name] = operation()
    except Exception as error:
        results[name] = "failed"
        LOGGER.warning(
            "Downstream delivery failed: service=%s errorType=%s",
            name,
            type(error).__name__,
        )


def lambda_handler(event, context):
    if isinstance(event, dict) and event.get("httpMethod") == "OPTIONS":
        return _response(event, 204, {})

    try:
        body = _parse_body(event)
        submission = _normalized_submission(body)
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        return _response(event if isinstance(event, dict) else {}, 400, {
            "ok": False,
            "message": str(error),
        })

    try:
        TABLE.put_item(Item=submission)
    except Exception as error:
        LOGGER.error("DynamoDB persistence failed: errorType=%s", type(error).__name__)
        return _response(event, 500, {
            "ok": False,
            "message": "The submission could not be stored.",
        })

    delivery = {}
    _attempt_delivery(
        "googleSheets",
        lambda: _post_to_google_sheets(submission),
        delivery,
    )
    _attempt_delivery(
        "ownerNotification",
        lambda: _send_owner_notification(submission),
        delivery,
    )
    _attempt_delivery(
        "customerConfirmation",
        lambda: _send_customer_confirmation(submission),
        delivery,
    )

    LOGGER.info(
        "Submission processed: leadId=%s submissionType=%s leadType=%s delivery=%s",
        submission["leadId"],
        submission["submissionType"],
        submission["leadType"],
        delivery,
    )
    return _response(event, 200, {
        "ok": True,
        "message": "Submission received.",
        "leadId": submission["leadId"],
        "submissionType": submission["submissionType"],
        "leadType": submission["leadType"],
        "delivery": delivery,
    })
