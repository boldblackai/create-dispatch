"""Linear routing and delivery tests; no model, AWS, or Linear API calls."""
import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin"


class BaseAdapter:
    def __init__(self, config, platform):
        self.platform = platform
        self._session_tasks = {}
        self._background_tasks = set()

    def _event_session_key(self, event):
        return event.source.chat_id


def load_adapter():
    modules = {}
    for name in ("gateway", "gateway.config", "gateway.platforms", "gateway.platforms.base",
                 "gateway.platforms.event", "gateway.session", "linear_test_plugin"):
        modules[name] = types.ModuleType(name)
    modules["gateway.config"].Platform = str
    modules["gateway.platforms.base"].BasePlatformAdapter = BaseAdapter
    modules["gateway.platforms.base"].SendResult = types.SimpleNamespace
    modules["gateway.platforms.event"].MessageEvent = types.SimpleNamespace
    modules["gateway.session"].SessionSource = types.SimpleNamespace
    modules["linear_test_plugin"].__path__ = [str(PLUGIN)]
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location("linear_test_plugin.adapter", PLUGIN / "adapter.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


adapter_module = load_adapter()
PAYLOAD = {
    "type": "AgentSessionEvent", "action": "created", "organizationId": "org", "appUserId": "app",
    "agentSession": {"id": "session", "organizationId": "org", "appUserId": "app", "creatorId": "user"},
}


def record(identity="work", text=None, **activity):
    payload = copy.deepcopy(PAYLOAD)
    if text is not None:
        payload["action"] = "prompted"
        payload["agentActivity"] = {"userId": "user", "content": {"body": text}, **activity}
    return {"id": identity, "payload": payload}


class ClassificationTests(unittest.TestCase):
    def test_standalone_status_phrases(self):
        for text in ("/status", " STATUS? ", "Any updates?", "What’s happening?",
                     "are you still working?", "How is it going?", "What's the status?"):
            with self.subTest(text=text):
                self.assertTrue(adapter_module.is_status_request(record(text=text)["payload"]))

    def test_work_context_and_mixed_requests_are_not_status_commands(self):
        for text in ("status then fix it", "check deployment status", "what's happening? fix the tests",
                     "> status", "`/status`", "change status to done", "continue", "/stop"):
            with self.subTest(text=text):
                self.assertFalse(adapter_module.is_status_request(record(text=text)["payload"]))
        payload = record()["payload"]
        payload["promptContext"] = "/status"
        self.assertFalse(adapter_module.is_status_request(payload))
        self.assertFalse(adapter_module.is_status_request(record(text="status", signal="stop")["payload"]))


class ResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "inbox.db"
        self.adapter = adapter_module.LinearAdapter(None)
        self.adapter.inbox = adapter_module.Inbox(self.path)
        self.addCleanup(lambda: self.adapter.inbox.db.close())
        self.adapter.organization, self.adapter.app_user = "org", "app"
        self.adapter.allowed = {"user"}
        self.adapter._running = True
        self.adapter._activity = AsyncMock(return_value="activity-id")
        self.adapter.handle_message = AsyncMock()

    def start_recorded_run(self):
        self.adapter.inbox.add(record())
        self.adapter.inbox.set_state("work", "running")
        self.adapter.inbox.record_progress("work", "Verified existing fix: PR #123; deployment not checked.")
        self.adapter.current["session"] = "work"

    async def request_status(self, identity="status", text="/status"):
        await self.adapter._receive_record(record(identity, text))
        await self.adapter._drain_status_requests()

    async def test_status_bypasses_a_blocked_run_without_starting_a_model(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def accept(event):
            event._gateway_accepted = True
            self.adapter._session_tasks["session"] = asyncio.create_task(release.wait())
            entered.set()

        self.adapter.handle_message.side_effect = accept
        await self.adapter._receive_record(record())
        worker = asyncio.create_task(self.adapter._work("session"))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await self.adapter.send("session", "Verified existing fix: PR #123; deployment unverified.")
            await asyncio.wait_for(self.request_status(), 1)
            self.assertFalse(worker.done())
            self.adapter.handle_message.assert_awaited_once()
            call = self.adapter._activity.call_args
            self.assertEqual(call.args[:2], ("session", "thought"))
            self.assertIn("PR #123", call.args[2])
            self.assertIn("Latest recorded update", call.args[2])
            self.assertEqual(self.adapter.inbox.status_requests(), [])
            self.assertIsNone(self.adapter.inbox.next("session"))
        finally:
            release.set()
            await worker

    async def test_unauthorized_status_never_reads_or_sends(self):
        self.start_recorded_run()
        for target in ("organizationId", "appUserId"):
            incoming = record("bad-" + target, "status")
            incoming["payload"][target] = "other"
            await self.adapter._receive_record(incoming)
        incoming = record("bad-user", "status")
        incoming["payload"]["agentActivity"]["userId"] = "stranger"
        await self.adapter._receive_record(incoming)
        await self.adapter._drain_status_requests()
        self.adapter._activity.assert_not_awaited()
        self.adapter.handle_message.assert_not_awaited()

    async def test_mixed_request_stays_queued_and_stop_keeps_its_control_path(self):
        self.start_recorded_run()
        await self.adapter._receive_record(record("mixed", "status then fix tests"))
        self.assertEqual(self.adapter.inbox.next("session")[0], "mixed")
        await self.request_status()
        self.assertIn("1 work request(s) queued", self.adapter._activity.call_args.args[2])
        await self.adapter._receive_record(record("stop", "status", signal="stop"))
        event = self.adapter.handle_message.call_args.args[0]
        self.assertEqual(event.text, "/stop")
        self.assertTrue(event.allow_gateway_control)
        self.assertIsNone(self.adapter.inbox.next("session"))

    async def test_status_retry_is_durable_deduplicated_and_never_work(self):
        self.start_recorded_run()
        self.adapter._activity.side_effect = RuntimeError("offline")
        await self.request_status()
        self.assertEqual(self.adapter.inbox.status_requests(), [("status", "session")])
        self.assertIsNone(self.adapter.inbox.next("session"))
        self.adapter.inbox.db.close()
        self.adapter.inbox = adapter_module.Inbox(self.path)
        self.adapter.current.clear()
        self.adapter._activity.side_effect = None
        await self.request_status()
        call = self.adapter._activity.call_args
        self.assertIn("interrupted", call.args[2])
        self.assertIn("PR #123", call.args[2])
        self.assertEqual(call.args[3], "status:status")
        self.assertEqual(self.adapter.inbox.status_requests(), [])
        self.adapter._activity.reset_mock()
        await self.request_status()
        self.adapter._activity.assert_not_awaited()
        self.adapter.handle_message.assert_not_awaited()

    async def test_idle_status_returns_last_result_without_a_new_run(self):
        self.start_recorded_run()
        await self.adapter.send("session", "Existing fix verified; production deployment pending.", metadata={"notify": True})
        self.adapter.inbox.set_state("work", "done")
        self.adapter.current.clear()
        await self.request_status()
        self.assertEqual(self.adapter._activity.call_args.args[1], "response")
        self.assertIn("last run returned a result", self.adapter._activity.call_args.args[2])
        self.adapter.handle_message.assert_not_awaited()

    async def test_no_history_and_queued_work_do_not_claim_success(self):
        await self.request_status()
        self.assertIn("No progress update", self.adapter._activity.call_args.args[2])
        await self.adapter._receive_record(record())
        await self.request_status("status2")
        self.assertEqual(self.adapter._activity.call_args.args[1], "thought")
        self.assertIn("1 work request(s) queued", self.adapter._activity.call_args.args[2])

    async def test_poller_handles_status_while_session_work_remains_running(self):
        self.start_recorded_run()
        incoming = {"Body": json.dumps(record("poll-status", "any updates?")), "ReceiptHandle": "receipt"}
        queue = Mock()

        def receive(**kwargs):
            if queue.receive_message.call_count == 1:
                return {"Messages": [incoming]}
            self.adapter._running = False
            return {}

        queue.receive_message.side_effect = receive
        self.adapter.sqs, self.adapter.queue_url = queue, "queue"
        await asyncio.wait_for(self.adapter._poll(), 1)
        queue.delete_message.assert_called_once_with(QueueUrl="queue", ReceiptHandle="receipt")
        self.assertIn("PR #123", self.adapter._activity.call_args.args[2])
        self.assertEqual(self.adapter.current, {"session": "work"})
        self.adapter.handle_message.assert_not_awaited()

    async def test_late_interim_update_cannot_replace_a_final_result(self):
        self.start_recorded_run()
        await self.adapter.send("session", "Final result", metadata={"notify": True})
        self.adapter._activity.reset_mock()
        self.assertTrue((await self.adapter.send("session", "Delayed interim callback")).success)
        self.adapter._activity.assert_not_awaited()
        self.assertEqual(self.adapter.inbox.snapshot("session")[0][3], "Final result")

    async def test_failed_progress_delivery_does_not_replace_last_known_update(self):
        self.start_recorded_run()
        self.adapter._activity.side_effect = RuntimeError("offline")
        result = await self.adapter.send("session", "New finding")
        self.assertFalse(result.success)
        self.assertIn("PR #123", self.adapter.inbox.snapshot("session")[0][3])

    async def test_failure_status_is_explicit_and_heartbeats_stop(self):
        self.start_recorded_run()
        event = types.SimpleNamespace(source=types.SimpleNamespace(chat_id="session"), message_id="work")
        await self.adapter.on_processing_complete(event, types.SimpleNamespace(value="failure"))
        await self.request_status()
        self.assertIn("reported an error", self.adapter._activity.call_args.args[2])
        self.adapter._activity.reset_mock()
        await self.adapter.send_typing("session")
        self.adapter._activity.assert_not_awaited()

    async def test_liveness_is_ephemeral_and_does_not_overwrite_progress(self):
        self.start_recorded_run()
        with patch.object(adapter_module.time, "monotonic", return_value=100):
            await self.adapter.send_typing("session")
            await self.adapter.send_typing("session")
        self.adapter._activity.assert_awaited_once()
        self.assertTrue(self.adapter._activity.call_args.kwargs["ephemeral"])
        self.assertIn("PR #123", self.adapter.inbox.snapshot("session")[0][3])
        await self.adapter.send("session", "Finished", metadata={"notify": True})
        self.adapter._activity.reset_mock()
        await self.adapter.send_typing("session")
        self.adapter._activity.assert_not_awaited()

    async def test_final_delivery_wins_race_with_status_and_is_not_reopened(self):
        self.start_recorded_run()
        entered, release = asyncio.Event(), asyncio.Event()

        async def deliver(*args, **kwargs):
            if args[2] == "Final result":
                entered.set()
                await release.wait()
            return "id"

        self.adapter._activity.side_effect = deliver
        final = asyncio.create_task(self.adapter.send("session", "Final result", metadata={"notify": True}))
        await entered.wait()
        status = asyncio.create_task(self.request_status())
        release.set()
        await asyncio.gather(final, status)
        self.assertEqual(self.adapter._activity.call_args.args[1], "response")
        self.assertIn("Final result", self.adapter._activity.call_args.args[2])

    async def test_ephemeral_graphql_payload_and_stable_retry_identity(self):
        activity = adapter_module.LinearAdapter._activity.__get__(self.adapter)
        graphql = Mock(return_value={"agentActivityCreate": {"success": True}})
        with patch.object(adapter_module, "graphql", graphql):
            first = await activity("session", "thought", "Running", "heartbeat", ephemeral=True)
            second = await activity("session", "thought", "Running", "heartbeat", ephemeral=True)
        self.assertEqual(first, second)
        self.assertTrue(graphql.call_args.args[1]["input"]["ephemeral"])
        with patch.object(adapter_module, "graphql", side_effect=[TimeoutError(), {"agentActivity": {"agentSession": {"id": "session"}}}]):
            self.assertEqual(await activity("session", "thought", "Running", "heartbeat"), first)


if __name__ == "__main__":
    unittest.main()
