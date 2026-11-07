"""Native generateContent adapter for the fixed post-run observer only."""

import asyncio
import time
from urllib.parse import urlsplit

import httpx

from finbot.core.data.models import LLMResponse
from finbot.experiments.providers import ProviderError, _notify, _scrub, _target, validate_base_url


def usage(data):
    raw = data.get("usageMetadata") or {}

    def count(key):
        value = raw.get(key)
        return value if type(value) is int and value >= 0 else None

    candidates, thoughts = count("candidatesTokenCount"), count("thoughtsTokenCount")
    return {
        "input_tokens": count("promptTokenCount"),
        "output_tokens": candidates + (thoughts or 0) if candidates is not None else None,
        "cached_input_tokens": count("cachedContentTokenCount"),
        "reasoning_tokens": thoughts,
    }


class GeminiObserverClient:
    def __init__(self, connection, config, on_call=None):
        self.connection = dict(connection)
        self.endpoint = validate_base_url(connection["base_url"])
        self.config = dict(config)
        if not urlsplit(self.endpoint).path.endswith(f'/models/{config["model"]}:generateContent'):
            raise ProviderError("Gemini observer model must match its generateContent endpoint")
        self.on_call = on_call
        self.http = httpx.AsyncClient(
            timeout=config.get("timeout_seconds", 120),
            follow_redirects=False,
            trust_env=connection.get("use_env_proxy", False),
        )

    async def aclose(self):
        await self.http.aclose()

    async def chat(self, request):
        started = time.monotonic()
        generation = {
            "maxOutputTokens": self.config.get("max_output_tokens", 2000),
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "required": ["confidence", "error_present", "reason", "evidence"],
                "properties": {
                    "confidence": {"type": "NUMBER", "nullable": True},
                    "error_present": {"type": "BOOLEAN", "nullable": True},
                    "reason": {"type": "STRING"},
                    "evidence": {"type": "ARRAY", "items": {"type": "STRING"}},
                },
            },
        }
        if self.config.get("temperature") is not None:
            generation["temperature"] = self.config["temperature"]
        messages = request.messages or []
        payload = {
            "systemInstruction": {
                "parts": [{"text": m["content"]} for m in messages if m["role"] == "system"]
            },
            "contents": [
                {"role": "user", "parts": [{"text": m["content"]}]}
                for m in messages
                if m["role"] == "user"
            ],
            "generationConfig": generation,
        }
        record = {
            "success": False,
            "request": payload,
            "response": None,
            "usage": usage({}),
            "effective_params": {"model": self.config["model"], "generationConfig": generation},
            "provider": "gemini",
            "model_requested": self.config["model"],
        }
        try:
            if self.connection.get("use_env_proxy"):
                # An administrator-controlled proxy resolves the configured endpoint.
                # Preserve its hostname for CONNECT routing, as ordinary curl does.
                target, headers, extensions = self.endpoint, {}, {}
            else:
                target, headers, extensions = await _target(
                    self.endpoint, {**self.connection, "api_key": ""}
                )
            headers["X-goog-api-key"] = self.connection["api_key"]
            response = await self.http.post(
                target, json=payload, headers=headers, extensions=extensions
            )
            record["http_status"] = response.status_code
            if response.status_code >= 300:
                try:
                    error = response.json().get("error") or {}
                    record["provider_error"] = {
                        "status": error.get("status"),
                        "message": str(error.get("message") or "")[:2000],
                    }
                except (ValueError, AttributeError, TypeError):
                    pass
                raise ProviderError(f"Gemini observer HTTP {response.status_code}")
            data = response.json()
            record.update(response=data, usage=usage(data), model_returned=data.get("modelVersion"))
            candidates = data.get("candidates") or []
            if not candidates or candidates[0].get("finishReason") != "STOP":
                raise ProviderError("Gemini observer returned no completed candidate")
            content = "".join(
                part.get("text", "")
                for part in candidates[0].get("content", {}).get("parts", [])
                if not part.get("thought")
            )
            if not content:
                raise ProviderError("Gemini observer returned no text")
            record["success"] = True
            return LLMResponse(
                content=content,
                provider="http",
                metadata={"usage": record["usage"], "model_returned": data.get("modelVersion")},
            )
        except asyncio.CancelledError:
            record["error"] = "Gemini observer request cancelled"
            raise
        except ProviderError as exc:
            record["error"] = str(exc)
            raise
        except httpx.HTTPError:
            record["error"] = "Gemini observer connection failed or timed out"
            raise ProviderError(record["error"]) from None
        except (TypeError, ValueError, KeyError, AttributeError):
            record["error"] = "Gemini observer returned a malformed response"
            raise ProviderError(record["error"]) from None
        finally:
            record["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
            await _notify(self.on_call, _scrub(record, self.connection.get("api_key")))
