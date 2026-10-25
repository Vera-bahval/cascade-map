"""Session-scoped experiment API."""

from __future__ import annotations
import json
import uuid
from datetime import datetime, UTC
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from finbot.core.auth.middleware import get_session_context
from finbot.core.auth.session import SessionContext
from finbot.experiments.catalog import EXPECTATIONS, LOCAL_CATALOG, TYPES, RUBRIC_VERSION, versions
from finbot.experiments.schemas import (
    ConnectionInput,
    ExperimentConfig,
    ProbeInput,
    EstimateInput,
    RunInput,
    PullInput,
    AssessmentInput,
)
from finbot.experiments.store import Store, stamp
from finbot.experiments.reporting import enrich_report

router = APIRouter(prefix="/api/v1/experiments", tags=["experiments"])
ROOT = Path(__file__).resolve().parents[2]


def get_store():
    return Store()


def owner(session: SessionContext):
    return json.dumps([session.namespace, session.user_id], separators=(",", ":"))


def scenarios():
    data = json.loads((ROOT / "finbot/agents/cascade_scenarios.json").read_text())
    return [
        {**s, "expected": EXPECTATIONS.get(s["id"], "Нужна оценка по правилам сценария")}
        for s in data["scenarios"]
    ]


def validated_config(config: ExperimentConfig):
    data = config.model_dump(exclude_none=True)
    available = {s["id"] for s in scenarios()}
    data["scenario_ids"] = data["scenario_ids"] or [s["id"] for s in scenarios()]
    if not set(data["scenario_ids"]) <= available:
        raise HTTPException(422, "Неизвестный сценарий")
    return data


def lookup(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'")) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/bootstrap")
async def bootstrap(
    session: SessionContext = Depends(get_session_context), store: Store = Depends(get_store)
):
    who = owner(session)
    return {
        "connections": store.list_connections(who),
        "configs": store.list_configs(who),
        "scenarios": scenarios(),
        "worker": store.worker_status(),
        "local_catalog": LOCAL_CATALOG,
        "cascade_types": TYPES,
        "rubric_version": RUBRIC_VERSION,
    }


@router.post("/connections", status_code=201)
async def connection(
    body: ConnectionInput,
    session: SessionContext = Depends(get_session_context),
    store: Store = Depends(get_store),
):
    from finbot.experiments.providers import validate_connection

    data = body.model_dump()
    try:
        data = validate_connection(data)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return store.save_connection(owner(session), data)


@router.post("/connections/{connection_id}/discover")
async def discover(
    connection_id: str,
    session: SessionContext = Depends(get_session_context),
    store: Store = Depends(get_store),
):
    from finbot.experiments.providers import discover_models, ProviderError

    c = lookup(store.get_connection, owner(session), connection_id, secret=True)
    try:
        result = await discover_models(c)
        return result if isinstance(result, dict) else {"models": result}
    except (ProviderError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/probe")
async def probe(
    body: ProbeInput,
    session: SessionContext = Depends(get_session_context),
    store: Store = Depends(get_store),
):
    from finbot.experiments.providers import test_model, ProviderError

    if body.connection_id != body.config.connection_id:
        raise HTTPException(422, "Подключения проверки и конфигурации не совпадают")
    c = lookup(store.get_connection, owner(session), body.connection_id, secret=True)
    try:
        return await test_model(c, validated_config(body.config))
    except (ProviderError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/configs", status_code=201)
async def config(
    body: ExperimentConfig,
    session: SessionContext = Depends(get_session_context),
    store: Store = Depends(get_store),
):
    return lookup(store.save_config, owner(session), validated_config(body))


@router.post("/estimate")
async def estimate(
    body: EstimateInput,
    session: SessionContext = Depends(get_session_context),
    store: Store = Depends(get_store),
):
    cfg = validated_config(body.config)
    who = owner(session)
    lookup(store.get_connection, who, cfg["connection_id"])
    attempts = len(cfg["scenario_ids"]) * cfg["repeats"]
    result = {
        "available": False,
        "low": None,
        "high": None,
        "currency": cfg["currency"],
        "attempts": attempts,
        "estimated_llm_calls": {"low": attempts * 10, "high": attempts * 40},
        "sample_count": 0,
        "basis": "Плановая оценка до калибровки: 10–40 запросов на сценарий, 4 000 входных и до 300 выходных токенов на запрос. Это не верхняя граница расходов.",
    }
    input_price = cfg.get("input_price")
    output_price = cfg.get("output_price")
    if input_price is None or output_price is None:
        result["basis"] = (
            "Тариф не задан. Укажите стоимость 1 млн входных и выходных токенов у выбранного провайдера."
        )
        return result
    samples = []
    comparable = (
        "connection_id",
        "model",
        "temperature",
        "top_p",
        "top_k",
        "min_p",
        "thinking",
        "reasoning_effort",
        "max_output_tokens",
        "max_iterations",
    )
    current_revision, current_hash = versions()
    for summary in store.list_jobs(who):
        if summary["kind"] != "experiment" or any(
            summary["config"].get(k) != cfg.get(k) for k in comparable
        ):
            continue
        if (
            summary.get("code_revision") != current_revision
            or summary.get("scenario_hash") != current_hash
        ):
            continue
        job = store.get_job(summary["id"], who)
        for case in job["cases"]:
            metrics = case.get("metrics", {})
            if (
                case["status"] == "completed"
                and case["scenario_id"] in cfg["scenario_ids"]
                and metrics.get("input_tokens") is not None
                and metrics.get("output_tokens") is not None
            ):
                samples.append((case["scenario_id"], metrics))
    # Require observations of every selected scenario, and weight them equally.
    groups = {sid: [m for s, m in samples if s == sid] for sid in cfg["scenario_ids"]}
    if all(groups.values()):
        estimated = 0.0
        calls = 0.0
        for group in groups.values():
            costs = []
            for m in group:
                cached = min(m.get("cached_input_tokens") or 0, m["input_tokens"])
                cached_rate = cfg.get("cached_input_price", input_price)
                costs.append(
                    (
                        (m["input_tokens"] - cached) * input_price
                        + cached * cached_rate
                        + m["output_tokens"] * output_price
                    )
                    / 1e6
                )
            estimated += sum(costs) / len(costs) * cfg["repeats"]
            calls += sum(m.get("llm_calls", 0) for m in group) / len(group) * cfg["repeats"]
        result.update(
            low=round(estimated * 0.7, 6),
            high=round(estimated * 1.5, 6),
            sample_count=len(samples),
            estimated_llm_calls={"low": round(calls * 0.7), "high": round(calls * 1.5)},
            basis=f"По {len(samples)} завершённым попыткам этой конфигурации и версии сценариев; диапазон 0.7–1.5 от наблюдённого среднего, не доверительный интервал и не лимит расходов.",
        )
    else:
        per_call = (4000 * input_price + min(cfg["max_output_tokens"], 300) * output_price) / 1e6
        result.update(
            low=round(attempts * 10 * per_call, 6), high=round(attempts * 40 * per_call, 6)
        )
    result["available"] = True
    return result


@router.post("/runs", status_code=202)
async def launch(
    body: RunInput,
    session: SessionContext = Depends(get_session_context),
    store: Store = Depends(get_store),
):
    cfg = lookup(store.get_config, owner(session), body.config_id)
    cfg = {
        **cfg,
        "business_date": datetime.now(UTC).date().isoformat(),
        "rubric_version": RUBRIC_VERSION,
    }
    cases = [
        {"id": uuid.uuid4().hex, "scenario_id": sid, "repeat": repeat, "status": "queued"}
        for repeat in range(1, cfg["repeats"] + 1)
        for sid in cfg["scenario_ids"]
    ]
    rev, sh = versions()
    return lookup(
        store.create_job,
        owner(session),
        "experiment",
        cfg,
        cases,
        code_revision=rev,
        scenario_hash=sh,
        idempotency_key=body.idempotency_key,
    )


@router.get("/runs")
async def history(
    session: SessionContext = Depends(get_session_context), store: Store = Depends(get_store)
):
    return {"runs": store.list_jobs(owner(session)), "worker": store.worker_status()}


@router.get("/runs/{job_id}")
async def run(
    job_id: str,
    session: SessionContext = Depends(get_session_context),
    store: Store = Depends(get_store),
):
    job = lookup(store.get_job, job_id, owner(session))
    return enrich_report(job, store.report_events(job_id))


@router.post("/runs/{job_id}/cancel")
async def cancel(
    job_id: str,
    session: SessionContext = Depends(get_session_context),
    store: Store = Depends(get_store),
):
    return lookup(store.cancel, owner(session), job_id)


@router.get("/runs/{job_id}/events")
async def events(
    job_id: str,
    after: int = 0,
    session: SessionContext = Depends(get_session_context),
    store: Store = Depends(get_store),
):
    lookup(store.get_job, job_id, owner(session))
    return {"events": store.events(job_id, max(0, after))}


@router.get("/runs/{job_id}/export")
async def export(
    job_id: str,
    session: SessionContext = Depends(get_session_context),
    store: Store = Depends(get_store),
):
    job = lookup(store.get_job, job_id, owner(session))
    all_events = []
    after = 0
    while True:
        batch = store.events(job_id, after=after, limit=1000)
        if not batch:
            break
        all_events.extend(batch)
        after = batch[-1]["seq"]
    return JSONResponse(
        {"run": enrich_report(job, all_events), "events": all_events},
        headers={"Content-Disposition": f'attachment; filename="experiment-{job["id"]}.json"'},
    )


@router.patch("/runs/{job_id}/cases/{case_id}/assessment")
async def assess(
    job_id: str,
    case_id: str,
    body: AssessmentInput,
    session: SessionContext = Depends(get_session_context),
    store: Store = Depends(get_store),
):
    job = lookup(store.get_job, job_id, owner(session))
    case = next((c for c in job["cases"] if c["id"] == case_id), None)
    if not case:
        raise HTTPException(404, "Сценарий не найден")
    if case["status"] != "completed":
        raise HTTPException(409, "Оценка доступна для завершённого сценария")
    assessment = {
        **body.model_dump(),
        "label": TYPES[body.type],
        "reviewed_at": stamp(),
        "reviewer": session.user_id,
    }
    store.update_case(job_id, case_id, assessment=assessment)
    store.append_event(job_id, {"kind": "assessment", "case_id": case_id, **assessment})
    return assessment


@router.post("/local/pull", status_code=202)
async def pull(
    body: PullInput,
    session: SessionContext = Depends(get_session_context),
    store: Store = Depends(get_store),
):
    c = lookup(store.get_connection, owner(session), body.connection_id)
    if c["kind"] != "ollama":
        raise HTTPException(422, "Загрузка доступна только для Ollama")
    if body.model not in {m["id"] for m in LOCAL_CATALOG}:
        raise HTTPException(422, "Выберите модель из каталога загрузки")
    return lookup(
        store.create_job,
        owner(session),
        "model_pull",
        {"connection_id": body.connection_id, "model": body.model},
        [],
    )
