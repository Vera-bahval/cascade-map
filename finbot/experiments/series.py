"""Bounded, decimal-exact expansion of immutable experiment manifests."""

from decimal import Decimal
from itertools import product
from typing import Any, Literal

from pydantic import Field, model_validator

from finbot.experiments.schemas import ExperimentConfig, InputModel

AXES = {"temperature", "top_p", "top_k", "min_p", "seed"}
MAX_RUNS = 5000
MAX_ATTEMPTS = 50000


class ParameterRange(InputModel):
    start: float
    stop: float
    step: float = Field(gt=0)

    @model_validator(mode="after")
    def ascending(self):
        if self.stop < self.start:
            raise ValueError("Конец диапазона должен быть не меньше начала")
        return self

    def values(self):
        start, stop, step = map(lambda v: Decimal(str(v)), (self.start, self.stop, self.step))
        size = int((stop - start) // step) + 1
        if size > MAX_RUNS:
            raise ValueError("Слишком много значений в диапазоне")
        return [float(start + i * step) for i in range(size)]


class ModelTarget(InputModel):
    connection_id: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    overrides: dict[str, Any] = Field(default_factory=dict)
    matrix: dict[str, ParameterRange | list[float | int | None]] = Field(default_factory=dict)


class SeriesManifest(InputModel):
    name: str = Field(default="Серия экспериментов", min_length=1, max_length=120)
    defaults: dict[str, Any] = Field(default_factory=dict)
    matrix: dict[str, ParameterRange | list[float | int | None]] = Field(default_factory=dict)
    models: list[ModelTarget] = Field(min_length=1, max_length=50)
    schema_version: Literal[1] = 1


class SeriesLaunch(InputModel):
    config_id: str
    idempotency_key: str = Field(min_length=8, max_length=100)


def expand_manifest(manifest: SeriesManifest, available_scenarios, connection_kinds):
    """Cartesian product; null removes a shared parameter for an individual model."""
    result, attempts = [], 0
    forbidden = {"model", "connection_id", "name"}
    if forbidden & manifest.defaults.keys():
        raise ValueError("Модель и подключение задаются в models")
    for target in manifest.models:
        if forbidden & target.overrides.keys():
            raise ValueError("Модель и подключение задаются в models")
        kind = connection_kinds[target.connection_id]
        matrix = {
            **{k: v for k, v in manifest.matrix.items() if k not in target.overrides},
            **target.matrix,
        }
        if set(matrix) - AXES:
            raise ValueError("Диапазоны поддержаны для temperature, top_p, top_k, min_p, seed")
        axes, values = [], []
        for key, axis in matrix.items():
            expanded = axis.values() if isinstance(axis, ParameterRange) else axis
            if not expanded or len(expanded) > MAX_RUNS or len(set(expanded)) != len(expanded):
                raise ValueError(f"Пустой, слишком большой или повторяющийся диапазон {key}")
            axes.append(key)
            values.append(expanded)
        size = 1
        for axis in values:
            size *= len(axis)
        if size + len(result) > MAX_RUNS:
            raise ValueError(f"Не больше {MAX_RUNS} комбинаций в серии")
        for combination in product(*values):
            data = {**manifest.defaults, **target.overrides, **dict(zip(axes, combination))}
            cfg = ExperimentConfig.model_validate(
                {
                    **data,
                    "name": manifest.name,
                    "model": target.model,
                    "connection_id": target.connection_id,
                }
            ).model_dump(exclude_none=True)
            cfg["scenario_ids"] = cfg["scenario_ids"] or list(available_scenarios)
            if not set(cfg["scenario_ids"]) <= set(available_scenarios):
                raise ValueError("Неизвестный сценарий")
            if kind == "responses" and any(
                k in cfg for k in ("seed", "top_k", "min_p", "thinking")
            ):
                raise ValueError(
                    "Responses не поддерживает seed, top_k, min_p, thinking; задайте null в overrides/matrix"
                )
            if kind == "ollama" and "thinking" in cfg and "reasoning_effort" in cfg:
                raise ValueError("Ollama: выберите thinking или reasoning_effort")
            attempts += len(cfg["scenario_ids"]) * cfg["repeats"]
            if attempts > MAX_ATTEMPTS:
                raise ValueError(f"Не больше {MAX_ATTEMPTS} попыток сценариев в серии")
            result.append(cfg)
    return {
        "runs": result,
        "run_count": len(result),
        "attempt_count": attempts,
        "mode": "cartesian",
    }
