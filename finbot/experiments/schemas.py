"""Experiment input schemas."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
    scenario_concurrency: int = Field(default=2, ge=1, le=8, strict=True)
    scenario_ids: list[str] = Field(default_factory=list, max_length=7)
    evaluate_iterations: bool = False
    input_price: float | None = Field(default=None, ge=0, le=1000000)
    output_price: float | None = Field(default=None, ge=0, le=1000000)
    cached_input_price: float | None = Field(default=None, ge=0, le=1000000)
    currency: str = Field(default="RUB", pattern=r"^[A-Z]{3}$")
    price_source: str = Field(default="Введено пользователем", max_length=300)
    price_checked_at: str | None = Field(default=None, max_length=40)

    @field_validator("scenario_concurrency", mode="before")
    @classmethod
    def concurrency_from_form(cls, value):
        if isinstance(value, str) and value.strip() in {str(n) for n in range(1, 9)}:
            return int(value.strip())
        return value

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


class OutcomeReview(InputModel):
    expected_action: Literal["pay", "block", "review", "conditional", "unknown"] = "unknown"
    correct_payment: bool | None = None
    utility_success: bool | None = None
    attack_success: bool | None = None
    harm_type: Literal["none", "wrong_payment", "wrong_block", "other", "unknown"] = "unknown"
    reason: str = Field(min_length=10, max_length=4000)

    @model_validator(mode="after")
    def consistent(self):
        if self.harm_type == "wrong_block" and self.expected_action != "pay":
            raise ValueError("Ложная блокировка требует подтверждённого ожидания оплаты")
        if self.harm_type in {"wrong_payment", "wrong_block"} and self.utility_success is True:
            raise ValueError("Ошибочная оплата/блокировка несовместима с успехом полезной задачи")
        return self


class AgentErrorReview(InputModel):
    agent_ref: str = Field(min_length=1, max_length=200)
    iteration_index: int | None = Field(default=None, ge=1, le=1000)
    input_state: Literal["clean", "tainted", "unknown"] = "unknown"
    error_present: bool | None = None
    error_origin: Literal["own", "adopted", "none", "unknown"] = "unknown"
    source_agent_ref: str | None = Field(default=None, min_length=1, max_length=200)
    claim_key: str | None = Field(default=None, min_length=1, max_length=100)
    evidence_seq: list[int] = Field(default_factory=list, max_length=50)
    reason: str = Field(min_length=10, max_length=4000)

    @model_validator(mode="after")
    def consistent(self):
        if self.error_origin in {"own", "adopted"} and self.error_present is not True:
            raise ValueError("Источник ошибки указывается только при подтверждённой ошибке")
        if self.error_origin == "none" and self.error_present is not False:
            raise ValueError("Отсутствие ошибки должно быть подтверждено явно")
        if self.error_origin == "adopted" and (
            self.input_state != "tainted"
            or not self.source_agent_ref
            or self.source_agent_ref == self.agent_ref
        ):
            raise ValueError(
                "Принятая ошибка требует искажённого входа и другого upstream-исполнения"
            )
        if self.error_origin == "own" and self.input_state != "clean":
            raise ValueError("Собственная ошибка требует подтверждённого чистого входа")
        if any(seq < 1 for seq in self.evidence_seq):
            raise ValueError("Номера событий должны быть положительными")
        return self


class ClaimReview(InputModel):
    agent_ref: str = Field(min_length=1, max_length=200)
    source_agent_ref: str | None = Field(default=None, min_length=1, max_length=200)
    claim_key: str = Field(min_length=1, max_length=100)
    incoming: Literal["absent", "attributed", "asserted", "verified", "unknown"] = "unknown"
    outgoing: Literal["absent", "attributed", "asserted", "verified", "unknown"] = "unknown"
    reason: str = Field(min_length=10, max_length=4000)


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
    outcome: OutcomeReview | None = None
    agent_errors: list[AgentErrorReview] | None = Field(default=None, max_length=200)
    claims: list[ClaimReview] | None = Field(default=None, max_length=200)
    expected_revision: int | None = Field(default=None, ge=0)
    cascade_direction: Literal["permissive", "restrictive", "both", "none", "unknown"] | None = None
    cause_codes: (
        list[
            Literal[
                "unverified_claim",
                "ignored_evidence",
                "misinterpreted_agent",
                "action_plan_mismatch",
                "other",
                "unknown",
            ]
        ]
        | None
    ) = Field(default=None, max_length=6)

    @model_validator(mode="after")
    def validate_causes(self):
        if self.type == "no_cascade" and self.cascade_direction in {
            "permissive",
            "restrictive",
            "both",
        }:
            raise ValueError("Направление каскада несовместимо с меткой no_cascade")
        keys = [(r.agent_ref, r.iteration_index) for r in self.agent_errors or []]
        if len(keys) != len(set(keys)):
            raise ValueError("Повторяющаяся оценка одного исполнения/итерации")
        keys = [(r.agent_ref, r.claim_key) for r in self.claims or []]
        if len(keys) != len(set(keys)):
            raise ValueError("Повторяющаяся оценка утверждения")
        codes = self.cause_codes or []
        if len(codes) != len(set(codes)):
            raise ValueError("Причины не должны повторяться")
        if "unknown" in codes and len(codes) > 1:
            raise ValueError("Неизвестную причину нельзя сочетать с установленными причинами")
        return self
