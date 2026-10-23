from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from api.main import app, create_app
from api.telemetry import LocalRateLimiter


def test_readiness_and_version_endpoints_report_local_artifact_state() -> None:
    client = TestClient(app)

    readiness = client.get("/ready")
    payload = readiness.json()
    required = (
        "airport_scope",
        "search_rules",
        "posture",
        "delay_risk",
        "weather_historical",
        "alternatives",
    )
    expected_ready = all(payload["components"][name]["ready"] for name in required)
    assert readiness.status_code == (200 if expected_ready else 503)
    assert payload["status"] == ("ready" if expected_ready else "degraded")
    assert client.get("/version").json() == {"service": "rerouted-api", "version": "0.1.0"}


def test_trace_and_session_ids_are_returned_and_validation_logs_are_redacted(
    caplog: object,
) -> None:
    from uuid import uuid4

    trace_id = str(uuid4())
    session_id = str(uuid4())
    caplog.set_level("INFO", logger="rerouted.telemetry")  # type: ignore[attr-defined]
    response = TestClient(app).post(
        "/answer",
        headers={
            "X-Trace-ID": trace_id,
            "X-Session-ID": session_id,
            "Content-Type": "application/json",
        },
        content='{"query":"booking locator BOOKING-SECRET"',
    )

    assert response.status_code == 422
    assert response.headers["X-Trace-ID"] == trace_id
    assert response.headers["X-Session-ID"] == session_id
    log_text = "\n".join(record.getMessage() for record in caplog.records)  # type: ignore[attr-defined]
    events = [json.loads(line) for line in log_text.splitlines() if line.startswith("{")]
    assert any(event["event"] == "request.validation_failed" for event in events)
    assert "BOOKING-SECRET" not in log_text


def test_request_size_is_bounded() -> None:
    limited = create_app(
        services=app.state.services,
        max_request_bytes=32,
    )
    response = TestClient(limited).post(
        "/answer",
        content="{" + ("x" * 32),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["detail"] == "request body is too large"
    assert response.headers.get("X-Trace-ID")


def test_local_rate_limit_has_deterministic_fixed_window_reset() -> None:
    now = [100.0]
    limiter = LocalRateLimiter(limit=2, window_seconds=10, clock=lambda: now[0])

    assert limiter.allow("127.0.0.1") is True
    assert limiter.allow("127.0.0.1") is True
    assert limiter.allow("127.0.0.1") is False
    now[0] = 110.0
    assert limiter.allow("127.0.0.1") is True


def test_http_rate_limit_returns_429() -> None:
    limited = create_app(services=app.state.services, rate_limit_requests=1)
    client = TestClient(limited)

    assert client.get("/version").status_code == 200
    throttled = client.get("/version")
    assert throttled.status_code == 429
    assert throttled.headers.get("X-Trace-ID")


def test_weather_failure_is_returned_without_logging_exception_details(
    monkeypatch: object, caplog: object
) -> None:
    def fail_weather(request: object) -> None:
        del request
        raise RuntimeError("upstream-secret-contact@example.test")

    monkeypatch.setattr(app.state.services, "weather", fail_weather)  # type: ignore[attr-defined]
    caplog.set_level("INFO", logger="rerouted.telemetry")  # type: ignore[attr-defined]
    response = TestClient(app).post(
        "/weather",
        json={
            "airport": "JFK",
            "window_start": datetime(2026, 3, 14, 12, tzinfo=UTC).isoformat(),
            "window_end": datetime(2026, 3, 14, 13, tzinfo=UTC).isoformat(),
            "mode": "historical",
        },
    )
    assert response.status_code == 503
    assert "upstream-secret-contact@example.test" not in "\n".join(
        record.getMessage() for record in caplog.records  # type: ignore[attr-defined]
    )
