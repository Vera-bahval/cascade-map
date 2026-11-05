import pytest
from pydantic import ValidationError

from finbot.experiments.schemas import ExperimentConfig


def test_new_configs_default_to_two_parallel_scenarios():
    config = ExperimentConfig(connection_id="test", model="test-model")
    assert config.scenario_concurrency == 2


@pytest.mark.parametrize("value", [1, 2, 4, 8, "4", " 4 "])
def test_concurrency_survives_saved_config_round_trip(value):
    config = ExperimentConfig(connection_id="test", model="test-model", scenario_concurrency=value)
    restored = ExperimentConfig.model_validate_json(config.model_dump_json())
    assert restored.scenario_concurrency == int(value)
    assert type(restored.scenario_concurrency) is int


@pytest.mark.parametrize(
    "value", [0, -1, 9, 1.5, 4.0, True, False, "0", "9", "4.0", "4.5", "", "four", None]
)
def test_concurrency_rejects_invalid_or_unbounded_values(value):
    with pytest.raises(ValidationError):
        ExperimentConfig(connection_id="test", model="test-model", scenario_concurrency=value)
