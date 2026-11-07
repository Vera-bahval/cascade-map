import json

import httpx
import pytest

from finbot.core.data.models import LLMRequest
from finbot.experiments.gemini_observer import GeminiObserverClient
from finbot.experiments.providers import ProviderError

ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent"
)


@pytest.mark.asyncio
async def test_analytics_schema_does_not_force_iteration_judgment(monkeypatch):
    schema = {
        "type": "OBJECT",
        "required": ["analysis"],
        "properties": {"analysis": {"type": "STRING"}},
    }

    def handler(request):
        payload = json.loads(request.content)
        assert payload["generationConfig"]["responseSchema"] == schema
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"text": '{"analysis":"Coverage is incomplete"}'}]},
                    }
                ]
            },
        )

    async def target(url, connection):
        return url, {}, {}

    monkeypatch.setattr("finbot.experiments.gemini_observer._target", target)
    client = GeminiObserverClient(
        {"base_url": ENDPOINT, "api_key": "fixture"},
        {"model": "gemini-flash-latest"},
        response_schema=schema,
    )
    await client.http.aclose()
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        response = await client.chat(
            LLMRequest(messages=[{"role": "user", "content": "Aggregates"}])
        )
        assert json.loads(response.content)["analysis"] == "Coverage is incomplete"
    finally:
        await client.aclose()


async def client_with_transport(monkeypatch, handler):
    async def target(url, connection):
        assert not connection["api_key"]
        return url, {}, {}

    monkeypatch.setattr("finbot.experiments.gemini_observer._target", target)
    records = []
    client = GeminiObserverClient(
        {"base_url": ENDPOINT, "api_key": "private-gemini-key"},
        {"model": "gemini-flash-latest", "max_output_tokens": 2000},
        records.append,
    )
    await client.http.aclose()
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client, records


@pytest.mark.asyncio
async def test_native_gemini_post_header_schema_usage_and_returned_version(monkeypatch):
    def handler(request):
        assert request.method == "POST" and str(request.url) == ENDPOINT
        assert request.headers["x-goog-api-key"] == "private-gemini-key"
        assert "Authorization" not in request.headers
        payload = json.loads(request.content)
        assert payload["systemInstruction"]["parts"][0]["text"] == "Observer instructions"
        assert payload["contents"][0]["parts"][0]["text"] == "Iteration evidence"
        assert payload["generationConfig"]["responseMimeType"] == "application/json"
        return httpx.Response(
            200,
            json={
                "modelVersion": "gemini-flash-specific-version",
                "usageMetadata": {
                    "promptTokenCount": 20,
                    "candidatesTokenCount": 7,
                    "thoughtsTokenCount": 3,
                },
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [
                                {"thought": True, "text": "Not part of the judgment"},
                                {
                                    "text": '{"confidence":0.8,"error_present":null,"reason":"Evidence","evidence":[]}'
                                },
                            ]
                        },
                    }
                ],
            },
        )

    client, records = await client_with_transport(monkeypatch, handler)
    try:
        result = await client.chat(
            LLMRequest(
                messages=[
                    {"role": "system", "content": "Observer instructions"},
                    {"role": "user", "content": "Iteration evidence"},
                ]
            )
        )
    finally:
        await client.aclose()
    assert json.loads(result.content)["confidence"] == 0.8
    assert records[0]["model_returned"] == "gemini-flash-specific-version"
    assert records[0]["usage"]["output_tokens"] == 10
    assert records[0]["usage"]["input_tokens"] == 20
    assert records[0]["usage"]["cached_input_tokens"] is None
    assert "private-gemini-key" not in json.dumps(records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body",
    [
        (429, {"error": {"message": "private-gemini-key"}}),
        (
            200,
            {
                "candidates": [
                    {"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "partial"}]}}
                ]
            },
        ),
        (200, {"promptFeedback": {"blockReason": "SAFETY"}}),
    ],
)
async def test_errors_and_incomplete_responses_are_recorded_without_retries(
    monkeypatch, status, body
):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=body)

    client, records = await client_with_transport(monkeypatch, handler)
    try:
        with pytest.raises(ProviderError):
            await client.chat(LLMRequest(messages=[{"role": "user", "content": "Evidence"}]))
    finally:
        await client.aclose()
    assert len(calls) == len(records) == 1
    assert not records[0]["success"]
    assert "private-gemini-key" not in json.dumps(records)


def test_model_and_endpoint_must_match():
    with pytest.raises(ProviderError, match="match"):
        GeminiObserverClient({"base_url": ENDPOINT, "api_key": "key"}, {"model": "other-model"})


@pytest.mark.asyncio
async def test_proxy_mode_preserves_endpoint_hostname_and_does_not_pin_dns(monkeypatch):
    async def unexpected_target(*args, **kwargs):
        raise AssertionError("Proxy must resolve the original endpoint")

    monkeypatch.setattr("finbot.experiments.gemini_observer._target", unexpected_target)
    client = GeminiObserverClient(
        {"base_url": ENDPOINT, "api_key": "proxy-mode-key", "use_env_proxy": True},
        {"model": "gemini-flash-latest"},
    )
    await client.http.aclose()

    def handler(request):
        assert str(request.url) == ENDPOINT
        assert request.headers["x-goog-api-key"] == "proxy-mode-key"
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "{}"}]}}]},
        )

    client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        response = await client.chat(LLMRequest(messages=[{"role": "user", "content": "Evidence"}]))
        assert response.content == "{}"
    finally:
        await client.aclose()
