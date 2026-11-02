"""Linear AgentSessionEvent receiver. Deployed as Lambda index.handler.

Only signed, fresh events for the configured workspace and app enter the queue.
No payloads or credentials are logged. The ECS host needs no inbound listener.
"""
import base64
import hashlib
import hmac
import json
import os
import time
import uuid

_secret = None


def validate(event, secret, organization_id, app_user_id, now=None):
    if event.get("requestContext", {}).get("http", {}).get("method") != "POST":
        raise ValueError("method")
    raw = event.get("body", "")
    raw = base64.b64decode(raw, validate=True) if event.get("isBase64Encoded") else raw.encode()
    if len(raw) > 240_000:
        raise ValueError("size")
    headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, headers.get("linear-signature", "")):
        raise ValueError("signature")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("payload")
    timestamp = float(payload["webhookTimestamp"]) / 1000
    if not abs((time.time() if now is None else now) - timestamp) <= 60:
        raise ValueError("timestamp")
    if payload.get("organizationId") != organization_id:
        raise ValueError("identity")
    if payload.get("type") != "AgentSessionEvent" or payload.get("action") not in ("created", "prompted"):
        return None
    if payload.get("appUserId") != app_user_id:
        raise ValueError("identity")
    session = payload["agentSession"]
    if not isinstance(session, dict):
        raise ValueError("session")
    uuid.UUID(session["id"])
    if session.get("appUserId") != app_user_id or session.get("organizationId") != organization_id:
        raise ValueError("session identity")
    # Use the semantic event identity: retries may carry a new delivery ID.
    activity = payload.get("agentActivity") or {}
    if not isinstance(activity, dict):
        raise ValueError("activity")
    if payload["action"] == "prompted":
        content = activity.get("content") or {}
        if not isinstance(content, dict) or not isinstance(content.get("body", ""), str):
            raise ValueError("content")
    event_id = activity.get("id") if payload["action"] == "prompted" else session["id"] + ":created"
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("activity id")
    return {"id": event_id, "payload": payload}


def handler(event, context):
    global _secret
    headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
    if not headers.get("linear-signature"):
        return {"statusCode": 400, "body": "Invalid webhook"}
    import boto3
    from botocore.config import Config
    config = Config(connect_timeout=1, read_timeout=2, retries={"max_attempts": 0})
    try:
        if _secret is None:
            _secret = boto3.client("ssm", config=config).get_parameter(
                Name=os.environ["SIGNING_SECRET_PARAMETER"], WithDecryption=True)["Parameter"]["Value"]
        record = validate(event, _secret, os.environ["ORGANIZATION_ID"], os.environ["APP_USER_ID"])
    except (ValueError, TypeError, KeyError):
        return {"statusCode": 400, "body": "Invalid webhook"}
    except Exception:
        return {"statusCode": 503, "body": "Temporarily unavailable"}
    if record:
        try:
            boto3.client("sqs", config=config).send_message(
                QueueUrl=os.environ["QUEUE_URL"], MessageBody=json.dumps(record),
                MessageGroupId=record["payload"]["agentSession"]["id"],
                MessageDeduplicationId=hashlib.sha256(record["id"].encode()).hexdigest())
        except Exception:
            return {"statusCode": 503, "body": "Temporarily unavailable"}
    return {"statusCode": 200, "body": "Accepted"}
