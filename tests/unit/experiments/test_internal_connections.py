import asyncio
import json
import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from finbot.core.data.models import LLMRequest
from finbot.experiments import providers as p
from finbot.experiments.api import get_store
from finbot.experiments.executor import isolated_business_database
from finbot.experiments.schemas import ConnectionInput
from finbot.experiments.store import Store

INTERNAL = {
    "name": "Work vLLM",
    "kind": "chat_completions",
    "base_url": "http://server:8100/v1",
    "allow_private_network": True,
    "api_key": "private-test-key",
}


@pytest.mark.parametrize("url", ["http://server:8100/v1", "https://10.20.30.40:8100/v1"])
def test_internal_access_is_explicit_and_connection_scoped(url):
    connection = {**INTERNAL, "base_url": url}
    assert p.validate_connection(connection)["base_url"] == url
    with pytest.raises(p.ProviderError, match="Внутренний сервер"):
        p.validate_connection({**connection, "allow_private_network": False})
    assert not ConnectionInput(name="Public", kind="chat_completions").allow_private_network


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/v1",
        "http://[::ffff:169.254.169.254]/v1",
        "http://metadata.google.internal/v1",
        "http://100.100.100.200/v1",
        "http://[fd00:ec2::254]/v1",
        "http://0.0.0.0/v1",
        "http://224.0.0.1/v1",
        "http://8.8.8.8/v1",
        "http://user:password@server:8100/v1",
    ],
)
def test_internal_permission_does_not_allow_special_or_public_addresses(url):
    with pytest.raises(p.ProviderError):
        p.validate_connection({**INTERNAL, "base_url": url})


def resolve_to(monkeypatch, addresses):
    async def resolver(*args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 8100)) for address in addresses
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)


@pytest.mark.asyncio
async def test_private_dns_is_checked_for_public_and_internal_connections(monkeypatch):
    resolve_to(monkeypatch, ["10.20.30.40"])
    assert await p._check_dns(INTERNAL["base_url"], allow_private_network=True) == "10.20.30.40"
    with pytest.raises(p.ProviderError, match="Внутренний сервер"):
        await p._check_dns("https://server:8100/v1")


@pytest.mark.parametrize(
    "bad_address", ["169.254.169.254", "100.100.100.200", "fd00:ec2::254", "8.8.8.8"]
)
@pytest.mark.asyncio
async def test_internal_dns_rejects_any_forbidden_answer(monkeypatch, bad_address):
    resolve_to(monkeypatch, ["10.20.30.40", bad_address])
    with pytest.raises(p.ProviderError):
        await p._target(INTERNAL["base_url"] + "/models", INTERNAL)


@pytest.mark.asyncio
async def test_internal_catalogue_probe_and_generation_keep_permission_and_pin_dns(monkeypatch):
    resolve_to(monkeypatch, ["10.20.30.40"])
    seen = []

    def handler(request):
        seen.append(request)
        assert request.url.host == "10.20.30.40"
        assert request.url.port == 8100
        assert request.headers["host"] == "server:8100"
        assert request.headers["authorization"] == "Bearer private-test-key"
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "work-model"}]})
        assert request.url.path == "/v1/chat/completions"
        data = json.loads(request.content)
        assert data["model"] == "work-model"
        messages = data["messages"]
        if messages[-1]["role"] == "tool":
            message = {
                "role": "assistant",
                "content": json.loads(messages[-1]["content"])["probe_value"],
            }
        elif data.get("tools"):
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "probe", "function": {"name": "connection_probe", "arguments": "{}"}}
                ],
            }
        else:
            message = {"role": "assistant", "content": "Ready"}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        p.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    assert (await p.discover_models(INTERNAL))[0]["id"] == "work-model"
    assert (await p.test_model(INTERNAL, {"model": "work-model"}))["status"] == "ready"
    client = p.ExperimentLLMClient(INTERNAL, {"model": "work-model"})
    try:
        response = await client.chat(LLMRequest(messages=[{"role": "user", "content": "Hello"}]))
        assert response.content == "Ready"
    finally:
        await client.aclose()
    assert len(seen) == 4


@pytest.mark.asyncio
async def test_internal_catalogue_does_not_follow_redirects(monkeypatch):
    resolve_to(monkeypatch, ["10.20.30.40"])
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        p.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    with pytest.raises(p.ProviderError, match="HTTP 302"):
        await p.discover_models(INTERNAL)
    assert len(seen) == 1


def test_mounted_save_reports_validation_and_preserves_internal_permission(tmp_path):
    import re
    from finbot.main import app, vendor_app

    store = Store(tmp_path / "experiments.db")
    vendor_app.dependency_overrides[get_store] = lambda: store
    try:
        with isolated_business_database():
            client = TestClient(app, raise_server_exceptions=False)
            page = client.get("/vendor/experiments")
            token = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text)[1]
            client.headers["X-CSRF-Token"] = token
            base = "/vendor/api/v1/experiments"
            rejected = client.post(
                base + "/connections", json={**INTERNAL, "allow_private_network": False}
            )
            assert rejected.status_code == 422
            assert rejected.headers["content-type"].startswith("application/json")
            assert "Внутренний сервер" in rejected.json()["error"]["message"]
            saved = client.post(base + "/connections", json=INTERNAL)
            assert saved.status_code == 201, saved.text
            assert saved.json()["allow_private_network"] is True
            assert "private-test-key" not in saved.text
            config = client.post(
                base + "/configs", json={"connection_id": saved.json()["id"], "model": "work-model"}
            )
            assert config.status_code == 201, config.text
            run = client.post(base + "/runs", json={"config_id": config.json()["id"]})
            assert run.status_code == 202, run.text
            claim = store.claim_job("test-worker")
            assert claim["connection"]["allow_private_network"] is True
            assert claim["connection"]["base_url"] == INTERNAL["base_url"]
            assert claim["connection"]["api_key"] == INTERNAL["api_key"]
    finally:
        vendor_app.dependency_overrides.pop(get_store, None)
