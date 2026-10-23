"""FastAPI entry point for local Rerouted tools and guarded answer composition."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict
from starlette.responses import JSONResponse

from api.answer import AnswerRequest, AnswerResponse, AnswerService
from api.telemetry import LocalControlsMiddleware, LocalRateLimiter, record_event
from core.contracts.tools import (
    DelayRiskRequest,
    DelayRiskResponse,
    FindAlternativesRequest,
    FindAlternativesResponse,
    SearchRulesRequest,
    SearchRulesResponse,
    WeatherContextRequest,
    WeatherContextResponse,
)
from domains.air_us.bootstrap import AirUSServices


class ComponentHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ready: bool
    detail: str


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ready", "degraded"]
    service: Literal["rerouted-api"] = "rerouted-api"
    version: str
    components: dict[str, ComponentHealth]


class VersionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service: Literal["rerouted-api"] = "rerouted-api"
    version: str


def create_app(
    services: AirUSServices | None = None,
    *,
    max_request_bytes: int = 1_048_576,
    rate_limit_requests: int = 120,
    rate_limit_window_seconds: float = 60.0,
) -> FastAPI:
    """Build the API with injectable local services for tests and offline replay."""

    runtime = services or AirUSServices()
    answer_service = AnswerService(runtime)

    # Import here so importing contracts or the ingestion modules does not need to
    # start a transport or load model/data artifacts.
    from core.mcp.server import create_mcp_server

    mcp_server = create_mcp_server(runtime.tool_handlers())

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with mcp_server.session_manager.run():
            yield

    application = FastAPI(
        title="Rerouted API",
        description="Citation-grounded help for US flight disruptions.",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.services = runtime
    application.state.answer_service = answer_service
    application.state.mcp_server = mcp_server
    application.add_middleware(
        LocalControlsMiddleware,
        max_request_bytes=max_request_bytes,
        rate_limiter=LocalRateLimiter(
            limit=rate_limit_requests,
            window_seconds=rate_limit_window_seconds,
        ),
    )

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        del request
        record_event("request.validation_failed", error_count=len(exc.errors()))
        return JSONResponse(
            status_code=422,
            content=jsonable_encoder({"detail": exc.errors()}),
        )

    @application.get("/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        components = {
            name: ComponentHealth(ready=state.ready, detail=state.detail)
            for name, state in runtime.readiness.items()
        }
        components["mcp"] = ComponentHealth(
            ready=True, detail="official MCP Python SDK v2 server configured"
        )
        status: Literal["ready", "degraded"] = (
            "ready" if all(state.ready for state in components.values()) else "degraded"
        )
        return HealthResponse(status=status, version=application.version, components=components)

    @application.get("/ready", response_model=HealthResponse, tags=["system"])
    async def readiness() -> JSONResponse:
        health_response = await health()
        required = (
            "airport_scope",
            "search_rules",
            "posture",
            "delay_risk",
            "weather_historical",
            "alternatives",
        )
        ready = all(
            name in health_response.components and health_response.components[name].ready
            for name in required
        )
        readiness_response = health_response.model_copy(
            update={"status": "ready" if ready else "degraded"}
        )
        return JSONResponse(
            status_code=200 if ready else 503,
            content=readiness_response.model_dump(mode="json"),
        )

    @application.get("/version", response_model=VersionResponse, tags=["system"])
    async def version() -> VersionResponse:
        return VersionResponse(version=application.version)

    @application.post("/predict", response_model=DelayRiskResponse, tags=["tools"])
    async def predict(request: DelayRiskRequest) -> DelayRiskResponse:
        try:
            return runtime.predict(request)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @application.post("/rules/search", response_model=SearchRulesResponse, tags=["tools"])
    async def rules_search(request: SearchRulesRequest) -> SearchRulesResponse:
        if not runtime.readiness["search_rules"].ready:
            raise HTTPException(status_code=503, detail=runtime.readiness["search_rules"].detail)
        return runtime.search_rules(request)

    @application.post("/weather", response_model=WeatherContextResponse, tags=["tools"])
    async def weather(request: WeatherContextRequest) -> WeatherContextResponse:
        try:
            return runtime.weather(request)
        except (RuntimeError, ValueError, OSError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"weather source request failed: {exc}"
            ) from exc

    @application.post("/alternatives", response_model=FindAlternativesResponse, tags=["tools"])
    async def alternatives(request: FindAlternativesRequest) -> FindAlternativesResponse:
        try:
            return runtime.alternatives(request)
        except (RuntimeError, ValueError, OSError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @application.post("/answer", response_model=AnswerResponse, tags=["answer"])
    async def answer(request: AnswerRequest) -> AnswerResponse:
        response = answer_service.answer(request)
        search_result = next(
            (item for item in response.tool_trace if item.tool == "search_rules"), None
        )
        ranked_clause_ids: list[str] = []
        retrieval_diagnostics: dict[str, str | bool | None] = {}
        if search_result is not None and search_result.result is not None:
            clauses = search_result.result.get("clauses", [])
            if isinstance(clauses, list):
                ranked_clause_ids = [
                    clause["clause_id"]
                    for clause in clauses
                    if isinstance(clause, dict) and isinstance(clause.get("clause_id"), str)
                ]
            retrieval_diagnostics = {
                "retrieval_provider": search_result.result.get("retrieval_provider"),
                "retrieval_model": search_result.result.get("retrieval_model"),
                "retrieval_fallback_used": bool(
                    search_result.result.get("retrieval_fallback_reason")
                ),
            }
        record_event(
            "answer.completed",
            planner_version="local-tool-planner-v1",
            tool_names=[item.tool for item in response.tool_trace],
            tool_error_count=sum(item.error is not None for item in response.tool_trace),
            ranked_clause_ids=ranked_clause_ids,
            resolver_status=response.resolver_status,
            cited_clause_ids=[item.clause_id for item in response.citations],
            disposition=response.status,
            refusal_reason=response.refusal_reason,
            retry_count=0,
            token_count=None,
            estimated_cost_usd=0.0,
            cache_behavior="not_applicable",
            **retrieval_diagnostics,
        )
        return response

    # MCP's transport app owns /mcp. Its mount is last so it cannot shadow the
    # explicit REST routes. The root lifespan starts the mounted server's manager.
    application.mount("/", mcp_server.streamable_http_app())
    return application


app = create_app()
