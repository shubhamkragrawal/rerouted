"""Local Streamlit form for the guarded Rerouted answer endpoint."""

from __future__ import annotations

from contextlib import suppress
from datetime import UTC, date, datetime, time
from os import getenv
from typing import Any

import httpx
import streamlit as st

from core.guardrails.output import DISCLOSURE

API_URL = getenv("REROUTED_API_URL", "http://localhost:8000").rstrip("/")
_FACT_CHOICES: dict[str, bool | None] = {"Unknown": None, "Yes": True, "No": False}

st.set_page_config(page_title="Rerouted", page_icon="✈️", layout="centered")
st.title("✈️ Rerouted")
st.caption("Clear, sourced options when a US flight falls apart.")
st.info(DISCLOSURE)

with st.form("disruption"):
    query = st.text_area(
        "What happened?",
        placeholder="My flight was cancelled and I rejected the replacement flight.",
        max_chars=4000,
    )
    left, right = st.columns(2)
    with left:
        flight_date = st.date_input("Flight date", value=date.today())
        carrier = st.text_input("Carrier code", max_chars=3).upper()
        origin = st.text_input("Origin airport", max_chars=3).upper()
    with right:
        flight = st.text_input("Flight number", max_chars=8)
        destination = st.text_input("Destination airport", max_chars=3).upper()
        event = st.selectbox("Disruption", ("cancelled", "delayed", "diverted", "renumbered"))

    st.subheader("Facts for the refund rules")
    fact_left, fact_right = st.columns(2)
    with fact_left:
        covered = st.selectbox("Covered flight under Part 260?", tuple(_FACT_CHOICES))
        merchant = st.selectbox("Was the carrier merchant of record?", tuple(_FACT_CHOICES))
        cancelled = st.selectbox("Was the flight cancelled?", tuple(_FACT_CHOICES))
    with fact_right:
        number_changed = st.selectbox("Did the flight number change?", tuple(_FACT_CHOICES))
        disposition = st.selectbox(
            "What happened to the alternative offer?",
            (
                "unknown",
                "no_alternative_offered",
                "rejected_all",
                "accepted_travel",
                "accepted_compensation",
            ),
        )
        itinerary_kind = st.selectbox("Itinerary type", ("domestic", "international"))

    with st.expander("Optional historical flight and weather context"):
        departure_time = st.time_input("Scheduled departure time", value=time(8, 0))
        include_alternatives = st.checkbox("Search historical BTS alternatives")
        not_before_text = st.text_input("Search departures after (ISO 8601 with timezone)")
        original_arrival_text = st.text_input("Original scheduled arrival (ISO 8601 with timezone)")
        include_weather = st.checkbox("Check weather context")
        weather_start_text = st.text_input("Weather window start (ISO 8601 with timezone)")
        weather_end_text = st.text_input("Weather window end (ISO 8601 with timezone)")
        weather_mode = st.selectbox("Weather source", ("historical", "live"))

    submitted = st.form_submit_button("Explain my options", type="primary")


def _parse_aware(value: str, label: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO 8601 date and time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone, such as Z or -04:00")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


if submitted:
    try:
        request: dict[str, Any] = {
            "query": query,
            "disruption": {
                "carrier": carrier,
                "flight": flight,
                "date": flight_date.isoformat(),
                "origin": origin,
                "dest": destination,
                "event": event,
            },
            "facts": {
                "covered_flight": _FACT_CHOICES[covered],
                "merchant_of_record": _FACT_CHOICES[merchant],
                "cancelled": _FACT_CHOICES[cancelled],
                "flight_number_changed": _FACT_CHOICES[number_changed],
                "consumer_disposition": disposition,
                "itinerary_kind": itinerary_kind,
            },
            "delay_request": {
                "carrier": carrier,
                "origin": origin,
                "dest": destination,
                "flight_date": flight_date.isoformat(),
                "crs_dep_time": departure_time.isoformat(),
                "mode": "local_baseline",
            },
        }
        if include_alternatives:
            request["alternatives_request"] = {
                "origin": origin,
                "dest": destination,
                "not_before": _parse_aware(not_before_text, "Search departure cutoff"),
                "original_scheduled_arrival": _parse_aware(
                    original_arrival_text, "Original scheduled arrival"
                ),
                "max_stops": 1,
                "same_carrier_only": False,
                "flight_date": flight_date.isoformat(),
            }
        if include_weather:
            request["weather_request"] = {
                "airport": origin,
                "window_start": _parse_aware(weather_start_text, "Weather start"),
                "window_end": _parse_aware(weather_end_text, "Weather end"),
                "mode": weather_mode,
            }
        response = httpx.post(f"{API_URL}/answer", json=request, timeout=25.0)
        response.raise_for_status()
        result = response.json()
        if result["status"] == "refusal":
            st.warning(result["answer"])
        else:
            st.success(result["answer"])
        st.subheader("Decision")
        st.write("Refund eligibility", result["entitlement"]["label"])
        st.write("DOT enforcement posture", result["enforcement"]["status"])
        st.write("Posture checked", "Yes" if result["posture_checked"] else "No")
        st.write("Resolver facts", result["entitlement"]["rationale"])

        if result["options"]:
            st.subheader("Historical schedule options")
            st.caption("BTS schedule replay only; these are not live seats or bookable inventory.")
            for index, option in enumerate(result["options"], start=1):
                itinerary = " → ".join(leg["origin"] for leg in option["legs"])
                itinerary += f" → {option['legs'][-1]['dest']}"
                st.write(
                    f"{index}. {itinerary}: {option['total_duration_min']} minutes; "
                    f"arrival delta {option['arrival_delta_vs_original_min']} minutes"
                )

        if result["citations"]:
            st.subheader("Citations")
            for citation in result["citations"]:
                st.markdown(
                    f"- [{citation['clause_id']} · {citation['title']}]({citation['source_url']})"
                )
        if result["posture"]:
            st.subheader("Enforcement notices")
            for posture in result["posture"]:
                st.markdown(
                    f"- [{posture['fr_doc']}]({posture['source_url']}) — {posture['effect']}"
                )
        with st.expander("Tool trace"):
            st.json(result["tool_trace"], expanded=False)
        st.info(result["disclosure"])
    except ValueError as exc:
        st.error(str(exc))
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text
        with suppress(ValueError):
            detail = exc.response.json().get("detail", detail)
        st.error(f"The local API could not complete the request: {detail}")
    except httpx.HTTPError as exc:
        st.error(f"The local API is unavailable: {exc}")

with st.sidebar:
    st.subheader("Local service")
    try:
        health_response = httpx.get(f"{API_URL}/health", timeout=2.0)
        health_response.raise_for_status()
        health = health_response.json()
        if health["status"] == "ready":
            st.success("API and configured data are ready")
        else:
            st.warning("API connected; some local data artifacts are not configured")
        for name, component in health["components"].items():
            st.caption(f"{'✓' if component['ready'] else '○'} {name}: {component['detail']}")
    except httpx.HTTPError:
        st.error("API is not running")
