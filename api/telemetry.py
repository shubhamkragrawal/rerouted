"""Privacy-safe local request telemetry and lightweight service controls."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any
from uuid import UUID, uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("rerouted.telemetry")
TELEMETRY_EXPORT_MODE = os.environ.get("REROUTED_TELEMETRY_EXPORT", "none").casefold()
if TELEMETRY_EXPORT_MODE != "none":
    raise RuntimeError(
        "this local build supports REROUTED_TELEMETRY_EXPORT=none only; "
        "remote exporters are not configured"
    )
trace_id_var: ContextVar[str] = ContextVar("rerouted_trace_id", default="")
session_id_var: ContextVar[str] = ContextVar("rerouted_session_id", default="")


def _opaque_id(value: str | None) -> str:
    if value:
        try:
            return str(UUID(value))
        except ValueError:
            pass
    return str(uuid4())


def record_event(event: str, **attributes: Any) -> None:
    """Write one allowlisted, content-free JSON event to the local application log."""

    safe: dict[str, Any] = {
        "event": event,
        "trace_id": trace_id_var.get(),
        "session_id": session_id_var.get(),
    }
    allowed = {
        "request.completed": {"method", "route", "status_code", "duration_ms"},
        "request.rejected": {"reason", "status_code"},
        "request.validation_failed": {"error_count"},
        "tool.completed": {"tool", "outcome", "duration_ms"},
        "answer.completed": {
            "planner_version", "tool_names", "tool_error_count", "ranked_clause_ids",
            "resolver_status", "cited_clause_ids", "disposition", "refusal_reason",
            "retry_count", "token_count", "estimated_cost_usd", "cache_behavior",
            "retrieval_provider", "retrieval_model", "retrieval_fallback_used",
        },
    }
    for key in allowed.get(event, set()):
        if key in attributes:
            safe[key] = attributes[key]
    logger.info(json.dumps(safe, separators=(",", ":"), sort_keys=True))


class LocalRateLimiter:
    """Thread-safe fixed-window limiter with bounded per-client state."""

    def __init__(
        self,
        limit: int = 120,
        window_seconds: float = 60.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_clients: int = 4096,
    ) -> None:
        if limit < 1 or window_seconds <= 0 or max_clients < 1:
            raise ValueError("rate limit, window, and client bound must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock
        self._max_clients = max_clients
        self._windows: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def allow(self, client_key: str) -> bool:
        now = self._clock()
        with self._lock:
            expired = [
                key
                for key, (started, _) in self._windows.items()
                if now - started >= self.window_seconds
            ]
            for key in expired:
                del self._windows[key]

            window = self._windows.get(client_key)
            if window is None:
                if len(self._windows) >= self._max_clients:
                    return False
                self._windows[client_key] = (now, 1)
                return True

            started, count = window
            if count >= self.limit:
                return False
            self._windows[client_key] = (started, count + 1)
            return True


class LocalControlsMiddleware:
    """Bound request bodies, rate, and correlation metadata without extra packages."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_request_bytes: int = 1_048_576,
        rate_limiter: LocalRateLimiter | None = None,
    ) -> None:
        self.app = app
        self.max_request_bytes = max_request_bytes
        self.rate_limiter = rate_limiter or LocalRateLimiter()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        trace_id = _opaque_id(headers.get(b"x-trace-id", b"").decode("latin-1"))
        session_id = _opaque_id(headers.get(b"x-session-id", b"").decode("latin-1"))
        trace_token = trace_id_var.set(trace_id)
        session_token = session_id_var.set(session_id)
        started = time.perf_counter()
        path = scope.get("path", "")
        known_routes = {
            "/health", "/ready", "/version", "/predict", "/rules/search",
            "/weather", "/alternatives", "/answer", "/mcp",
        }
        route = path if path in known_routes else "other"
        client = scope.get("client")
        client_key = str(client[0]) if client else "unknown"
        response_status = 500

        async def send_response(status_code: int, detail: str, reason: str) -> None:
            from starlette.responses import JSONResponse

            response = JSONResponse({"detail": detail}, status_code=status_code)
            response.headers["X-Trace-ID"] = trace_id
            response.headers["X-Session-ID"] = session_id
            record_event("request.rejected", reason=reason, status_code=status_code)
            await response(scope, receive, send)

        try:
            if not self.rate_limiter.allow(client_key):
                await send_response(429, "request rate limit exceeded", "rate_limit")
                return

            content_length = headers.get(b"content-length")
            if content_length is not None:
                try:
                    declared_size = int(content_length)
                except ValueError:
                    await send_response(400, "invalid Content-Length", "invalid_content_length")
                    return
                if declared_size < 0:
                    await send_response(400, "invalid Content-Length", "invalid_content_length")
                    return
                if declared_size > self.max_request_bytes:
                    await send_response(413, "request body is too large", "body_too_large")
                    return

            body = bytearray()
            more_body = True
            while more_body:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                if message["type"] != "http.request":
                    continue
                body.extend(message.get("body", b""))
                if len(body) > self.max_request_bytes:
                    await send_response(413, "request body is too large", "body_too_large")
                    return
                more_body = message.get("more_body", False)

            replayed = False

            async def replay_receive() -> Message:
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            async def send_with_ids(message: Message) -> None:
                nonlocal response_status
                if message["type"] == "http.response.start":
                    response_status = int(message["status"])
                    response_headers = list(message.get("headers", []))
                    response_headers.extend(
                        [(b"x-trace-id", trace_id.encode()), (b"x-session-id", session_id.encode())]
                    )
                    message = {**message, "headers": response_headers}
                await send(message)

            await self.app(scope, replay_receive, send_with_ids)
            record_event(
                "request.completed",
                method=scope.get("method", ""),
                route=route,
                status_code=response_status,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
            )
        finally:
            trace_id_var.reset(trace_token)
            session_id_var.reset(session_token)
