"""Memory-only client-credentials tokens for the Linear app actor."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


TOKEN_URL = "https://api.linear.app/oauth/token"
# Keep every token issuer on this scope set: Linear revokes existing app tokens
# when client_credentials is requested with a different set of scopes.
SCOPES = ("read", "write", "app:assignable", "app:mentionable", "webhook")
logger = logging.getLogger(__name__)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TokenCache:
    """One thread-safe, memory-only token cache per gateway process."""

    def __init__(self):
        self._lock = threading.Lock()
        self._token = None
        self._renew_at = 0.0

    def get(self, rejected=None):
        with self._lock:
            if (self._token and time.monotonic() < self._renew_at
                    and self._token != rejected):
                return self._token
            client_id = os.environ.get("LINEAR_CLIENT_ID", "").strip()
            client_secret = os.environ.get("LINEAR_CLIENT_SECRET", "").strip()
            if not client_id or not client_secret:
                raise RuntimeError("Linear app credentials are missing from the environment")
            body = urllib.parse.urlencode({
                "grant_type": "client_credentials", "scope": ",".join(SCOPES),
                "client_id": client_id, "client_secret": client_secret,
            }).encode()
            request = urllib.request.Request(TOKEN_URL, data=body, headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            })
            # Never redirect a request containing the app's client secret.
            try:
                with urllib.request.build_opener(_NoRedirect()).open(request, timeout=20) as response:
                    record = json.load(response)
            except urllib.error.HTTPError as exc:
                status = exc.code
                exc.close()
                raise RuntimeError(f"Linear token endpoint returned HTTP {status}") from None
            except Exception:
                # Endpoint responses/exceptions must not leak credentials into logs.
                raise RuntimeError("Linear token request failed") from None
            try:
                token = record["access_token"]
                lifetime = int(record["expires_in"])
                scopes = set(record.get("scope", "").replace(",", " ").split())
                if (not isinstance(token, str) or not token or "\r" in token or "\n" in token
                        or record.get("token_type", "").lower() != "bearer"
                        or lifetime <= 0 or not set(SCOPES) <= scopes):
                    raise ValueError()
            except (KeyError, TypeError, ValueError, AttributeError):
                raise RuntimeError("Linear returned an invalid or insufficiently scoped app token") from None
            self._token = token
            self._renew_at = time.monotonic() + lifetime - min(300, lifetime / 10)
            logger.info("Linear app token acquired with read/write, assignable, mentionable and webhook scopes; renewal enabled")
            return token


_tokens = TokenCache()
