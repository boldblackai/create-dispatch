import base64
import copy
import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


receiver = load("linear_receiver", "webhook.py")
inbox = load("linear_inbox", "plugin/inbox.py")
SESSION = "7cb2abcc-211b-4eeb-8660-36dd5f903f52"
PAYLOAD = {"type": "AgentSessionEvent", "action": "created", "webhookTimestamp": 1000000,
           "organizationId": "org", "appUserId": "app",
           "agentSession": {"id": SESSION, "organizationId": "org", "appUserId": "app"}}


def request(payload=None):
    body = json.dumps(PAYLOAD if payload is None else payload)
    return {"requestContext": {"http": {"method": "POST"}}, "body": body,
            "headers": {"Linear-Signature": hmac.new(b"secret", body.encode(), hashlib.sha256).hexdigest()}}


class WebhookTests(unittest.TestCase):
    def validate(self, event, **kwargs):
        return receiver.validate(event, "secret", "org", "app", now=1000, **kwargs)

    def test_signed_created_event_has_stable_identity(self):
        self.assertEqual(self.validate(request())["id"], SESSION + ":created")

    def test_base64_body(self):
        event = request()
        event["body"] = base64.b64encode(event["body"].encode()).decode()
        event["isBase64Encoded"] = True
        self.assertEqual(self.validate(event)["payload"], PAYLOAD)

    def test_tampered_body_rejected(self):
        event = request()
        event["body"] += " "
        with self.assertRaises(ValueError):
            self.validate(event)

    def test_missing_signature_rejected(self):
        event = request()
        event["headers"] = {}
        with self.assertRaises(ValueError):
            self.validate(event)

    def test_stale_future_and_nonfinite_timestamps_rejected(self):
        for timestamp in (938999, 1061001, float("nan"), float("inf")):
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                self.validate(request({**PAYLOAD, "webhookTimestamp": timestamp}))

    def test_wrong_workspace_and_app_rejected(self):
        for key in ("organizationId", "appUserId"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.validate(request({**PAYLOAD, key: "other"}))
            data = copy.deepcopy(PAYLOAD)
            data["agentSession"][key] = "other"
            with self.assertRaises(ValueError):
                self.validate(request(data))

    def test_unrelated_events_ignored(self):
        self.assertIsNone(self.validate(request({**PAYLOAD, "type": "Issue"})))

    def test_other_categories_need_no_agent_identity_but_still_require_authenticity(self):
        payload = {"type": "Issue", "action": "update", "organizationId": "org", "webhookTimestamp": 1000000}
        self.assertIsNone(self.validate(request(payload)))
        with self.assertRaises(ValueError):
            self.validate(request({**payload, "organizationId": "other"}))
        event = request(payload)
        event["body"] += " "
        with self.assertRaises(ValueError):
            self.validate(event)

    def test_prompt_requires_activity_identity(self):
        data = {**PAYLOAD, "action": "prompted"}
        with self.assertRaises(ValueError):
            self.validate(request(data))
        data["agentActivity"] = {"id": "activity", "signal": "stop"}
        self.assertEqual(self.validate(request(data))["id"], "activity")

    def test_rejects_wrong_method_and_oversize(self):
        event = request()
        event["requestContext"]["http"]["method"] = "GET"
        with self.assertRaises(ValueError):
            self.validate(event)
        event = request()
        event["body"] = "x" * 240001
        with self.assertRaises(ValueError):
            self.validate(event)


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "inbox.db"
        self.box = inbox.Inbox(self.path)
        self.addCleanup(lambda: self.box.db.close())

    def add(self, identity):
        return self.box.add({"id": identity, "payload": PAYLOAD})

    def test_duplicates_do_not_rerun_even_after_completion(self):
        self.assertTrue(self.add("event"))
        self.assertFalse(self.add("event"))
        self.box.set_state("event", "done")
        self.assertFalse(self.add("event"))
        self.assertIsNone(self.box.next(SESSION))

    def test_restart_preserves_pending_but_interrupts_started_work(self):
        self.add("running")
        self.add("pending")
        self.box.set_state("running", "running")
        self.box.db.close()
        self.box = inbox.Inbox(self.path)
        self.assertEqual(self.box.next(SESSION)[::2], ("running", "interrupted"))
        self.box.set_state("running", "done")
        self.assertEqual(self.box.next(SESSION)[::2], ("pending", "pending"))

    def test_stop_discards_queued_work(self):
        self.add("first")
        self.add("second")
        self.box.stop(SESSION)
        self.assertIsNone(self.box.next(SESSION))

    def test_progress_preserves_start_time_and_status_survives_restart(self):
        self.add("work")
        self.box.set_state("work", "running")
        with patch.object(inbox.time, "time", return_value=100):
            self.box.record_progress("work", "Started")
        with patch.object(inbox.time, "time", return_value=150):
            self.box.record_progress("work", "Existing fix verified")
        self.box.add({"id": "status", "payload": PAYLOAD}, state="status_pending")
        self.assertEqual(self.box.sessions(), [])
        self.assertIsNone(self.box.next(SESSION))
        self.box.stop(SESSION)
        self.box.db.close()
        self.box = inbox.Inbox(self.path)
        self.assertEqual(self.box.snapshot(SESSION), (
            ("interrupted", 100, 150, "Existing fix verified", "thought"), 0))
        self.assertEqual(self.box.status_requests(), [("status", SESSION)])

    def test_legacy_inbox_without_progress_table_upgrades_without_replaying_work(self):
        self.add("old-run")
        self.box.set_state("old-run", "done")
        self.box.db.execute("DROP TABLE progress")
        self.box.db.commit()
        self.box.db.close()
        self.box = inbox.Inbox(self.path)
        self.assertEqual(self.box.snapshot(SESSION), (None, 0))
        self.assertIsNone(self.box.next(SESSION))


if __name__ == "__main__":
    unittest.main()
