"""Session-scoped experiment API."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from finbot.core.auth.middleware import get_session_context
from finbot.core.auth.session import SessionContext
from finbot.experiments.catalog import (
    EXPECTATIONS,
    LOCAL_CATALOG,
    RUBRIC_VERSION,
    TYPES,
    scenario_catalog,
    versions,
)
from finbot.experiments.causes import CAUSE_TYPES
from finbot.experiments.reporting import enrich_report
from finbot.experiments.schemas import (
    AssessmentInput,
    ConnectionInput,
    EstimateInput,
    ExperimentConfig,
    ProbeInput,
    PullInput,
    RunInput,
)
from finbot.experiments.series import SeriesLaunch, SeriesManifest, expand_manifest
from finbot.experiments.store import Store, stamp

router = APIRouter(prefix="/api/v1/experiments", tags=["experiments"])


def get_store():
    return Store()


def owner(session: SessionContext):
    return json.dumps([session.namespace, session.user_id], separators=(",", ":"))


def get_owner(
    request: Request,
    response: Response,
    session: SessionContext = Depends(get_session_context),
    store: Store = Depends(get_store),
):
    from finbot.experiments.workspace import workspace_owner

    return workspace_owner(request, response, session, store)


def scenarios():
    return [
        {**s, "expected": EXPECTATIONS.get(s["id"], "Нужна оценка по правилам сценария")}
        for s in scenario_catalog()[0]
    ]


def validated_config(config: ExperimentConfig):
    data = config.model_dump(exclude_none=True)
    available = {s["id"] for s in scenarios()}
    data["scenario_ids"] = data["scenario_ids"] or [s["id"] for s in scenarios()]
    if not set(data["scenario_ids"]) <= available:
        raise HTTPException(422, "Неизвестный сценарий")
    if data.get("evaluate_iterations"):
        from finbot.experiments.evaluation import judge_policy

        if judge_policy() is None:
            raise HTTPException(422, "Постоянный оценщик не настроен на сервере")
    return data


def lookup(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'")) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/bootstrap")
async def bootstrap(who: str = Depends(get_owner), store: Store = Depends(get_store)):
    from finbot.experiments.evaluation import judge_policy

    return {
        "connections": store.list_connections(who),
        "configs": store.list_configs(who),
        "scenarios": scenarios(),
        "worker": store.worker_status(),
        "local_catalog": LOCAL_CATALOG,
        "cascade_types": TYPES,
        "cause_types": CAUSE_TYPES,
        "rubric_version": RUBRIC_VERSION,
        "series_configs": store.series_configs(who),
        "judge_available": judge_policy() is not None,
    }


def series_plan(body, who, store):
    kinds = {
        target.connection_id: lookup(store.get_connection, who, target.connection_id)["kind"]
        for target in body.models
    }
    plan = lookup(expand_manifest, body, [s["id"] for s in scenarios()], kinds)
    if any(cfg.get("evaluate_iterations") for cfg in plan["runs"]):
        from finbot.experiments.evaluation import judge_policy

        if judge_policy() is None:
            raise HTTPException(422, "Постоянный оценщик не настроен на сервере")
    return plan


@router.post("/series/preview")
async def preview_series(
    body: SeriesManifest, who: str = Depends(get_owner), store: Store = Depends(get_store)
):
    return series_plan(body, who, store)


@router.post("/series/configs", status_code=201)
async def save_series_config(
    body: SeriesManifest, who: str = Depends(get_owner), store: Store = Depends(get_store)
):
    series_plan(body, who, store)
    return store.save_series_config(who, body.model_dump(mode="json"))


@router.post("/series", status_code=202)
async def launch_series(
    body: SeriesLaunch, who: str = Depends(get_owner), store: Store = Depends(get_store)
):
    saved = lookup(store.get_series_config, who, body.config_id)
    manifest = SeriesManifest.model_validate(saved["manifest"])
    plan = series_plan(manifest, who, store)
    for cfg in plan["runs"]:
        cfg["rubric_version"] = RUBRIC_VERSION
    revision, scenario_hash = versions()
    return lookup(
        store.create_series,
        who,
        saved["manifest"],
        plan,
        {s["id"]: s for s in scenarios()},
        key=body.idempotency_key,
        revision=revision,
        scenario_hash=scenario_hash,
    )


@router.get("/series")
async def series_history(who: str = Depends(get_owner), store: Store = Depends(get_store)):
    from finbot.experiments.archive import configured

    states = store.result_sync_statuses()
    return {
        "series": [
            {**s, "result_sync": {"enabled": configured(), **states.get(s["id"], {})}}
            for s in store.list_series(who)
        ]
    }


@router.get("/series/{series_id}/export")
async def export_series(
    series_id: str, who: str = Depends(get_owner), store: Store = Depends(get_store)
):
    item = lookup(store.get_series, series_id, who)
    lines = [{"kind": "series", **{k: v for k, v in item.items() if k != "runs"}}]
    lines.extend({"kind": "run", **run} for run in item["runs"])
    return PlainTextResponse(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="series-{series_id}.jsonl"'},
    )


@router.post("/series/{series_id}/cancel")
async def cancel_series(
    series_id: str, who: str = Depends(get_owner), store: Store = Depends(get_store)
):
    item = lookup(store.get_series, series_id, who)
    for run in item["runs"]:
        store.cancel(who, run["id"])
    return store.get_series(series_id, who)


@router.post("/runs/{job_id}/evaluation", status_code=202)
async def retry_evaluation(
    job_id: str, who: str = Depends(get_owner), store: Store = Depends(get_store)
):
    return lookup(store.retry_evaluation, who, job_id)


@router.post("/connections", status_code=201)
async def connection(
    body: ConnectionInput,
    who: str = Depends(get_owner),
    store: Store = Depends(get_store),
):
    from finbot.experiments.providers import validate_connection

    data = body.model_dump()
    try:
        data = validate_connection(data)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return store.save_connection(who, data)


@router.post("/connections/{connection_id}/discover")
async def discover(
    connection_id: str,
    who: str = Depends(get_owner),
    store: Store = Depends(get_store),
):
    from finbot.experiments.providers import ProviderError, discover_models

    c = lookup(store.get_connection, who, connection_id, secret=True)
    try:
        result = await discover_models(c)
        return result if isinstance(result, dict) else {"models": result}
    except (ProviderError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/probe")
async def probe(
    body: ProbeInput,
    who: str = Depends(get_owner),
    store: Store = Depends(get_store),
):
    from finbot.experiments.providers import ProviderError, test_model

    if body.connection_id != body.config.connection_id:
        raise HTTPException(422, "Подключения проверки и конфигурации не совпадают")
    c = lookup(store.get_connection, who, body.connection_id, secret=True)
    try:
        return await test_model(c, validated_config(body.config))
    except (ProviderError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/configs", status_code=201)
async def config(
    body: ExperimentConfig,
    who: str = Depends(get_owner),
    store: Store = Depends(get_store),
):
    return lookup(store.save_config, who, validated_config(body))


@router.post("/estimate")
async def estimate(
    body: EstimateInput,
    who: str = Depends(get_owner),
    store: Store = Depends(get_store),
):
    cfg = validated_config(body.config)
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
    who: str = Depends(get_owner),
    store: Store = Depends(get_store),
):
    cfg = lookup(store.get_config, who, body.config_id)
    cfg = {
        **cfg,
        "business_date": datetime.now(UTC).date().isoformat(),
        "rubric_version": RUBRIC_VERSION,
    }
    snapshots = {s["id"]: s for s in scenarios()}
    cases = [
        {
            "id": uuid.uuid4().hex,
            "scenario_id": sid,
            "repeat": repeat,
            "status": "queued",
            "scenario_snapshot": snapshots[sid],
        }
        for repeat in range(1, cfg["repeats"] + 1)
        for sid in cfg["scenario_ids"]
    ]
    rev, sh = versions()
    return lookup(
        store.create_job,
        who,
        "experiment",
        cfg,
        cases,
        code_revision=rev,
        scenario_hash=sh,
        idempotency_key=body.idempotency_key,
    )


@router.get("/runs")
async def history(who: str = Depends(get_owner), store: Store = Depends(get_store)):
    from finbot.experiments.archive import configured
    from finbot.experiments.pricing import request_cost
    from finbot.experiments.request_reporting import billing_metrics

    runs = store.list_jobs(who, include_cases=True)
    statuses = store.result_sync_statuses()
    for item in runs:
        if "cost_basis" not in (item.get("metrics") or {}):
            tariff = request_cost({}, item.get("config") or {})
            billing = (
                tariff
                if tariff["cost_basis"] == "missing_tariff"
                else billing_metrics(item, store.billing_events(item["id"]))["run"]
            )
            item["metrics"] = {**(item.get("metrics") or {}), **billing}
        item.pop("cases", None)
        item["result_sync"] = {"enabled": configured(), **statuses.get(item["id"], {})}
    return {"runs": runs, "worker": store.worker_status()}


@router.get("/runs/{job_id}")
async def run(
    job_id: str,
    who: str = Depends(get_owner),
    store: Store = Depends(get_store),
):
    job = lookup(store.get_job, job_id, who)
    from finbot.experiments.archive import configured

    result = enrich_report(job, store.report_events(job_id))
    result["evaluation"] = store.evaluation_status(job_id)
    result["result_sync"] = {
        "enabled": configured(),
        **store.result_sync_statuses().get(job_id, {}),
    }
    return result


@router.post("/runs/{job_id}/sync", status_code=202)
async def retry_sync(
    job_id: str,
    who: str = Depends(get_owner),
    store: Store = Depends(get_store),
):
    from finbot.experiments.archive import configured

    lookup(store.get_job, job_id, who)
    if not configured():
        raise HTTPException(409, "Общая PostgreSQL не настроена на этом сервере.")
    return lookup(store.request_result_sync, job_id)


@router.post("/runs/{job_id}/cancel")
async def cancel(
    job_id: str,
    who: str = Depends(get_owner),
    store: Store = Depends(get_store),
):
    return lookup(store.cancel, who, job_id)


@router.get("/runs/{job_id}/events")
async def events(
    job_id: str,
    after: int = 0,
    who: str = Depends(get_owner),
    store: Store = Depends(get_store),
):
    lookup(store.get_job, job_id, who)
    return {"events": store.events(job_id, max(0, after))}


@router.get("/runs/{job_id}/export")
async def export(
    job_id: str,
    who: str = Depends(get_owner),
    store: Store = Depends(get_store),
):
    job = lookup(store.get_job, job_id, who)
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
    who: str = Depends(get_owner),
    store: Store = Depends(get_store),
):
    job = lookup(store.get_job, job_id, who)
    case = next((c for c in job["cases"] if c["id"] == case_id), None)
    if not case:
        raise HTTPException(404, "Сценарий не найден")
    if case["status"] != "completed":
        raise HTTPException(409, "Оценка доступна для завершённого сценария")
    assessment = {
        **(case.get("assessment") or {}),
        **body.model_dump(exclude_unset=True, exclude_none=True),
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
    who: str = Depends(get_owner),
    store: Store = Depends(get_store),
):
    c = lookup(store.get_connection, who, body.connection_id)
    if c["kind"] != "ollama":
        raise HTTPException(422, "Загрузка доступна только для Ollama")
    if body.model not in {m["id"] for m in LOCAL_CATALOG}:
        raise HTTPException(422, "Выберите модель из каталога загрузки")
    return lookup(
        store.create_job,
        who,
        "model_pull",
        {"connection_id": body.connection_id, "model": body.model},
        [],
    )
