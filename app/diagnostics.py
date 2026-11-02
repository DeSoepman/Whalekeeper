"""Failure reports to the Whalekeeper log server (see logserver/).

On by default; set WHALEKEEPER_DIAGNOSTICS=off to disable. Reports hold container
state, Whalekeeper's own log lines and the recent logs of failed containers, with
credentials masked on a best-effort basis. Environment variables are never sent.
"""
import asyncio
import json
import logging
import os
import re
import sys
import time
import traceback
import uuid
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional, Set

import aiohttp
import docker
import requests

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://whalekeeper.rubenpauwels.be"

# Ships in the public image, so this is not a secret: it only keeps casual bots
# away from the log server. Never reuse it for anything else.
INGEST_TOKEN = "fa42f4bd6461404edec6da425a4ba1ab38b64535577ee8f7710b9df977867238"

LOG_BUFFER_SIZE = 200
LOG_LINES_PER_REPORT = 50
CONTAINER_LOG_LINES = 50
MAX_LOG_LINE_LENGTH = 1000
MAX_MESSAGE_LENGTH = 8000
MAX_TRACEBACK_LENGTH = 8000
# The log server rejects bodies over 128 KB.
MAX_PAYLOAD_BYTES = 120_000
HEARTBEAT_INTERVAL_SECONDS = 6 * 3600
ERROR_REPEAT_SECONDS = 24 * 3600
MAX_TRACKED_ERRORS = 1000
FORWARDED_LOGGERS = ("app", "asyncio", "uvicorn")
# Expected for every locally built image, which has no registry to pull from.
IGNORED_ERROR_PATTERNS = ("pull access denied",)
# Pass as extra= on an error log when the same failure is reported explicitly.
ALREADY_REPORTED = {"diagnostics_reported": True}
REQUEST_TIMEOUT_SECONDS = 10
RETRY_DELAYS_SECONDS = (0, 30, 300)
INSTANCE_ID_SETTING = "diagnostics_instance_id"

REDACTIONS = (
    (
        re.compile(
            r"""(?i)(["']?\b(?:[\w-]*password|passwd|pwd|[\w-]*secret|[\w-]*token|api[_-]?key|authorization)\b["']?\s*[=:]\s*)(?:bearer\s+)?(?:"[^"]*"|'[^']*'|[^\s,;&"'}]+)"""
        ),
        r"\1[REDACTED]",
    ),
    (re.compile(r"(?i)\bbearer\s+[\w.~+/=-]+"), "Bearer [REDACTED]"),
    (re.compile(r"://[^/\s:@]+:[^/\s@]+@"), "://[REDACTED]@"),
    (re.compile(r"(?i)(discord(?:app)?\.com/api/webhooks/)\S+"), r"\1[REDACTED]"),
)


def redact(text: str) -> str:
    """Mask common credential patterns. Best effort: not a guarantee."""
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def redact_value(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(str(value))


class LogBuffer(logging.Handler):
    """Keeps the most recent log lines in memory for failure reports."""

    def __init__(self, size: int = LOG_BUFFER_SIZE):
        super().__init__()
        self.lines = deque(maxlen=size)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:
            self.handleError(record)

    def tail(self, count: int = LOG_LINES_PER_REPORT) -> List[str]:
        return list(self.lines)[-count:]


def current_traceback() -> Optional[str]:
    """Traceback of the exception being handled, if any."""
    if sys.exc_info()[0] is None:
        return None
    return traceback.format_exc()[-MAX_TRACEBACK_LENGTH:]


def container_log_tail(container, lines: int = CONTAINER_LOG_LINES) -> List[str]:
    """Recent stdout/stderr of a container. Masked later, when the report is built."""
    output = container.logs(tail=lines, timestamps=True)
    text = output.decode("utf-8", "replace")
    return [line[:MAX_LOG_LINE_LENGTH] for line in text.splitlines()]


def describe_container(container) -> Dict:
    """Whitelisted container state. Must never include Config.Env."""
    attrs = container.attrs
    state = attrs.get("State") or {}
    host_config = attrs.get("HostConfig") or {}
    labels = (attrs.get("Config") or {}).get("Labels") or {}
    return {
        "name": container.name,
        "status": state.get("Status"),
        "error": state.get("Error"),
        "exit_code": state.get("ExitCode"),
        "oom_killed": state.get("OOMKilled"),
        "restart_count": attrs.get("RestartCount"),
        "health": (state.get("Health") or {}).get("Status"),
        "image": (attrs.get("Config") or {}).get("Image"),
        "network_mode": host_config.get("NetworkMode"),
        "restart_policy": (host_config.get("RestartPolicy") or {}).get("Name"),
        "compose_project": labels.get("com.docker.compose.project"),
        "compose_service": labels.get("com.docker.compose.service"),
    }


def fit_payload(payload: Dict, limit: int = MAX_PAYLOAD_BYTES) -> Dict:
    """Trim the least important parts until the payload fits the server's limit."""
    def fits() -> bool:
        return len(json.dumps(payload)) <= limit

    context = payload["context"]
    if fits():
        return payload
    for dependent in context.get("dependents", []):
        dependent.pop("logs", None)
    if fits():
        return payload
    payload["log_lines"] = payload["log_lines"][-10:]
    if fits():
        return payload
    if isinstance(context.get("container"), dict) and context["container"].get("logs"):
        context["container"]["logs"] = context["container"]["logs"][-10:]
    if fits():
        return payload
    payload["context"] = {"truncated": True}
    payload["log_lines"] = []
    return payload


class ErrorLogForwarder(logging.Handler):
    """
    Reports every error logged anywhere in the app, so new code is covered without
    wiring. Repeats of the same error are sent once per ERROR_REPEAT_SECONDS.
    """

    def __init__(self, reporter: "DiagnosticsReporter"):
        super().__init__(level=logging.ERROR)
        self.reporter = reporter
        self.last_sent: Dict[str, float] = {}

    def should_send(self, record: logging.LogRecord, message: str) -> bool:
        if record.name.split(".")[0] not in FORWARDED_LOGGERS:
            return False
        if record.name == __name__ or getattr(record, "diagnostics_reported", False):
            return False
        lowered = message.lower()
        if any(pattern in lowered for pattern in IGNORED_ERROR_PATTERNS):
            return False

        # Ignore IDs and numbers, so one error with a changing ID counts as one error.
        key = f"{record.name}:{re.sub(r'[0-9a-f]{8,}|[0-9]+', '#', lowered)[:500]}"
        # Handler.handle() already holds the handler's lock around emit().
        now = time.monotonic()
        if now - self.last_sent.get(key, -ERROR_REPEAT_SECONDS) < ERROR_REPEAT_SECONDS:
            return False
        if len(self.last_sent) >= MAX_TRACKED_ERRORS:
            self.last_sent.clear()
        self.last_sent[key] = now
        return True

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if not self.should_send(record, message):
                return
            if record.exc_info and record.exc_info[0]:
                trace = "".join(traceback.format_exception(*record.exc_info))[-MAX_TRACEBACK_LENGTH:]
            else:
                trace = current_traceback()
            context = {
                "logger": record.name,
                "location": f"{record.pathname}:{record.lineno} in {record.funcName}",
            }
            if trace:
                context["traceback"] = trace
            self.reporter.report("error_log", "error", message, context=context)
        except Exception:
            self.handleError(record)


class DiagnosticsReporter:
    def __init__(self, url: str, token: str, instance_id: str, instance_name: str,
                 version: str, log_buffer: LogBuffer):
        self.endpoint = f"{url.rstrip('/')}/api/events"
        self.heartbeat_endpoint = f"{url.rstrip('/')}/api/heartbeat"
        self.token = token
        self.instance_id = instance_id
        self.instance_name = instance_name
        self.version = version
        self.log_buffer = log_buffer
        self.tasks: Set[asyncio.Task] = set()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.heartbeat_task: Optional[asyncio.Task] = None

    def report(self, kind: str, level: str, message: str,
               container: Optional[str] = None, context: Optional[Dict] = None) -> None:
        """
        Queue a report. Sending happens in the background so updates never wait on it.
        Safe to call from any thread: sync routes log errors from a thread pool.
        """
        payload = fit_payload({
            "instance_id": self.instance_id,
            "instance_name": self.instance_name,
            "version": self.version,
            "kind": kind,
            "level": level,
            "container": container,
            "message": redact(message)[:MAX_MESSAGE_LENGTH],
            "context": redact_value(context or {}),
            "log_lines": [
                redact(line)[:MAX_LOG_LINE_LENGTH] for line in self.log_buffer.tail()
            ],
        })
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            if self.loop is None:
                logger.warning("Dropped diagnostics report: event loop not available")
                return
            self.loop.call_soon_threadsafe(self._enqueue, payload)
            return
        self._enqueue(payload)

    def _enqueue(self, payload: Dict) -> None:
        task = asyncio.create_task(self._send(payload))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _send(self, payload: Dict) -> None:
        for delay in RETRY_DELAYS_SECONDS:
            await asyncio.sleep(delay)
            try:
                status = await self._post(self.endpoint, payload)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                logger.warning(f"Could not send diagnostics report: {type(e).__name__}")
                continue
            if status < 300:
                return
            logger.warning(f"Log server rejected diagnostics report: HTTP {status}")
            if status < 500 and status != 429:
                return

    async def _post(self, endpoint: str, payload: Dict) -> int:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # No redirects: the token must only ever reach the configured server.
            async with session.post(
                endpoint,
                json=payload,
                headers={"Authorization": f"Bearer {self.token}"},
                allow_redirects=False,
            ) as response:
                return response.status

    async def _heartbeat_loop(self) -> None:
        """Lets the log server notice when this instance goes silent."""
        payload = {
            "instance_id": self.instance_id,
            "instance_name": self.instance_name,
            "version": self.version,
        }
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            try:
                status = await self._post(self.heartbeat_endpoint, payload)
                if status >= 300:
                    logger.warning(f"Log server rejected heartbeat: HTTP {status}")
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                logger.warning(f"Could not send heartbeat: {type(e).__name__}")

    def start_heartbeat(self) -> None:
        self.heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def drain(self) -> None:
        """Wait for queued reports."""
        await asyncio.sleep(0)
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    async def close(self) -> None:
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
        for task in list(self.tasks):
            task.cancel()
        await self.drain()


def read_version() -> str:
    try:
        return Path("VERSION").read_text().strip() or "unknown"
    except OSError:
        return "unknown"


def get_instance_id(db) -> str:
    instance_id = db.get_secure_setting(INSTANCE_ID_SETTING)
    if not instance_id:
        instance_id = uuid.uuid4().hex
        db.set_secure_setting(INSTANCE_ID_SETTING, instance_id)
    return instance_id


def get_instance_name(client) -> str:
    try:
        return str(client.info().get("Name") or "unknown")[:255]
    except (docker.errors.DockerException, requests.RequestException) as e:
        logger.warning(f"Could not read Docker host name: {e}")
        return "unknown"


def create_reporter(db, client, log_buffer: LogBuffer) -> Optional[DiagnosticsReporter]:
    """Build the reporter, or return None when diagnostics are switched off."""
    if os.environ.get("WHALEKEEPER_DIAGNOSTICS", "on").strip().lower() in ("off", "false", "0", "no"):
        logger.info("Diagnostics reporting is disabled")
        return None
    url = os.environ.get("WHALEKEEPER_DIAGNOSTICS_URL", DEFAULT_URL)
    logger.info("Diagnostics reporting is enabled (set WHALEKEEPER_DIAGNOSTICS=off to disable)")
    return DiagnosticsReporter(
        url=url,
        token=INGEST_TOKEN,
        instance_id=get_instance_id(db),
        instance_name=get_instance_name(client),
        version=read_version(),
        log_buffer=log_buffer,
    )
