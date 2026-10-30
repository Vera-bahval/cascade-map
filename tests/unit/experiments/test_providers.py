import asyncio
import copy
import json

import httpx
import pytest

from finbot.core.data.models import LLMRequest
from finbot.core.llm import client as client_module
from finbot.experiments import providers as p

CONNECTION = {
    "kind": "chat_completions",
    "base_url": "https://provider.example/v1",
    "api_key": "secret-test-key",
}


def test_invalid_usage_never_becomes_negative_cost_or_fake_tokens():
    counts = p._usage(
        {
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": -1,
                "prompt_tokens_details": {"cached_tokens": 20},
                "completion_tokens_details": {"reasoning_tokens": "5"},
            }
        }
    )
    assert counts == {
        "input_tokens": 10,
        "output_tokens": None,
        "cached_input_tokens": None,
        "reasoning_tokens": None,
    }
    assert p._usage({"eval_count": True}, native=True)["output_tokens"] is None


TOOL = {"type": "function", "name": "lookup", "parameters": {"type": "object", "properties": {}}}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    async def resolve(_, **kwargs):
        return "8.8.8.8"

    monkeypatch.setattr(p, "_check_dns", resolve)


def response(message, usage=None):
    return httpx.Response(
        200, json={"choices": [{"message": message, "finish_reason": "stop"}], "usage": usage or {}}
    )


def configured(handler, config=None, kind="chat_completions", callback=None):
    client = p.ExperimentLLMClient(
        {**CONNECTION, "kind": kind}, config or {"model": "selected", "temperature": 0}, callback
    )
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


@pytest.mark.asyncio
async def test_parallel_tool_roundtrip_canonical_history_and_usage():
    seen, records = [], []

    def handler(req):
        payload = json.loads(req.content)
        seen.append(payload)
        assert req.headers["host"] == "provider.example"
        assert req.url.host == "8.8.8.8"
        assert req.extensions["sni_hostname"] == "provider.example"
        if len(seen) == 1:
            return response(
                {
                    "role": "assistant",
                    "content": "Checking",
                    "tool_calls": [
                        {
                            "id": "a",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                        },
                        {
                            "id": "b",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"id":2}'},
                        },
                    ],
                },
                {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "prompt_tokens_details": {"cached_tokens": 3},
                },
            )
        return response({"role": "assistant", "content": "Finished"})

    client = configured(handler, callback=records.append)
    initial = LLMRequest(
        messages=[{"role": "user", "content": "Check"}], tools=[TOOL], model="ignored"
    )
    untouched = initial.model_dump()
    first = await client.chat(initial)
    history = first.messages + [
        {"type": "function_call_output", "call_id": "a", "output": "one"},
        {"type": "function_call_output", "call_id": "b", "output": "two"},
    ]
    second = await client.chat(LLMRequest(messages=history, tools=[TOOL]))
    await client.aclose()
    assert initial.model_dump() == untouched
    assert seen[0]["model"] == "selected" and seen[0]["temperature"] == 0
    assert seen[0]["tools"][0]["function"]["name"] == "lookup"
    assert len(seen[1]["messages"][1]["tool_calls"]) == 2
    assert [m["tool_call_id"] for m in seen[1]["messages"][-2:]] == ["a", "b"]
    assert second.content == "Finished"
    assert records[0]["usage"]["cached_input_tokens"] == 3
    assert records[0]["usage"]["reasoning_tokens"] is None
    assert "secret-test-key" not in json.dumps(records)


@pytest.mark.asyncio
async def test_responses_full_history_encrypted_reasoning_and_no_previous_id():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        if len(seen) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "r1",
                    "status": "completed",
                    "output": [
                        {
                            "type": "reasoning",
                            "id": "reasoning1",
                            "encrypted_content": "ciphertext",
                            "summary": [],
                        },
                        {
                            "type": "function_call",
                            "name": "lookup",
                            "call_id": "c1",
                            "arguments": "{}",
                        },
                    ],
                    "usage": {
                        "input_tokens": 20,
                        "output_tokens": 30,
                        "output_tokens_details": {"reasoning_tokens": 15},
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ]
            },
        )

    client = configured(handler, {"model": "reasoner", "reasoning_effort": "low"}, "responses")
    first = await client.chat(
        LLMRequest(messages=[{"role": "user", "content": "lookup"}], tools=[TOOL])
    )
    await client.chat(
        LLMRequest(
            messages=first.messages
            + [{"type": "function_call_output", "call_id": "c1", "output": "ok"}],
            previous_response_id="r1",
        )
    )
    await client.aclose()
    assert seen[0]["include"] == ["reasoning.encrypted_content"]
    assert not seen[0]["store"] and "temperature" not in seen[0]
    assert "previous_response_id" not in seen[1]
    assert seen[1]["input"][1]["encrypted_content"] == "ciphertext"
    assert first.metadata["usage"]["reasoning_tokens"] == 15


@pytest.mark.asyncio
async def test_ollama_native_thinking_and_tool_arguments():
    seen = []

    def handler(req):
        assert req.url.path == "/api/chat"
        seen.append(json.loads(req.content))
        return httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": "",
                    "thinking": "check",
                    "tool_calls": [{"function": {"name": "lookup", "arguments": {}}}],
                },
                "done": True,
                "prompt_eval_count": 12,
                "eval_count": 4,
            },
        )

    client = configured(
        handler, {"model": "local", "thinking": True, "top_k": 20, "min_p": 0}, "ollama"
    )
    result = await client.chat(
        LLMRequest(messages=[{"role": "user", "content": "hi"}], tools=[TOOL])
    )
    await client.chat(
        LLMRequest(
            messages=result.messages
            + [
                {
                    "type": "function_call_output",
                    "call_id": result.tool_calls[0]["call_id"],
                    "output": "ok",
                }
            ],
            tools=[TOOL],
        )
    )
    await client.aclose()
    assert seen[0]["think"] is True and seen[0]["options"]["top_k"] == 20
    assert seen[1]["messages"][1]["tool_calls"][0]["function"]["arguments"] == {}
    assert seen[1]["messages"][1]["thinking"] == "check"


@pytest.mark.asyncio
async def test_unsupported_parameters_not_silently_removed():
    client = configured(
        lambda _: pytest.fail("Must not send request"), {"model": "x", "seed": 10}, "responses"
    )
    with pytest.raises(p.ProviderError, match="does not support"):
        await client.chat(LLMRequest(messages=[]))
    await client.aclose()


@pytest.mark.asyncio
async def test_errors_and_echoed_credentials_are_redacted():
    records = []
    client = configured(
        lambda _: httpx.Response(401, text="secret-test-key"), callback=records.append
    )
    with pytest.raises(p.ProviderError, match="HTTP 401") as error:
        await client.chat(LLMRequest(messages=[]))
    await client.aclose()
    assert "secret-test-key" not in str(error.value) + json.dumps(records)
    client = configured(
        lambda _: response({"role": "assistant", "content": "secret-test-key"}),
        callback=records.append,
    )
    await client.chat(LLMRequest(messages=[{"role": "user", "content": "secret-test-key"}]))
    await client.aclose()
    assert "secret-test-key" not in json.dumps(records)
    assert records[-1]["response"]["choices"][0]["message"]["content"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_truncation_is_technical_failure_but_usage_preserved():
    records = []
    client = configured(
        lambda _: httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "truncated"}, "finish_reason": "length"}],
                "usage": {"completion_tokens": 20},
            },
        ),
        callback=records.append,
    )
    with pytest.raises(p.ProviderError, match="token limit"):
        await client.chat(LLMRequest(messages=[]))
    await client.aclose()
    assert not records[0]["success"] and records[0]["usage"]["output_tokens"] == 20


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/v1",
        "https://user:pass@example.com/v1",
        "https://example.com?key=x",
        "https://example.com/#fragment",
        "http://169.254.169.254",
        "https://[::ffff:169.254.169.254]",
        "https://10.0.0.1",
        "https://100.100.100.200",
        "https://metadata.google.internal",
        "file:///etc/passwd",
        "https://0.0.0.0",
        "http://localhost@evil.example",
    ],
)
def test_unsafe_endpoints_rejected(url):
    with pytest.raises(p.ProviderError):
        p.validate_base_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434",
        "http://127.0.0.1:8000/v1",
        "http://[::1]:8000",
        "http://host.docker.internal:11434",
        "https://api.example.com/v1",
    ],
)
def test_supported_explicit_endpoints(url):
    assert p.validate_base_url(url) == url


@pytest.mark.asyncio
async def test_context_override_is_task_local_and_restored(monkeypatch):
    default = object()
    monkeypatch.setattr(client_module, "_default_client", default)
    a, b = object(), object()

    async def operation(client):
        with client_module.use_llm_client(client):
            await asyncio.sleep(0)
            assert client_module.get_llm_client() is client

            async def child():
                return client_module.get_llm_client()

            assert await asyncio.create_task(child()) is client

    await asyncio.gather(operation(a), operation(b))
    assert client_module.get_llm_client() is default


@pytest.mark.asyncio
async def test_probe_requires_actual_tool_result_continuation(monkeypatch):
    def handler(req):
        payload = json.loads(req.content)
        messages = payload["messages"]
        if messages[-1]["role"] == "tool":
            probe = json.loads(messages[-1]["content"])["probe_value"]
            return response({"role": "assistant", "content": probe})
        return response(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "probe", "function": {"name": "connection_probe", "arguments": "{}"}}
                ],
            }
        )

    original = p.ExperimentLLMClient

    def factory(*args, **kwargs):
        client = original(*args, **kwargs)
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return client

    monkeypatch.setattr(p, "ExperimentLLMClient", factory)
    result = await p.test_model(CONNECTION, {"model": "test"})
    assert result["status"] == "ready" and result["tool_continuation"]
    assert len(result["calls"]) == 2


@pytest.mark.asyncio
async def test_plain_text_probe_does_not_claim_tool_support(monkeypatch):
    original = p.ExperimentLLMClient

    def factory(*args, **kwargs):
        client = original(*args, **kwargs)
        client.http = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: response({"role": "assistant", "content": "I support tools"})
            )
        )
        return client

    monkeypatch.setattr(p, "ExperimentLLMClient", factory)
    result = await p.test_model(CONNECTION, {"model": "test"})
    assert result["status"] == "incompatible" and not result["tool_call"]


@pytest.mark.asyncio
async def test_catalogue_and_ollama_download_stream(monkeypatch):
    seen, progress = [], []

    def handler(req):
        seen.append(req)
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen:8b", "size": 500}]})
        if req.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "available"}]})
        assert req.url.path == "/api/pull"
        assert json.loads(req.content) == {"model": "qwen:8b", "stream": True}
        return httpx.Response(
            200, text='{"status":"downloading","completed":10,"total":20}\n{"status":"success"}\n'
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        p.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler))
    )
    assert (await p.discover_models(CONNECTION))[0]["id"] == "available"
    local = {"kind": "ollama", "base_url": "http://localhost:11434/v1"}
    assert (await p.discover_models(local))[0]["id"] == "qwen:8b"
    result = await p.pull_model(local, "qwen:8b", progress.append)
    assert result["status"] == "downloaded"
    assert progress[0]["completed"] == 10 and progress[-1]["status"] == "success"


@pytest.mark.asyncio
async def test_dns_rejects_metadata_resolution(monkeypatch):
    # Restore DNS validation without reloading active clients.
    async def resolver(*args, **kwargs):
        return [(2, 1, 6, "", ("169.254.169.254", 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
    with pytest.raises(p.ProviderError, match="Metadata"):
        await REAL_CHECK_DNS("https://provider.example/v1")


REAL_CHECK_DNS = p._check_dns
