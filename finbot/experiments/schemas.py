"""Experiment input schemas."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ConnectionInput(InputModel):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["chat_completions", "responses", "ollama", "mock"]
    base_url: str = Field(default="", max_length=2048)
    api_key: str = Field(default="", max_length=4096)
    allow_private_network: bool = False


class ExperimentConfig(InputModel):
    name: str = Field(default="Эксперимент", min_length=1, max_length=120)
    connection_id: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200, pattern=r"^[^\s\x00-\x1f]+$")
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    top_k: int | None = Field(default=None, ge=0, le=1000)
    min_p: float | None = Field(default=None, ge=0, le=1)
    seed: int | None = Field(default=None, ge=0, le=2147483647)
    thinking: bool | None = None
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] | None = None
    max_output_tokens: int = Field(default=5000, ge=256, le=32768)
    timeout_seconds: int = Field(default=60, ge=5, le=600)
    max_iterations: int = Field(default=10, ge=1, le=30)
    scenario_timeout_seconds: int = Field(default=1800, ge=30, le=7200)
    repeats: int = Field(default=1, ge=1, le=20)
    scenario_ids: list[str] = Field(default_factory=list, max_length=7)
    input_price: float | None = Field(default=None, ge=0, le=1000000)
    output_price: float | None = Field(default=None, ge=0, le=1000000)
    cached_input_price: float | None = Field(default=None, ge=0, le=1000000)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    price_source: str = Field(default="Введено пользователем", max_length=300)
    price_checked_at: str | None = Field(default=None, max_length=40)

    @model_validator(mode="after")
    def unique_scenarios(self):
        if len(self.scenario_ids) != len(set(self.scenario_ids)):
            raise ValueError("Сценарии не должны повторяться: используйте число повторений")
        if (self.input_price is None) != (self.output_price is None):
            raise ValueError("Укажите оба тарифа: входные и выходные токены")
        return self


class ProbeInput(InputModel):
    connection_id: str
    config: ExperimentConfig


class EstimateInput(InputModel):
    config: ExperimentConfig


class RunInput(InputModel):
    config_id: str
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=100)


class PullInput(InputModel):
    connection_id: str
    model: str = Field(min_length=1, max_length=200)


class AssessmentInput(InputModel):
    type: Literal[
        "no_cascade",
        "dirty_data",
        "half_cascade",
        "midchain_cascade",
        "full_cascade",
        "needs_review",
    ]
    reason: str = Field(min_length=10, max_length=4000)
