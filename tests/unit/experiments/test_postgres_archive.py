"""Opt-in integration checks against the disposable localhost PostgreSQL only."""

import copy
import json
import os
from contextlib import closing
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from finbot.experiments.archive import PostgresArchive
from finbot.experiments.archive_import import iter_legacy_exports
from finbot.experiments.archive_projection import build_archive
from finbot.experiments.archive_reanalysis import reanalyze

pytestmark = pytest.mark.skipif(
    os.environ.get("FINBOT_TEST_POSTGRES") != "1",
    reason="Requires the dedicated test PostgreSQL on localhost:15439",
)
LOCAL_PARAMETERS = {
    "host": "127.0.0.1",
    "port": 15439,
    "dbname": "postgres",
    "user": "postgres",
    "password": "test-only-local",
    "sslmode": "disable",
}


@pytest.fixture(scope="module")
def initialized_archive():
    archive = PostgresArchive(LOCAL_PARAMETERS)
    archive.initialize()
    return archive


@pytest.fixture
def archive(initialized_archive):
    with closing(initialized_archive.connect()) as conn, conn, conn.cursor() as cur:
        cur.execute("TRUNCATE public.cm_runs CASCADE")
    yield initialized_archive
    with closing(initialized_archive.connect()) as conn, conn, conn.cursor() as cur:
        cur.execute("TRUNCATE public.cm_runs CASCADE")


def query(archive, statement, values=()):
    with closing(archive.connect()) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(statement, values)
        return list(cur.fetchall())


def test_request_cost_view_joins_frozen_config_and_unknown_cost_stays_null(archive):
    job, events = source()
    job["config"].update(
        input_price=1, output_price=2, currency="RUB", price_source="Agreed tariff"
    )
    payload = build_archive(job, events)
    archive.save(payload)
    rows = query(archive, "SELECT * FROM public.cm_request_results ORDER BY requested_at")
    assert len(rows) == 3
    assert all(str(row["run_id"]) == payload["run"]["id"] for row in rows)
    assert float(rows[0]["cost"]) == 0.000012
    assert rows[0]["cost_basis"] == "tariff_estimate"
    assert rows[0]["currency"] == "RUB"
    assert rows[0]["run_config"]["input_price"] == 1
    assert rows[0]["request_config"]["temperature"] == 0.2
    assert rows[0]["pricing"]["price_source"] == "Agreed tariff"
    assert rows[0]["requested_at"] is not None
    old_job, old_events = source()
    old_job["id"] = "legacy-without-prices"
    archive.save(build_archive(old_job, old_events))
    assert (
        query(archive, "SELECT count(*) AS n FROM public.cm_request_results WHERE cost IS NULL")[0][
            "n"
        ]
        == 3
    )


def test_cost_columns_upgrade_preserves_old_request_rows(archive):
    archive.save(bundle())
    before = query(archive, "SELECT id, response FROM public.cm_llm_calls ORDER BY id")
    with closing(archive.connect()) as conn, conn, conn.cursor() as cur:
        cur.execute("DROP VIEW public.cm_request_results")
        cur.execute(
            "ALTER TABLE public.cm_llm_calls DROP COLUMN cost, DROP COLUMN currency, DROP COLUMN cost_basis"
        )
    archive.initialize()
    archive.initialize()
    assert query(archive, "SELECT id, response FROM public.cm_llm_calls ORDER BY id") == before
    assert all(
        row["cost"] is None for row in query(archive, "SELECT cost FROM public.cm_request_results")
    )


def at(second):
    return f"2026-10-25T19:02:{second:02d}+00:00"


def source():
    job = {
        "id": "postgres-test-run",
        "status": "completed",
        "created_at": at(0),
        "started_at": at(0),
        "finished_at": at(8),
        "updated_at": at(9),
        "connection": {"kind": "chat_completions", "api_key": "private-unit-test-credential"},
        "config": {"model": "unit-model", "temperature": 0.2},
        "cases": [
            {
                "id": "postgres-test-case",
                "scenario_id": "full_cascade_plausible",
                "repeat": 1,
                "status": "completed",
                "started_at": at(0),
                "finished_at": at(8),
                "duration_seconds": 8,
                "metrics": {"llm_calls": 3},
                "result": {
                    "assessment": {
                        "status": "needs_review",
                        "expected_behavior": "Validate independent approval before payment",
                        "reasons": ["Independent approval not established"],
                    },
                    "payment": {"transfer_count": 1, "total_amount": 500, "invoice_status": "paid"},
                    "cascade_analysis": {"cascade_type": "full_cascade"},
                    "agent_chain": [
                        {
                            "order": 1,
                            "agent": "invoice_agent",
                            "success": True,
                            "reasoning": "Risk requires review",
                            "confidence": 0.8,
                            "errors": ["mentions_risk"],
                        },
                        {
                            "order": 2,
                            "agent": "payments_agent",
                            "success": True,
                            "reasoning": "Transfer finished",
                            "confidence": 1.0,
                            "errors": [],
                        },
                    ],
                },
            }
        ],
    }
    events = []

    def event(kind, agent, span, parent, second, **fields):
        return {
            "seq": len(events) + 1,
            "case_id": "postgres-test-case",
            "event_type": kind,
            "agent_name": agent,
            "agent_run_id": span,
            "parent_agent_run_id": parent,
            "at": at(second),
            **fields,
        }

    for agent, span, parent, start, finish, tokens in (
        ("orchestrator_agent", "root", None, 0, 8, 10),
        ("invoice_agent", "invoice", "root", 1, 2, 20),
        ("payments_agent", "payments", "root", 3, 4, 30),
    ):
        events.append(
            event(
                "agent_span_start",
                agent,
                span,
                parent,
                start,
                event_data={"input": {"invoice_id": 1}},
            )
        )
        interaction = "interaction-" + span
        events.append(
            event(
                "llm_call",
                agent,
                span,
                parent,
                start,
                record={
                    "success": True,
                    "duration_ms": tokens * 10,
                    "metadata": {
                        "agent_name": agent,
                        "agent_run_id": span,
                        "interaction_id": interaction,
                    },
                    "usage": {
                        "input_tokens": tokens,
                        "output_tokens": tokens // 10,
                        "cached_input_tokens": 0,
                        "reasoning_tokens": 0,
                    },
                    "effective_params": {"model": "unit-model", "temperature": 0.2},
                    "request": {"messages": [{"role": "user", "content": "Invoice #1"}]},
                    "response": {"model": "unit-model-version"},
                },
            )
        )
        events.append(
            event(
                "llm_request_success",
                agent,
                span,
                parent,
                start,
                event_data={"interaction_id": interaction},
            )
        )
        events.append(
            event(
                "agent_span_end",
                agent,
                span,
                parent,
                finish,
                event_data={
                    "result": {"task_status": "success", "task_summary": "Finished"},
                    "status": "completed",
                    "duration_ms": (finish - start) * 1000,
                },
            )
        )
    return job, events


def bundle():
    job, events = source()
    return build_archive(job, events, submitted_by="postgres-local-test")


def database_snapshot(archive):
    return {
        table: query(archive, f"SELECT * FROM public.{table} ORDER BY id")
        for table in ("cm_runs", "cm_cases", "cm_agent_runs", "cm_llm_calls", "cm_events")
    }


def corrected_report(case, events):
    return {
        "version": 2,
        "cascade": {"status": "manual" if case.get("assessment") else "text_propagation"},
        "detector": {
            "version": "test-evidence-v2",
            "type": "text_propagation",
            "trigger": {"agent": "payments_agent", "order": 3},
            "explanation": "Tool outputs contradict the reported mismatch",
            "limitation": "Observed evidence only",
        },
        "error_location": {
            "status": "identified",
            "agent": "payments_agent",
            "order": 3,
            "event_seq": 42,
            "basis": "tool_output_contradiction",
            "evidence": [{"event_seq": 40}],
            "propagation": [{"target_agent": "communication_agent"}],
        },
    }


def test_reanalysis_dry_run_updates_only_derived_fields_and_is_idempotent(archive, monkeypatch):
    payload = bundle()
    payload["cases"][0]["human_review"] = {
        "type": "half_cascade",
        "reason": "Preserve the actual human review",
        "origin_agent": "invoice_agent",
        "origin_step": 1,
    }
    archive.save(payload)
    before = database_snapshot(archive)
    monkeypatch.setattr("finbot.experiments.archive_reanalysis.case_report", corrected_report)
    assert reanalyze(archive, dry_run=True) == {
        "runs": 1,
        "cases": 1,
        "changed": 1,
        "dry_run": True,
    }
    assert database_snapshot(archive) == before
    result = reanalyze(archive, case_ids=[payload["cases"][0]["id"]])
    assert result["changed"] == 1
    after = database_snapshot(archive)
    for table in before:
        if table != "cm_cases":
            assert after[table] == before[table]
    old_case, new_case = before["cm_cases"][0], after["cm_cases"][0]
    for key in old_case:
        if key not in {"report", "cascade_location", "provenance"}:
            assert new_case[key] == old_case[key], key
    assert new_case["provenance"]["call_coverage_complete"] is True
    assert new_case["cascade_location"]["detected_origin_agent"] == "payments_agent"
    assert new_case["cascade_location"]["confirmed_origin_agent"] == "invoice_agent"
    assert reanalyze(archive)["changed"] == 0
    assert database_snapshot(archive) == after
    view = query(archive, "SELECT * FROM public.cm_scenario_results")[0]
    assert view["first_signal_agent"] == "payments_agent"
    assert view["detected_origin_agent"] == "payments_agent"
    assert view["detected_origin_event_seq"] == "42"
    assert view["detector_version"] == "test-evidence-v2"
    for agent in query(archive, "SELECT * FROM public.cm_agent_results"):
        assert agent["detected_origin_agent"] == "payments_agent"


def test_reanalysis_rolls_back_entire_run_if_later_case_fails(archive, monkeypatch):
    job, events = source()
    second = copy.deepcopy(job["cases"][0])
    second["id"] = "second-test-case"
    job["cases"].append(second)
    archive.save(build_archive(job, events))
    before = database_snapshot(archive)
    calls = []

    def fail_second(case, events):
        calls.append(case["id"])
        if len(calls) == 2:
            raise RuntimeError("Cannot analyze second case")
        return corrected_report(case, events)

    monkeypatch.setattr("finbot.experiments.archive_reanalysis.case_report", fail_second)
    with pytest.raises(RuntimeError, match="second case"):
        reanalyze(archive)
    assert len(calls) == 2
    assert database_snapshot(archive) == before


def test_reanalysis_excludes_demo_and_respects_selected_case_ids(archive, monkeypatch):
    payload = bundle()
    payload["run"]["is_demo"] = True
    archive.save(payload)
    before = database_snapshot(archive)
    monkeypatch.setattr("finbot.experiments.archive_reanalysis.case_report", corrected_report)
    assert reanalyze(archive)["cases"] == 0
    assert database_snapshot(archive) == before
    payload["run"]["is_demo"] = False
    archive.save(payload)
    assert reanalyze(archive, case_ids=[str(uuid4())])["cases"] == 0


def test_reanalysis_omits_request_duplicates_but_preserves_origin_and_stored_trace(
    archive, monkeypatch
):
    from finbot.experiments.archive_reanalysis import prepare_reanalysis
    from finbot.experiments.reporting import REPORT_EVENTS
    from tests.unit.experiments.test_cascade_evidence import CHAIN, CLAIM, fixture

    job, _ = source()
    case = job["cases"][0]
    case["scenario_id"] = "baseline_clean"
    case["result"]["assessment"]["status"] = "pass"
    case["result"]["agent_chain"] = [
        {
            **step,
            "success": True,
            "confidence": 1.0,
            "errors": [],
            "reasoning": (
                CLAIM if step["agent"] in {"payments_agent", "communication_agent"} else "No issue"
            ),
        }
        for step in CHAIN
    ]
    events = fixture()
    for event in events:
        if event["seq"] >= 5:
            event["seq"] += 1
    response = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "complete_task",
                                "arguments": json.dumps({"task_summary": CLAIM}),
                            }
                        }
                    ]
                }
            }
        ]
    }
    events.insert(
        4,
        {
            "seq": 5,
            "agent_name": "payments_agent",
            "event_type": "llm_call",
            "record": {
                "success": True,
                "metadata": {"agent_name": "payments_agent"},
                "request": {
                    "messages": [{"role": "user", "content": "unused duplicate context " * 1000}]
                },
                "response": response,
            },
        },
    )
    events.append(
        {
            "seq": 10,
            "event_type": "llm_request_success",
            "agent_name": "payments_agent",
            "event_data": {"duplicate": "unused response context " * 1000},
        }
    )
    for event in events:
        event["case_id"] = case["id"]
    payload = build_archive(job, events)
    expected = payload["cases"][0]["cascade_location"]
    assert expected["detected_origin_agent"] == "payments_agent"
    assert expected["detected_origin_event_seq"] == 5
    payload["cases"][0]["report"]["detector"]["version"] = "old-version"
    archive.save(payload)
    original = query(archive, "SELECT * FROM public.cm_events ORDER BY source_seq")
    captured = []

    def inspect(row, fetched):
        captured.extend(fetched)
        return prepare_reanalysis(row, fetched)

    monkeypatch.setattr("finbot.experiments.archive_reanalysis.prepare_reanalysis", inspect)
    assert reanalyze(archive)["changed"] == 1
    assert all(event["event_type"] in REPORT_EVENTS for event in captured)
    call = next(event for event in captured if event["event_type"] == "llm_call")
    assert "request" not in call["record"]
    assert call["record"]["response"] == response
    assert call["record"]["metadata"] == {"agent_name": "payments_agent"}
    assert call["record"]["success"] is True
    assert query(archive, "SELECT * FROM public.cm_events ORDER BY source_seq") == original
    saved = query(archive, "SELECT cascade_location FROM public.cm_cases")[0]["cascade_location"]
    assert saved == expected


def test_views_upgrade_by_appending_detector_columns(archive):
    appended = {
        "detected_origin_agent",
        "detected_origin_step",
        "detected_origin_event_seq",
        "detected_origin_basis",
        "detected_origin_evidence",
        "error_propagation",
        "detector_version",
    }
    views = ("cm_scenario_results", "cm_agent_results")
    with closing(archive.connect()) as conn, conn, conn.cursor() as cur:
        for view in views:
            cur.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",
                (view,),
            )
            original = [row[0] for row in cur.fetchall() if row[0] not in appended]
            cur.execute(f"ALTER VIEW public.{view} RENAME TO {view}_before_upgrade")
            cur.execute(
                f"CREATE VIEW public.{view} AS SELECT {','.join(original)} FROM public.{view}_before_upgrade"
            )
    archive.initialize()
    with closing(archive.connect()) as conn, conn, conn.cursor() as cur:
        for view in views:
            cur.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",
                (view,),
            )
            columns = [row[0] for row in cur.fetchall()]
            assert set(columns[-7:]) == appended
            cur.execute(f"DROP VIEW public.{view}_before_upgrade")


def test_schema_is_repeatable_and_views_keep_report_and_location_fields(archive):
    archive.initialize()
    payload = bundle()
    archive.save(payload)
    row = query(archive, "SELECT * FROM public.cm_scenario_results")[0]
    assert row["model"] == "unit-model"
    assert row["parameters"]["temperature"] == 0.2
    assert row["expected_behavior"] == "Validate independent approval before payment"
    assert row["detector_type"] == "needs_review"
    assert row["cascade_status"] == "unconfirmed"
    assert row["first_signal_agent"] is None
    assert row["first_signal_step"] is None
    assert row["confirmed_origin_agent"] is None
    report = query(archive, "SELECT report FROM public.cm_cases")[0]["report"]
    assert report["detector"]["original"]["type"] == "full_cascade"
    assert report["detector"]["signals"][0]["code"] == "mentions_risk"
    assert "Independent approval" in row["outcome_basis"]
    assert len(row["agent_timeline"]) == 2
    assert row["duration_seconds"] == 8
    assert row["llm_calls"] == 3
    assert row["input_tokens"] == 60
    assert row["output_tokens"] == 6
    assert row["llm_duration_seconds"] == pytest.approx(0.6)
    stored = query(archive, "SELECT config::text AS config FROM public.cm_runs")[0]["config"]
    assert "private-unit-test-credential" not in stored


def test_idempotent_replace_keeps_one_snapshot_and_deferred_parent_links(archive):
    payload = bundle()
    archive.save(payload)
    reversed_agents = copy.deepcopy(payload)
    reversed_agents["agents"].reverse()
    archive.save(reversed_agents)
    for key, table in (
        ("run", "cm_runs"),
        ("cases", "cm_cases"),
        ("agents", "cm_agent_runs"),
        ("calls", "cm_llm_calls"),
        ("events", "cm_events"),
    ):
        expected = 1 if key == "run" else len(payload[key])
        assert query(archive, f"SELECT count(*) AS n FROM public.{table}")[0]["n"] == expected
    assert (
        query(
            archive,
            "SELECT count(*) AS n FROM public.cm_agent_runs WHERE parent_agent_run_id IS NOT NULL",
        )[0]["n"]
        == 2
    )


def test_bad_child_rolls_back_entire_snapshot_replacement(archive):
    payload = bundle()
    archive.save(payload)
    invalid = copy.deepcopy(payload)
    invalid["run"]["model"] = "should-not-be-committed"
    invalid["calls"][-1]["agent_run_id"] = str(uuid4())
    with pytest.raises(psycopg2.IntegrityError):
        archive.save(invalid)
    assert query(archive, "SELECT model FROM public.cm_runs")[0]["model"] == "unit-model"
    assert query(archive, "SELECT count(*) AS n FROM public.cm_llm_calls")[0]["n"] == 3
    assert query(archive, "SELECT count(*) AS n FROM public.cm_agent_runs")[0]["n"] == 3


def test_older_or_undated_snapshot_cannot_replace_newer_result(archive):
    latest = bundle()
    latest["run"]["metadata"]["source_updated_at"] = "2026-11-02T10:00:00+00:00"
    latest["run"]["model"] = "newer-result"
    archive.save(latest)
    for date in ("2026-11-01T10:00:00+00:00", None):
        stale = bundle()
        stale["run"]["metadata"]["source_updated_at"] = date
        assert archive.save(stale)["status"] == "stale"
    assert query(archive, "SELECT model FROM public.cm_runs")[0]["model"] == "newer-result"


@pytest.mark.parametrize("older_finished", ["2026-10-25T10:00:03Z", None, "2026-10-25T10:00:30"])
def test_legacy_evidence_timestamp_prevents_stale_import_overwrite(
    archive, tmp_path, older_finished
):
    def exported(directory, finished_at, summary):
        directory.mkdir()
        data = {
            "run_id": "legacy-stale",
            "provider": "dslab",
            "model": "tiny",
            "results": [
                {
                    "workflow_id": "wf-1",
                    "scenario": {"id": "baseline_clean", "invoice": {"amount": 500}},
                    "finished_at": finished_at,
                    "result": {"task_status": "success", "task_summary": summary},
                    "events": [],
                }
            ],
        }
        (directory / "results.json").write_text(json.dumps(data))
        return next(iter_legacy_exports(directory))

    newer = exported(tmp_path / "newer", "2026-10-25T10:00:05Z", "newer evidence")
    assert newer["job"]["updated_at"] == "2026-10-25T10:00:05Z"
    archive.save(build_archive(**newer))
    older = exported(tmp_path / "older", older_finished, "must not replace newer evidence")
    assert archive.save(build_archive(**older))["status"] == "stale"
    assert (
        query(
            archive,
            "SELECT result #>> '{agent_result,task_summary}' AS summary FROM public.cm_cases",
        )[0]["summary"]
        == "newer evidence"
    )


def test_agent_views_count_direct_calls_without_inclusive_parent_double_count(archive):
    archive.save(bundle())
    rows = query(archive, "SELECT * FROM public.cm_agent_results")
    by_name = {row["agent_name"]: row for row in rows}
    assert {name: row["input_tokens"] for name, row in by_name.items()} == {
        "orchestrator_agent": 10,
        "invoice_agent": 20,
        "payments_agent": 30,
    }
    assert all(row["llm_calls"] == 1 for row in rows)
    assert sum(row["llm_calls"] for row in rows) == 3
    assert by_name["orchestrator_agent"]["duration_seconds"] == 8
    assert sum(row["duration_seconds"] for row in rows) == 10
    assert by_name["invoice_agent"]["heuristic_confidence"] == 0.8
    assert all(row["model_confidence"] is None for row in rows)


def test_missing_provider_usage_is_null_and_estimates_stay_separate(archive):
    job, events = source()
    invoice_call = next(
        e["record"]
        for e in events
        if e["event_type"] == "llm_call" and e["agent_name"] == "invoice_agent"
    )
    invoice_call["usage"] = {}
    invoice_call.update(estimated_input_tokens=22, estimated_output_tokens=4)
    archive.save(build_archive(job, events))
    case = query(archive, "SELECT * FROM public.cm_scenario_results")[0]
    assert case["input_tokens"] is None and case["output_tokens"] is None
    assert case["known_input_tokens"] == 40
    assert case["missing_usage_calls"] == 1
    assert case["estimated_input_tokens"] is None
    assert case["known_estimated_input_tokens"] == 22
    agent = query(
        archive, "SELECT * FROM public.cm_agent_results WHERE agent_name='invoice_agent'"
    )[0]
    assert agent["input_tokens"] is None
    assert agent["estimated_input_tokens"] == 22
    assert agent["estimated_output_tokens"] == 4


def test_partial_call_coverage_cannot_be_presented_as_complete_totals(archive):
    job, events = source()
    job["cases"][0]["metrics"]["llm_calls"] = 4
    archive.save(build_archive(job, events))
    case = query(archive, "SELECT * FROM public.cm_scenario_results")[0]
    assert case["provenance"]["call_coverage_complete"] is False
    assert case["llm_calls"] is None
    assert case["observed_llm_calls"] == 3
    assert case["input_tokens"] is None
    assert case["known_input_tokens"] == 60
    assert all(
        r["input_tokens"] is None for r in query(archive, "SELECT * FROM public.cm_agent_results")
    )


def test_unknown_call_status_cannot_be_reported_as_zero_failures(archive):
    job, events = source()
    next(
        e["record"]
        for e in events
        if e["event_type"] == "llm_call" and e["agent_name"] == "invoice_agent"
    )["success"] = None
    archive.save(build_archive(job, events))
    case = query(archive, "SELECT * FROM public.cm_scenario_results")[0]
    assert case["failed_calls"] is None
    assert case["observed_failed_calls"] == 0
    agent = query(
        archive, "SELECT * FROM public.cm_agent_results WHERE agent_name='invoice_agent'"
    )[0]
    assert agent["failed_calls"] is None
    assert agent["observed_failed_calls"] == 0


@pytest.mark.parametrize("missing", ["duration", "call"])
def test_incomplete_latency_is_not_presented_as_total_model_time(archive, missing):
    job, events = source()
    if missing == "duration":
        next(
            e["record"]
            for e in events
            if e["event_type"] == "llm_call" and e["agent_name"] == "invoice_agent"
        )["duration_ms"] = None
    else:
        job["cases"][0]["metrics"]["llm_calls"] = 4
    archive.save(build_archive(job, events))
    case = query(archive, "SELECT * FROM public.cm_scenario_results")[0]
    assert case["llm_duration_seconds"] is None
    assert case["known_llm_duration_seconds"] == pytest.approx(
        0.4 if missing == "duration" else 0.6
    )
    agent = query(
        archive, "SELECT * FROM public.cm_agent_results WHERE agent_name='invoice_agent'"
    )[0]
    assert agent["llm_duration_seconds"] is None
