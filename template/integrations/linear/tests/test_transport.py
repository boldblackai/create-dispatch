"""Transport failures, generic PR links, and standalone stack boundaries."""
import io
import json
import unittest
import urllib.error
from unittest.mock import AsyncMock, Mock, patch

from test_linear_agent import PAYLOAD, load, request, receiver
from test_linear_responsiveness import adapter_module, record


class GraphQLTests(unittest.TestCase):
    def test_401_refreshes_once_without_replaying_other_failures(self):
        with patch.object(adapter_module.auth, "_tokens") as tokens, \
                patch.object(adapter_module.urllib.request, "build_opener") as opener:
            tokens.get.side_effect = ["first", "second"]
            opener.return_value.open.side_effect = [
                urllib.error.HTTPError("https://api.linear.app/graphql", 401, "", {}, io.BytesIO()),
                io.StringIO(json.dumps({"data": {"ok": True}})),
            ]
            self.assertEqual(adapter_module.graphql("query { viewer { id } }", {}), {"ok": True})
            tokens.get.assert_any_call(rejected="first")
            self.assertEqual(opener.return_value.open.call_count, 2)

        for status in (403, 429, 500):
            with self.subTest(status=status), patch.object(adapter_module.auth, "_tokens") as tokens, \
                    patch.object(adapter_module.urllib.request, "build_opener") as opener:
                tokens.get.return_value = "token"
                opener.return_value.open.side_effect = urllib.error.HTTPError(
                    "https://api.linear.app/graphql", status, "secret", {}, io.BytesIO(b"secret"))
                with self.assertRaisesRegex(RuntimeError, f"HTTP {status}") as error:
                    adapter_module.graphql("query {}", {})
                self.assertNotIn("secret", str(error.exception))
                opener.return_value.open.assert_called_once()


class GenericAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_any_public_github_repo_can_supply_pr_cards(self):
        adapter = adapter_module.LinearAdapter(None)
        adapter._activity = AsyncMock(return_value="activity")
        with patch.object(adapter_module, "graphql", Mock()) as graphql:
            result = await adapter.send("session", "See https://github.com/example/project/pull/123.")
        self.assertTrue(result.success)
        self.assertEqual(graphql.call_args.args[1]["input"]["addedExternalUrls"], [
            {"url": "https://github.com/example/project/pull/123", "label": "Pull request"}])

    async def test_ticket_text_never_becomes_gateway_control(self):
        adapter = adapter_module.LinearAdapter(None)
        adapter.organization = "org"
        event = adapter._event("event", record(text="/restart")["payload"])
        self.assertFalse(event.allow_gateway_control)


class PayloadTests(unittest.TestCase):
    def test_signed_malformed_payloads_are_rejected(self):
        payloads = [[], {**PAYLOAD, "agentSession": []},
                    {**PAYLOAD, "action": "prompted", "agentActivity": {"id": 1}},
                    {**PAYLOAD, "action": "prompted", "agentActivity": {"id": "id", "content": {"body": []}}}]
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises((ValueError, KeyError, TypeError)):
                receiver.validate(request(payload), "secret", "org", "app", now=1000)


class InfrastructureTests(unittest.TestCase):
    def test_standalone_stack_has_no_identity_defaults_or_gateway_resources(self):
        template = load("linear_template", "render_template.py").template()
        for name in ("OrganizationId", "AppUserId"):
            self.assertNotIn("Default", template["Parameters"][name])
        self.assertFalse(any(r["Type"].startswith(("AWS::ECS::", "AWS::EC2::"))
                             for r in template["Resources"].values()))
        policy = template["Resources"]["ConsumerPolicy"]["Properties"]
        self.assertEqual(policy["Roles"], [{"Fn::Sub": "${ClawName}-task"}])
        statement = policy["PolicyDocument"]["Statement"][0]
        self.assertEqual(statement["Resource"], {"Fn::GetAtt": ["Queue", "Arn"]})
        self.assertNotIn("sqs:SendMessage", statement["Action"])

    def test_receiver_code_is_embedded_and_permissions_cover_function_url(self):
        template = load("linear_template", "render_template.py").template()
        resources = template["Resources"]
        self.assertIn("def validate(", resources["Receiver"]["Properties"]["Code"]["ZipFile"])
        self.assertTrue(resources["InvokePermission"]["Properties"]["InvokedViaFunctionUrl"])
        self.assertEqual(resources["UrlPermission"]["Properties"]["FunctionUrlAuthType"], "NONE")
        self.assertTrue(resources["Queue"]["Properties"]["SqsManagedSseEnabled"])
