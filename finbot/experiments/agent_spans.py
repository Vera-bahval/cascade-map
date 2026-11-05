"""Invocation identity and elapsed time for experiment-only agent tracing."""

import asyncio
import time
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4


@dataclass
class AgentSpan:
    id: str
    parent_id: str | None
    agent_name: str


_active_span = ContextVar("experiment_agent_span", default=None)


def span_fields(agent_name=None):
    span = _active_span.get()
    if span is None:
        return {}
    if agent_name:
        span.agent_name = agent_name
    return {
        "agent_run_id": span.id,
        "parent_agent_run_id": span.parent_id,
        "agent_name": span.agent_name,
    }


async def run_in_span(capture, agent_name, task_data, invoke, *, workflow_id=None):
    parent = _active_span.get()
    span = AgentSpan(uuid4().hex, parent.id if parent else None, agent_name)
    token = _active_span.set(span)
    started = time.monotonic()
    result, error, status = None, None, "interrupted"

    async def emit(kind, data):
        await capture(
            **span_fields(),
            workflow_id=workflow_id,
            at=datetime.now(UTC).isoformat(),
            event_type=kind,
            event_subtype="lifecycle",
            event_data=data,
        )

    try:
        await emit("agent_span_start", {"input": task_data})
        result = await invoke()
        reported = result.get("task_status") if isinstance(result, dict) else None
        status = "failed" if reported in {"failed", "failure", "error"} else "completed"
        return result
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except Exception as exc:
        status = "failed"
        error = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        try:
            data = {
                "result": result,
                "status": status,
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
            }
            if error:
                data["error"] = error
            await emit("agent_span_end", data)
        finally:
            _active_span.reset(token)
