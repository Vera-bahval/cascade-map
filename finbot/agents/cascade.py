"""Capture agent outcomes and summary signals for invoice workflows.

Summary keywords are observations, not proof of a cascade. Experiment reports
classify propagation only from linked agent outputs and recorded actions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from finbot.agents.cascade_signals import (
    FINAL_AGENTS,
    NEGATIVE_KEYWORDS,
    RISK_KEYWORDS,
    AgentStepResult,
    CascadeAnalysis,
    _extract_errors,
    _infer_confidence,
    classify_cascade,
    effective_error_signals,
    extract_error_signals,
)
from finbot.agents.orchestrator import OrchestratorAgent

SCENARIOS_PATH = Path(__file__).parent / "cascade_scenarios.json"


class CascadeOrchestratorAgent(OrchestratorAgent):
    """OrchestratorAgent instrumented to expose cascade-failure telemetry.

    Behaves identically to the base orchestrator -- it just records each
    delegated agent's outcome as a structured `AgentStepResult` as the
    workflow runs. Callers can read `agent_chain` / `get_cascade_analysis()`
    after `process()` completes.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.agent_chain: list[AgentStepResult] = []

    def _capture_agent_context(self, agent_label: str, result: dict[str, Any]) -> None:
        super()._capture_agent_context(agent_label, result)
        status = result.get("task_status", "failed")
        summary = result.get("task_summary", "") or ""
        success = status == "success"
        step = AgentStepResult(
            order=len(self.agent_chain) + 1,
            agent=agent_label,
            success=success,
            confidence=_infer_confidence(success, summary),
            reasoning=summary,
            errors=_extract_errors(success, summary),
        )
        self.agent_chain.append(step)

    def get_cascade_analysis(self) -> CascadeAnalysis:
        steps = self.agent_chain
        if not steps:
            return CascadeAnalysis(
                initial_confidence=0.0,
                final_confidence=0.0,
                confidence_degradation=0.0,
                total_errors=0,
                failed_agents=[],
                cascade_failures_detected=False,
                cascade_type="none",
                reached_final_agent=False,
            )

        initial = steps[0].confidence
        cumulative = 1.0
        for s in steps:
            cumulative *= s.confidence
        cascade_type, detected = classify_cascade(steps)
        return CascadeAnalysis(
            initial_confidence=initial,
            final_confidence=cumulative,
            confidence_degradation=initial - cumulative,
            total_errors=sum(len(effective_error_signals(s)) for s in steps),
            failed_agents=[s.agent for s in steps if not s.success],
            cascade_failures_detected=detected,
            cascade_type=cascade_type,
            reached_final_agent=any(s.agent in FINAL_AGENTS for s in steps),
        )

    def get_agent_chain_dicts(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self.agent_chain]


def load_scenarios_file(path: Path | None = None) -> dict[str, Any]:
    """Load the cascade scenarios JSON file.

    Scenarios live in `cascade_scenarios.json` alongside this module so they
    can be edited or extended without code changes. The returned dict has the
    shape:

        {
            "version": int,
            "description": str,
            "cascade_types": {type_id: {label, severity, summary}},
            "scenarios": [
                {id, title, description, expected_cascade_type, severity,
                 invoice: {invoice_prefix, amount, description,
                           due_date_offset_days},
                 explanation},
                ...
            ],
        }
    """
    target = path or SCENARIOS_PATH
    with open(target, encoding="utf-8") as f:
        return json.load(f)


def get_scenario(scenario_id: str, path: Path | None = None) -> dict[str, Any]:
    """Return a single scenario by id. Raises KeyError if not found."""
    data = load_scenarios_file(path)
    for sc in data.get("scenarios", []):
        if sc.get("id") == scenario_id:
            return sc
    raise KeyError(f"Unknown cascade scenario id: {scenario_id!r}")


async def run_cascade_orchestrator(
    task_data: dict[str, Any],
    session_context: Any,
    workflow_id: str | None = None,
) -> dict[str, Any]:
    """Run the cascade-instrumented orchestrator and return a rich result.

    The returned dict extends the normal orchestrator result with:
        agent_chain       list of per-agent step dicts in execution order
        cascade_analysis  dict of aggregate cascade metrics
    """
    agent = CascadeOrchestratorAgent(
        session_context=session_context,
        workflow_id=workflow_id,
    )
    result = await agent.process(task_data=task_data)
    result["agent_chain"] = agent.get_agent_chain_dicts()
    result["cascade_analysis"] = agent.get_cascade_analysis().to_dict()
    return result
