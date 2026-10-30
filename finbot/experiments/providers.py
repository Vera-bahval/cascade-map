"""Experiment adapters. Errors omit provider bodies, which may echo credentials."""

import asyncio
import copy
import inspect
import ipaddress
import json
import socket
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

import httpx

from finbot.core.data.models import LLMRequest, LLMResponse


class ProviderError(ValueError):
    """Safe-to-display connection, parameter or model protocol error."""


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}


def validate_base_url(url, *, allow_private_network=False):
    try:
        parts = urlsplit(url)
        host = parts.hostname
        _ = parts.port
    except (ValueError, TypeError):
        raise ProviderError("Invalid endpoint URL") from None
    if (
        parts.scheme not in {"http", "https"}
        or not host
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise ProviderError("Use an http(s) base URL without credentials, query or fragment")
    if host.lower().rstrip(".") in {"metadata.google.internal", "metadata", "instance-data"}:
        raise ProviderError("Cloud metadata endpoints are not allowed")
    local = host.lower().rstrip(".") in LOCAL_HOSTS or allow_private_network
    if parts.scheme == "http" and not local:
        raise ProviderError(
            "Для HTTP-подключения к рабочей сети включите «Внутренний сервер». "
            "Для внешнего API нужен HTTPS."
        )
    _check_address(host, allow_private=local)
    if local:
        _check_internal_address(host)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def validate_connection(connection):
    result = copy.deepcopy(connection)
    if result.get("kind") not in {"chat_completions", "responses", "ollama", "mock"}:
        raise ProviderError("Unsupported provider protocol")
    if result["kind"] != "mock":
        result["base_url"] = validate_base_url(
            result.get("base_url", ""),
            allow_private_network=result.get("allow_private_network", False),
        )
    return result


def _check_address(host, allow_private=False):
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if (
        address.is_link_local
        or address.is_unspecified
        or address.is_multicast
        or str(address) in {"100.100.100.200", "192.0.0.192", "fd00:ec2::254"}
    ):
        raise ProviderError("Metadata, link-local and unspecified endpoints are not allowed")
    if not allow_private and not address.is_global:
        raise ProviderError("Для адреса в рабочей сети включите «Внутренний сервер».")


def _check_internal_address(host):
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_private:
        raise ProviderError(
            "Адрес внутреннего сервера должен вести в локальную или рабочую сеть. "
            "Для публичного адреса отключите «Внутренний сервер» и используйте HTTPS."
        )


async def _check_dns(base_url, *, allow_private_network=False):
    parts = urlsplit(base_url)
    try:
        addresses = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                parts.hostname,
                parts.port or (443 if parts.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            ),
            timeout=15,
        )
    except (OSError, TimeoutError):
        raise ProviderError(
            "Не удалось определить адрес сервера. Проверьте имя и доступность DNS "
            "на машине или в контейнере, где запущен FinBot."
        ) from None
    if not addresses:
        raise ProviderError("Endpoint hostname has no addresses")
    local = parts.hostname.lower().rstrip(".") in LOCAL_HOSTS or allow_private_network
    for entry in addresses:
        _check_address(entry[4][0], allow_private=local)
        if local:
            _check_internal_address(entry[4][0])
    return addresses[0][4][0]


async def _target(url, connection):
    # Pin the validated DNS address so DNS rebinding cannot redirect credentials.
    address = await _check_dns(
        url, allow_private_network=connection.get("allow_private_network", False)
    )
    original = httpx.URL(url)
    headers = {**_headers(connection), "Host": original.netloc.decode("ascii")}
    return original.copy_with(host=address), headers, {"sni_hostname": original.host}


def _endpoint(connection, suffix):
    base = validate_base_url(
        connection.get("base_url", ""),
        allow_private_network=connection.get("allow_private_network", False),
    )
    if connection.get("kind") == "ollama":
        parts = urlsplit(base)
        path = parts.path.rstrip("/")
        if path.endswith("/v1"):
            path = path[:-3]
        return urlunsplit((parts.scheme, parts.netloc, path + suffix, "", ""))
    return base + suffix


def _headers(connection):
    key = connection.get("api_key")
    return {"Authorization": f"Bearer {key}"} if key else {}


def _scrub(value, secret):
    if isinstance(value, dict):
        return {
            k: (
                "[REDACTED]"
                if k.lower() in {"authorization", "api_key", "access_token"}
                else _scrub(v, secret)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub(v, secret) for v in value]
    if isinstance(value, str) and secret:
        return value.replace(secret, "[REDACTED]")
    return value


async def _notify(callback, value):
    if callback:
        result = callback(value)
        if inspect.isawaitable(result):
            await result


def _arguments(raw):
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        raise ProviderError("Model returned invalid JSON tool arguments") from None
    if not isinstance(value, dict):
        raise ProviderError("Model returned non-object tool arguments")
    return value


def chat_messages(history):
    """Translate Responses items, grouping parallel calls into one assistant turn."""
    messages = []
    for source in history:
        item = copy.deepcopy(source)
        kind = item.get("type")
        if kind == "function_call":
            call = {
                "id": item["call_id"],
                "type": "function",
                "function": {
                    "name": item["name"],
                    "arguments": (
                        item["arguments"]
                        if isinstance(item["arguments"], str)
                        else json.dumps(item["arguments"])
                    ),
                },
            }
            if not messages or messages[-1].get("role") != "assistant":
                messages.append({"role": "assistant", "content": None})
            messages[-1].setdefault("tool_calls", []).append(call)
        elif kind == "function_call_output":
            output = item.get("output", "")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item["call_id"],
                    "content": output if isinstance(output, str) else json.dumps(output),
                }
            )
        elif kind == "reasoning":
            # Responses-only reasoning items must not be misrepresented as user input.
            continue
        elif "role" in item:
            messages.append(
                {
                    k: v
                    for k, v in item.items()
                    if k
                    in {
                        "role",
                        "content",
                        "tool_calls",
                        "tool_call_id",
                        "name",
                        "reasoning_content",
                    }
                }
            )
        else:
            raise ProviderError("Unsupported conversation history item")
    return messages


def _tools(tools):
    return [
        (
            copy.deepcopy(t)
            if "function" in t
            else {"type": "function", "function": {k: v for k, v in t.items() if k != "type"}}
        )
        for t in (tools or [])
    ]


def _usage(data, native=False):
    if native:
        result = {
            "input_tokens": data.get("prompt_eval_count"),
            "output_tokens": data.get("eval_count"),
            "cached_input_tokens": None,
            "reasoning_tokens": None,
        }
    else:
        usage = data.get("usage") or {}
        inputs = usage.get("input_tokens", usage.get("prompt_tokens"))
        outputs = usage.get("output_tokens", usage.get("completion_tokens"))
        in_details = usage.get("input_tokens_details", usage.get("prompt_tokens_details")) or {}
        out_details = (
            usage.get("output_tokens_details", usage.get("completion_tokens_details")) or {}
        )
        result = {
            "input_tokens": inputs,
            "output_tokens": outputs,
            "cached_input_tokens": in_details.get("cached_tokens"),
            "reasoning_tokens": out_details.get("reasoning_tokens"),
        }
    # Malformed usage must not produce negative costs or pretend to be measured.
    result = {
        key: value if type(value) is int and value >= 0 else None for key, value in result.items()
    }
    for subset, total in (
        ("cached_input_tokens", "input_tokens"),
        ("reasoning_tokens", "output_tokens"),
    ):
        if (
            result[subset] is not None
            and result[total] is not None
            and result[subset] > result[total]
        ):
            result[subset] = None
    return result


class ExperimentLLMClient:
    provider = "http"

    def __init__(self, connection, config, on_call=None):
        self.connection = validate_connection(connection)
        self.config = copy.deepcopy(config)
        self.default_model = config["model"]
        self.default_temperature = config.get("temperature")
        self.on_call = on_call
        self.kind = connection["kind"]
        if self.kind not in {"chat_completions", "responses", "ollama", "mock"}:
            raise ProviderError("Unsupported provider protocol")
        self.http = httpx.AsyncClient(
            timeout=config.get("timeout_seconds", 60), follow_redirects=False, trust_env=False
        )

    async def aclose(self):
        await self.http.aclose()

    def _payload(self, request):
        config = self.config
        params = {
            k: config[k]
            for k in ("temperature", "top_p", "top_k", "min_p", "seed")
            if config.get(k) is not None
        }
        thinking = config.get("thinking")
        effort = config.get("reasoning_effort")
        maximum = config.get("max_output_tokens", 5000)
        history = copy.deepcopy(request.messages or [])
        if self.kind == "responses":
            unsupported = set(params) & {"top_k", "min_p", "seed"}
            if unsupported or thinking is not None:
                raise ProviderError(
                    "Responses protocol does not support top_k, min_p, seed or thinking; use reasoning_effort"
                )
            payload = {
                "model": self.default_model,
                "input": history,
                "store": False,
                "max_output_tokens": maximum,
                "include": ["reasoning.encrypted_content"],
                **params,
            }
            if effort:
                payload["reasoning"] = {"effort": effort}
            if request.tools:
                payload["tools"] = copy.deepcopy(request.tools)
            if request.output_json_schema:
                payload["text"] = {
                    "format": {"type": "json_schema", "strict": True, **request.output_json_schema}
                }
            return "/responses", payload
        messages = chat_messages(history)
        payload = {"model": self.default_model, "messages": messages, "stream": False}
        if self.kind == "ollama":
            if effort is not None and thinking is not None:
                raise ProviderError("Choose either thinking or reasoning_effort for Ollama")
            payload["options"] = {**params, "num_predict": maximum}
            if thinking is not None or effort is not None:
                payload["think"] = thinking if thinking is not None else effort
            tool_names = {
                item.get("call_id"): item.get("name")
                for item in history
                if item.get("type") == "function_call"
            }
            for message in messages:
                if message.get("role") == "tool":
                    name = tool_names.get(message.get("tool_call_id"))
                    if name:
                        message["tool_name"] = name
                if "reasoning_content" in message:
                    message["thinking"] = message.pop("reasoning_content")
                for call in message.get("tool_calls", []):
                    call["function"]["arguments"] = _arguments(call["function"]["arguments"])
            if request.output_json_schema:
                payload["format"] = request.output_json_schema["schema"]
            endpoint = "/api/chat"
        else:
            payload.update(params)
            payload["max_completion_tokens"] = maximum
            if effort:
                payload["reasoning_effort"] = effort
            if thinking is not None:
                # Explicit vLLM-compatible extension: unsupported servers fail visibly.
                payload["chat_template_kwargs"] = {"enable_thinking": thinking}
            if request.output_json_schema:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"strict": True, **request.output_json_schema},
                }
            endpoint = "/chat/completions"
        if request.tools:
            payload["tools"] = _tools(request.tools)
        return endpoint, payload

    def _parse(self, data, history):
        additions, calls, texts = [], [], []
        if self.kind == "responses":
            if data.get("status") in {"failed", "incomplete", "cancelled"}:
                raise ProviderError("Model response was incomplete or failed")
            if not isinstance(data.get("output"), list):
                raise ProviderError("Missing Responses output list")
            for item in data["output"]:
                if item.get("type") == "function_call":
                    args = _arguments(item.get("arguments"))
                    call_id = item.get("call_id")
                    if not call_id or not item.get("name"):
                        raise ProviderError("Malformed model tool call")
                    calls.append({"name": item["name"], "call_id": call_id, "arguments": args})
                    additions.append(
                        {
                            "type": "function_call",
                            "name": item["name"],
                            "call_id": call_id,
                            "arguments": json.dumps(args),
                        }
                    )
                elif item.get("type") == "message":
                    text = "".join(
                        c.get("text", c.get("refusal", ""))
                        for c in item.get("content", [])
                        if isinstance(c, dict)
                    )
                    texts.append(text)
                    additions.append({"role": "assistant", "content": text})
                elif item.get("type") == "reasoning":
                    # Preserve provider-supplied reasoning items for stateless continuation.
                    additions.append(copy.deepcopy(item))
        else:
            if self.kind == "ollama":
                message = data.get("message")
                finish = data.get("done_reason")
            else:
                choices = data.get("choices") or []
                message = choices[0].get("message") if choices else None
                finish = choices[0].get("finish_reason") if choices else None
            if finish in {"length", "max_tokens"}:
                raise ProviderError("Model output token limit reached")
            if not isinstance(message, dict):
                raise ProviderError("Missing model response message")
            text = message.get("content") or message.get("refusal") or ""
            texts.append(text)
            reasoning = message.get("reasoning_content", message.get("thinking"))
            if text or reasoning:
                entry = {"role": "assistant", "content": text}
                if reasoning:
                    entry["reasoning_content"] = reasoning
                additions.append(entry)
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                args = _arguments(fn.get("arguments"))
                if not fn.get("name"):
                    raise ProviderError("Model returned a tool call without a name")
                call_id = call.get("id") or (
                    f"call_{uuid.uuid4().hex}" if self.kind == "ollama" else None
                )
                if not call_id:
                    raise ProviderError("Model returned a tool call without an ID")
                calls.append({"name": fn["name"], "call_id": call_id, "arguments": args})
                additions.append(
                    {
                        "type": "function_call",
                        "name": fn["name"],
                        "call_id": call_id,
                        "arguments": json.dumps(args),
                    }
                )
        if not calls and not any(texts):
            raise ProviderError("Model returned neither text nor tool calls")
        return LLMResponse(
            content="".join(texts),
            tool_calls=calls,
            success=True,
            provider="http",
            messages=history + additions,
            metadata={"response_id": data.get("id"), "usage": _usage(data, self.kind == "ollama")},
        )

    async def chat(self, request: LLMRequest) -> LLMResponse:
        start = time.monotonic()
        record = {
            "success": False,
            "usage": _usage({}),
            "request": None,
            "response": None,
            "effective_params": {"model": self.default_model},
            "metadata": copy.deepcopy(request.metadata or {}),
        }
        try:
            if self.kind == "mock":
                call = {
                    "name": "complete_task",
                    "call_id": f"mock_{uuid.uuid4().hex}",
                    "arguments": {
                        "task_status": "success",
                        "task_summary": "Mock execution; no model evaluation",
                    },
                }
                result = LLMResponse(
                    content="Mock execution; no model evaluation",
                    tool_calls=[call],
                    provider="mock",
                    messages=list(request.messages or []),
                    metadata={"mock": True, "usage": _usage({})},
                )
                record["success"] = True
                return result
            suffix, payload = self._payload(request)
            record["request"] = payload
            record["effective_params"] = {
                k: v for k, v in payload.items() if k not in {"input", "messages", "tools"}
            }
            url = _endpoint(self.connection, suffix)
            target, headers, extensions = await _target(url, self.connection)
            response = await self.http.post(
                target, json=payload, headers=headers, extensions=extensions
            )
            if response.status_code >= 300:
                raise ProviderError(
                    f"Provider HTTP {response.status_code}; check model, credentials and supported parameters"
                )
            data = response.json()
            record["response"] = data
            record["usage"] = _usage(data, self.kind == "ollama")
            result = self._parse(data, copy.deepcopy(request.messages or []))
            record["success"] = True
            return result
        except asyncio.CancelledError:
            record["error"] = "Request cancelled"
            raise
        except ProviderError as exc:
            record["error"] = str(exc)
            raise
        except httpx.TimeoutException:
            record["error"] = "Provider request timed out"
            raise ProviderError(record["error"]) from None
        except httpx.HTTPError:
            record["error"] = "Provider connection failed"
            raise ProviderError(record["error"]) from None
        except (TypeError, ValueError, KeyError, AttributeError):
            record["error"] = "Provider returned a malformed response"
            raise ProviderError(record["error"]) from None
        finally:
            record["duration_ms"] = round((time.monotonic() - start) * 1000, 2)
            await _notify(self.on_call, _scrub(record, self.connection.get("api_key")))


async def discover_models(connection):
    connection = validate_connection(connection)
    if connection.get("kind") == "mock":
        return [{"id": "mock", "name": "Mock (no quality evaluation)"}]
    native = connection.get("kind") == "ollama"
    url = _endpoint(connection, "/api/tags" if native else "/models")
    target, headers, extensions = await _target(url, connection)
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
            response = await client.get(target, headers=headers, extensions=extensions)
            if response.status_code >= 300:
                raise ProviderError(
                    f"Model catalogue HTTP {response.status_code}; manual model entry is available"
                )
            data = response.json()
            entries = data.get("models" if native else "data")
            if not isinstance(entries, list):
                raise ProviderError("Provider did not return a model catalogue")
            return [
                {
                    "id": item.get("name") if native else item["id"],
                    "name": item.get("name") or item["id"],
                    **(
                        {"size": item.get("size"), "details": item.get("details")} if native else {}
                    ),
                }
                for item in entries
            ]
    except ProviderError:
        raise
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        raise ProviderError("Could not read provider model catalogue") from None


async def test_model(connection, config):
    """Paid when using a paid provider. Proves only this two-step tool roundtrip."""
    probe_config = {**config, "max_output_tokens": min(config.get("max_output_tokens", 1024), 1024)}
    client = ExperimentLLMClient(connection, probe_config)
    records = []
    client.on_call = records.append
    tool = {
        "type": "function",
        "name": "connection_probe",
        "description": "Return a probe value",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }
    try:
        first = await client.chat(
            LLMRequest(
                messages=[
                    {
                        "role": "user",
                        "content": "Call connection_probe once with no arguments. After receiving its result, return exactly the probe_value.",
                    }
                ],
                tools=[tool],
            )
        )
        if (
            not first.tool_calls
            or len(first.tool_calls) != 1
            or first.tool_calls[0]["name"] != "connection_probe"
        ):
            return {
                "status": "incompatible",
                "tool_call": False,
                "tool_continuation": False,
                "message": "The model did not call the requested tool",
                "calls": records,
            }
        call = first.tool_calls[0]
        probe = f"verified_{uuid.uuid4().hex[:12]}"
        history = list(first.messages or []) + [
            {
                "type": "function_call_output",
                "call_id": call["call_id"],
                "output": json.dumps({"probe_value": probe}),
            }
        ]
        second = await client.chat(LLMRequest(messages=history, tools=[tool]))
        continued = probe in (second.content or "") and not second.tool_calls
        return {
            "status": "ready" if continued else "incompatible",
            "tool_call": True,
            "tool_continuation": continued,
            "message": "Two-step tool probe only; not a benchmark",
            "calls": records,
        }
    finally:
        await client.aclose()


test_model.__test__ = False


async def pull_model(connection, model, on_progress=None):
    """Download only via an already installed Ollama service; no shell commands."""
    if connection.get("kind") != "ollama":
        raise ProviderError("One-click download requires an existing Ollama server")
    if not model or not isinstance(model, str) or len(model) > 200:
        raise ProviderError("Invalid model name")
    url = _endpoint(connection, "/api/pull")
    target, headers, extensions = await _target(url, connection)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(180, connect=15), follow_redirects=False, trust_env=False
        ) as client:
            async with client.stream(
                "POST",
                target,
                headers=headers,
                extensions=extensions,
                json={"model": model, "stream": True},
            ) as response:
                if response.status_code >= 300:
                    raise ProviderError(f"Ollama download HTTP {response.status_code}")
                complete = False
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    event = json.loads(line)
                    if event.get("error"):
                        raise ProviderError("Ollama could not download the selected model")
                    safe = {
                        k: event[k]
                        for k in ("status", "digest", "total", "completed")
                        if k in event
                    }
                    await _notify(on_progress, _scrub(safe, connection.get("api_key")))
                    complete = event.get("status") == "success"
                if not complete:
                    raise ProviderError("Ollama download ended before completion")
                return {
                    "status": "downloaded",
                    "model": model,
                    "message": "Downloaded; readiness requires a model probe",
                }
    except ProviderError:
        raise
    except (httpx.HTTPError, ValueError, TypeError):
        raise ProviderError("Ollama download interrupted") from None
