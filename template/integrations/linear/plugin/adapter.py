"""Hermes platform adapter: SQS -> durable inbox -> normal gateway -> Linear."""
import asyncio
import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request
import uuid

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from .inbox import Inbox
from . import auth

logger = logging.getLogger(__name__)


def is_status_request(payload):
    # Only standalone, explicit follow-ups. Never discard a mixed status/work
    # request, parse quoted issue context as a command, or start another model.
    activity = payload.get("agentActivity") or {}
    if payload.get("action") != "prompted" or activity.get("signal"):
        return False
    body = (activity.get("content") or {}).get("body") or ""
    text = " ".join(body.lower().replace("’", "'").split()).rstrip("?.!")
    return text in {
        "/status", "status", "status update", "any update", "any updates",
        "what's the status", "what is the status", "what's happening",
        "what is happening", "are you still working", "how's it going", "how is it going",
    }


def age(timestamp):
    seconds = max(0, int(time.time() - timestamp))
    return f"{seconds // 60}m {seconds % 60}s"


def actor(payload):
    if payload["action"] == "prompted":
        return (payload.get("agentActivity") or {}).get("userId")
    return payload["agentSession"].get("creatorId")


def graphql(query, variables):
    token = auth._tokens.get()
    for attempt in range(2):
        request = urllib.request.Request("https://api.linear.app/graphql",
            data=json.dumps({"query": query, "variables": variables}).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with urllib.request.build_opener(auth._NoRedirect()).open(request, timeout=25) as response:
                result = json.load(response)
            if result.get("errors"):
                raise RuntimeError("Linear GraphQL request failed")
            return result["data"]
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            if status == 401 and attempt == 0:
                token = auth._tokens.get(rejected=token)
                continue
            raise RuntimeError(f"Linear API returned HTTP {status}") from None


class LinearAdapter(BasePlatformAdapter):
    def __init__(self, config):
        super().__init__(config, Platform("linear"))
        self.workers = {}
        self.current = {}
        self.last_typing = {}
        self.finished = set()
        self.poller = None
        self.delivery_locks = {}

    def _delivery_lock(self, session):
        return self.delivery_locks.setdefault(session, asyncio.Lock())

    async def connect(self, *, is_reconnect=False):
        import boto3
        from botocore.config import Config
        self.queue_url = os.environ["LINEAR_AGENT_QUEUE_URL"]
        self.organization = os.environ["LINEAR_ORGANIZATION_ID"]
        self.app_user = os.environ["LINEAR_APP_USER_ID"]
        self.allowed = {value.strip() for value in os.environ.get("LINEAR_ALLOWED_USERS", "").split(",")} - {"", "*"}
        if not self.allowed:
            raise RuntimeError("LINEAR_ALLOWED_USERS must explicitly authorize Linear user IDs")
        self.sqs = boto3.client("sqs", region_name=os.getenv("AWS_REGION", "us-east-1"),
            config=Config(connect_timeout=3, read_timeout=25, retries={"max_attempts": 2}))
        home = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes")))
        home.mkdir(parents=True, exist_ok=True)
        self.inbox = Inbox(home / "linear-agent-inbox.db")
        self._running = True
        self.poller = asyncio.create_task(self._poll())
        logger.info("Linear agent adapter connected via outbound SQS")
        return True

    async def disconnect(self):
        self._running = False
        tasks = [self.poller, *self.workers.values(), *self._background_tasks]
        for task in tasks:
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in tasks if t), return_exceptions=True)
        if hasattr(self, "inbox"):
            self.inbox.db.close()

    def _authorized(self, payload):
        session = payload.get("agentSession") or {}
        return (payload.get("type") == "AgentSessionEvent"
            and payload.get("action") in ("created", "prompted")
            and payload.get("organizationId") == self.organization
            and session.get("organizationId") == self.organization
            and payload.get("appUserId") == self.app_user
            and session.get("appUserId") == self.app_user
            and actor(payload) in self.allowed)

    async def _poll(self):
        while self._running:
            try:
                await self._drain_status_requests()
                for session in self.inbox.sessions():
                    if session not in self.workers or self.workers[session].done():
                        self.workers[session] = asyncio.create_task(self._work(session))
                result = await asyncio.to_thread(self.sqs.receive_message,
                    QueueUrl=self.queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=20,
                    VisibilityTimeout=60)
                for message in result.get("Messages", []):
                    record = json.loads(message["Body"])
                    await self._receive_record(record)
                    # Local commit precedes deletion. A crash here only redelivers a deduplicated event.
                    await asyncio.to_thread(self.sqs.delete_message,
                        QueueUrl=self.queue_url, ReceiptHandle=message["ReceiptHandle"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Linear queue processing failed (%s); retrying", type(exc).__name__)
                await asyncio.sleep(3)

    async def _receive_record(self, record):
        payload = record["payload"]
        if not self._authorized(payload):
            logger.warning("Rejected Linear agent event outside configured identity/user allowlist")
            return
        status = is_status_request(payload)
        fresh = self.inbox.add(record, state="status_pending" if status else "pending")
        if not fresh:
            return
        session = payload["agentSession"]["id"]
        activity = payload.get("agentActivity") or {}
        if activity.get("signal") == "stop":
            self.inbox.stop(session)
            event = self._event(record["id"], payload)
            event.text, event.allow_gateway_control = "/stop", True
            await self.handle_message(event)
        elif not status:
            await self._activity(session, "thought",
                "Received. Hermes is checking this request. Ask /status for the latest recorded update.",
                record["id"] + ":ack", ephemeral=True)

    async def _drain_status_requests(self):
        # Runs in the inbox poller, independently of the per-session work task.
        for event_id, session in self.inbox.status_requests():
            try:
                async with self._delivery_lock(session):
                    snapshot, pending = self.inbox.snapshot(session)
                    active = session in self.current and session not in self.finished
                    kind = "thought" if active or pending else "response"
                    if snapshot:
                        state, started, updated, body, update_kind = snapshot
                        if state == "interrupted":
                            heading = "The previous run was interrupted. Reply with a work request to resume."
                        elif active:
                            heading = f"The current run started {age(started)} ago."
                        elif update_kind == "error":
                            heading = "The last run reported an error."
                        elif update_kind == "response":
                            heading = "The last run returned a result."
                        else:
                            heading = "No run is active; the last run has no recorded final result."
                        text = f"{heading}\n\nLatest recorded update ({age(updated)} ago):\n{body[:1600]}"
                    else:
                        text = "No progress update has been recorded for this session yet."
                    if pending:
                        text += f"\n\n{pending} work request(s) queued."
                    await self._activity(session, kind, text, event_id + ":status")
                    self.inbox.set_state(event_id, "done")
            except Exception as exc:
                # Durable retry with the same activity ID; never enqueue as work.
                logger.warning("Linear status delivery failed (%s); retrying", type(exc).__name__)

    def _event(self, event_id, payload):
        session = payload["agentSession"]
        issue = session.get("issue") or {}
        activity = payload.get("agentActivity") or {}
        if payload["action"] == "prompted":
            text = (activity.get("content") or {}).get("body") or "Continue the current request."
        else:
            text = payload.get("promptContext") or json.dumps({"issue": issue, "comment": session.get("comment")})
        text = "Linear agent session for " + str(issue.get("identifier") or session.get("issueId") or "issue") + "\n\n" + text
        source = SessionSource(platform=self.platform, chat_id=session["id"], chat_type="dm",
            chat_name=issue.get("identifier", "Linear session"), user_id=actor(payload),
            scope_id=self.organization)
        return MessageEvent(text=text, source=source, message_id=event_id,
            platform_update_id=event_id, allow_gateway_control=False,
            metadata={"linear_event_id": event_id})

    async def _work(self, session):
        try:
            while self._running and (row := self.inbox.next(session)):
                event_id, payload, state = row
                if state == "interrupted":
                    body = "Hermes restarted during this run. Reply here to resume; I will check existing work before continuing."
                    async with self._delivery_lock(session):
                        await self._activity(session, "error", body, event_id + ":interrupted")
                        self.inbox.record_progress(event_id, body, "error")
                        self.inbox.set_state(event_id, "done")
                    continue
                self.current[session] = event_id
                self.finished.discard(session)
                self.inbox.set_state(event_id, "running")
                self.inbox.record_progress(event_id, "Checking the request and existing work; no findings reported yet.")
                self.last_typing[session] = time.monotonic()
                event = self._event(event_id, payload)
                await self.handle_message(event)
                if not event._gateway_accepted:
                    raise RuntimeError("Gateway did not accept Linear event")
                task = self._session_tasks.get(self._event_session_key(event))
                if task:
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        if not self._running or not task.cancelled():
                            raise
                self.inbox.set_state(event_id, "done")
                self.current.pop(session, None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Linear session failed (%s)", type(exc).__name__)
            # A started run must never be retried automatically: tool side effects may have happened.
            self.inbox.set_state(event_id, "interrupted")
        finally:
            self.current.pop(session, None)

    async def _activity(self, session, kind, body, key=None, *, ephemeral=False):
        # Stable UUIDv4 for retries of the same outgoing activity.
        identity = str(uuid.UUID(bytes=hashlib.sha256(key.encode()).digest()[:16], version=4)) if key else str(uuid.uuid4())
        variables = {"input": {"id": identity, "agentSessionId": session,
            "content": {"type": kind, "body": body}}}
        if ephemeral:
            variables["input"]["ephemeral"] = True
        try:
            result = await asyncio.to_thread(graphql,
                "mutation($input:AgentActivityCreateInput!){agentActivityCreate(input:$input){success agentActivity{id}}}", variables)
            if not result["agentActivityCreate"]["success"]:
                raise RuntimeError("Activity was not accepted")
        except Exception:
            # Reconcile ambiguous delivery using the caller-chosen activity ID before retrying.
            result = await asyncio.to_thread(graphql,
                "query($id:String!){agentActivity(id:$id){id agentSession{id}}}", {"id": identity})
            if ((result.get("agentActivity") or {}).get("agentSession") or {}).get("id") != session:
                raise
        return identity

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        # Ticket sessions are scoped reply destinations, never cron/home channels.
        # Hermes currently emits this chat onboarding notice for every platform.
        if not (metadata or {}).get("notify") and content == (
            "📬 No home channel is set for Linear. "
            "A home channel is where Hermes delivers cron job results and cross-platform "
            "messages.\n\nType /sethome to make this chat your home channel, or ignore "
            "to skip."
        ):
            return SendResult(success=True)
        try:
            kind = "response" if (metadata or {}).get("notify") else "thought"
            key = self.current.get(chat_id, reply_to or chat_id) + ":" + kind + ":" + content
            async with self._delivery_lock(chat_id):
                # A delayed commentary callback must not reopen a finished run.
                if kind == "thought" and chat_id in self.finished:
                    return SendResult(success=True)
                message_id = await self._activity(chat_id, kind, content, key)
                if chat_id in self.current:
                    self.inbox.record_progress(self.current[chat_id], content, kind)
                self.last_typing[chat_id] = time.monotonic()
                if kind == "response":
                    self.finished.add(chat_id)
            # PR cards supplement the response; a card failure must not duplicate the response.
            urls = sorted(set(re.findall(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/\d+", content)))
            if urls:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(graphql,
                        "mutation($id:String!,$input:AgentSessionUpdateInput!){agentSessionUpdate(id:$id,input:$input){success}}",
                        {"id": chat_id, "input": {"addedExternalUrls": [{"url": u, "label": "Pull request"} for u in urls]}})
            return SendResult(success=True, message_id=message_id)
        except Exception:
            return SendResult(success=False, error="Linear activity delivery failed")

    async def send_typing(self, chat_id, metadata=None):
        async with self._delivery_lock(chat_id):
            if chat_id in self.current and chat_id not in self.finished and time.monotonic() - self.last_typing.get(chat_id, 0) > 45:
                snapshot, _ = self.inbox.snapshot(chat_id)
                elapsed = f" ({age(snapshot[1])} elapsed)" if snapshot else ""
                await self._activity(chat_id, "thought",
                    f"Run active{elapsed}; no new finding reported. Ask /status for the last update.",
                    ephemeral=True)
                self.last_typing[chat_id] = time.monotonic()

    async def on_processing_complete(self, event, outcome):
        if outcome.value == "failure":
            async with self._delivery_lock(event.source.chat_id):
                body = "This run did not finish successfully. Reply here to retry after checking the latest status."
                await self._activity(event.source.chat_id, "error", body, event.message_id + ":failure")
                self.inbox.record_progress(event.message_id, body, "error")
                self.finished.add(event.source.chat_id)

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "name": "Linear agent session", "type": "dm"}
