from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse


SPEC = importlib.util.spec_from_file_location(
    "linear_auth", Path(__file__).resolve().parents[1]
    / "plugin/auth.py"
)
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)
ENV = {"LINEAR_CLIENT_ID": "test-client", "LINEAR_CLIENT_SECRET": "test-secret"}


def response(token="test-token", **kwargs):
    return io.StringIO(json.dumps({"access_token": token, "expires_in": 3600,
                                  "scope": "read write app:assignable app:mentionable webhook",
                                  "token_type": "Bearer", **kwargs}))


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(plugin.os.environ, ENV, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.opener = patch.object(plugin.urllib.request, "build_opener")
        self.http = self.opener.start().return_value
        self.addCleanup(self.opener.stop)
        self.cache = plugin.TokenCache()

    def test_reuses_token_then_renews_before_expiry(self):
        self.http.open.side_effect = [response("first"), response("second")]
        with patch.object(plugin.time, "monotonic", return_value=100):
            self.assertEqual(self.cache.get(), "first")
            self.assertEqual(self.cache.get(), "first")
        with patch.object(plugin.time, "monotonic", return_value=3401):
            self.assertEqual(self.cache.get(), "second")
        self.assertEqual(self.http.open.call_count, 2)
        request = self.http.open.call_args.args[0]
        form = urllib.parse.parse_qs(request.data.decode())
        self.assertEqual(form["grant_type"], ["client_credentials"])
        self.assertEqual(form["scope"], ["read,write,app:assignable,app:mentionable,webhook"])

    def test_concurrent_rejected_requests_reuse_new_token(self):
        self.http.open.side_effect = [response("first"), response("second")]
        self.assertEqual(self.cache.get(), "first")
        self.assertEqual(self.cache.get(rejected="first"), "second")
        self.assertEqual(self.cache.get(rejected="first"), "second")
        self.assertEqual(self.http.open.call_count, 2)

    def test_missing_credentials_fail_without_network(self):
        with patch.dict(plugin.os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "missing"):
                self.cache.get()
        self.http.open.assert_not_called()

    def test_http_error_body_is_not_exposed(self):
        self.http.open.side_effect = urllib.error.HTTPError(
            plugin.TOKEN_URL, 401, "test-secret", {}, io.BytesIO(b"test-secret")
        )
        with self.assertRaisesRegex(RuntimeError, "HTTP 401") as exc:
            self.cache.get()
        self.assertNotIn("test-secret", str(exc.exception))

    def test_invalid_or_readonly_tokens_are_rejected(self):
        for invalid in ({"scope": "read"}, {"expires_in": 0},
                        {"token_type": "Other"}, {"access_token": "bad\nheader"}):
            with self.subTest(invalid=invalid):
                self.http.open.return_value = response(**invalid)
                with self.assertRaises(RuntimeError):
                    self.cache.get()

    def test_missing_agent_capability_is_rejected_without_caching(self):
        for scope in ("read write", "read write app:assignable",
                      "read write app:mentionable",
                      "read write app:assignable app:mentionable"):
            with self.subTest(scope=scope):
                self.http.open.return_value = response(scope=scope)
                with self.assertRaisesRegex(RuntimeError, "insufficiently scoped"):
                    self.cache.get()
                self.assertIsNone(self.cache._token)

    def test_token_request_cannot_redirect(self):
        self.assertIsNone(plugin._NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.org"))
