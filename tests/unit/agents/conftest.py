from unittest.mock import patch

import pytest

from finbot.core.llm.client import LLMClient, use_llm_client


@pytest.fixture(autouse=True)
def mock_agent_llm():
    with patch("finbot.core.llm.client.settings.LLM_PROVIDER", "mock"):
        client = LLMClient()
    with use_llm_client(client):
        yield client
