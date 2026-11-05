"""LLM Client with various providers and models
TODO: incorporate reasoning capabilities
"""

import logging
from contextlib import contextmanager
from contextvars import ContextVar

from finbot.config import settings
from finbot.core.data.models import LLMRequest, LLMResponse

logger = logging.getLogger(__name__)


class LLMClient:
    """LLM Client with configurable provider and model"""

    def __init__(self):
        self.provider = settings.LLM_PROVIDER
        self.default_model = settings.LLM_DEFAULT_MODEL
        self.default_temperature = settings.LLM_DEFAULT_TEMPERATURE
        self.client = self._get_client()

    def _get_client(self):
        """Get the LLM client"""
        if self.provider == "openai":
            # pylint: disable=import-outside-toplevel
            from finbot.core.llm.openai_client import OpenAIClient

            return OpenAIClient()
        elif self.provider == "mock":
            # pylint: disable=import-outside-toplevel
            from finbot.core.llm.mock_client import MockLLMClient

            return MockLLMClient()

        raise ValueError(f"Unsupported LLM provider: {self.provider}")

    async def chat(
        self,
        request: LLMRequest,
    ) -> LLMResponse:
        """
        Chat with LLM

        Args:
            messages: List of message dicts
            model: Optional model override
            temperature: Optional temperature override

        Returns:
            LLM response string
        """
        try:
            if request.provider and request.provider != self.provider:
                logger.warning(
                    "Provider mismatch, unexpected behavior may occur: "
                    "request.provider=%s, client.provider=%s",
                    request.provider,
                    self.provider,
                )
            return await self.client.chat(request)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("LLM call failed: %s", e)
            return LLMResponse(
                content=f"Error: LLM Provider {self.provider} unavailable - {str(e)}",
                provider=self.provider,
                success=False,
            )


_default_client = None
_active_client = ContextVar("finbot_llm_client", default=None)


def get_llm_client() -> LLMClient:
    """Use a task-local experiment client, or lazily create the ordinary client."""
    active = _active_client.get()
    if active is not None:
        return active
    global _default_client
    if _default_client is None:
        _default_client = LLMClient()
    return _default_client


@contextmanager
def use_llm_client(client):
    """Scope an immutable client to this task and its delegated agent tasks."""
    token = _active_client.set(client)
    try:
        yield client
    finally:
        _active_client.reset(token)


def __getattr__(name):
    # Preserve the legacy public singleton without initializing credentials on import.
    if name == "llm_client":
        global _default_client
        if _default_client is None:
            _default_client = LLMClient()
        return _default_client
    raise AttributeError(name)
