"""Case-level dashboard slices, including terminal technical failures."""

import json
from collections import Counter, defaultdict

from finbot.experiments.analytics_statistics import apply_confidence_level
from finbot.experiments.research_metrics import condition_signature


def dashboard_metrics(rows, cases):
    from finbot.experiments.analytics import CASCADE, fraction

    outcomes = [r["research_outcome"] for r in rows if r["research_outcome"]["eligible"]]
    harms = [o for o in outcomes if o["harm_type"] != "unknown"]
    utilities = [o["utility_success"] for o in outcomes if type(o["utility_success"]) is bool]
    attacks = [
        o["attack_success"]
        for o in outcomes
        if o["attack_applicable"] and type(o["attack_success"]) is bool
    ]
    triggers = [r for r in rows if r["scenario"] != "baseline_clean" and r["type"]]
    statuses = Counter(c["execution_status"] for c in cases)
    terminal = sum(statuses[s] for s in ("completed", "failed", "interrupted"))
    return {
        "attempts": len(cases),
        "completed": len(rows),
        "statuses": dict(statuses),
        "metrics": {
            "harm": fraction(sum(o["harm_type"] != "none" for o in harms), len(harms)),
            "utility_failure": fraction(sum(not u for u in utilities), len(utilities)),
            "attack": fraction(sum(attacks), len(attacks)),
            "cascade": fraction(sum(r["type"] in CASCADE for r in triggers), len(triggers)),
            "technical": fraction(statuses["failed"] + statuses["interrupted"], terminal),
        },
    }


def build_dashboard(details, runs, cases, confidence_level):
    by_run = {r["id"]: r for r in runs if not r.get("is_demo")}
    completed = defaultdict(list)
    for row in details:
        completed[(row["run_id"], row["scenario"])].append(row)
    buckets = defaultdict(list)
    for case in cases:
        if case["run_id"] in by_run:
            buckets[(case["run_id"], case["scenario_id"])].append(case)
    run_rows, cells = [], defaultdict(lambda: {"rows": [], "cases": []})
    for (run_id, scenario), attempts in buckets.items():
        run = by_run[run_id]
        config = run.get("config") or {}
        row = {
            "run_id": run_id,
            "source_run_id": run.get("source_run_id") or run_id,
            "started_at": run.get("started_at") or (run.get("metadata") or {}).get("created_at"),
            "model": run.get("model") or "unknown",
            "scenario": scenario,
            "config": config,
            "protocol": run.get("protocol"),
            "versions": {
                k: (run.get("metadata") or {}).get(k)
                for k in ("code_revision", "scenario_hash", "evaluation_policy")
            },
        }
        signature = condition_signature(row)
        rows = completed[(run_id, scenario)]
        run_rows.append({**row, **dashboard_metrics(rows, attempts)})
        cell = cells[(row["model"], scenario, signature)]
        cell["rows"].extend(rows)
        cell["cases"].extend(attempts)
    cell_rows = [
        {
            "model": model,
            "scenario": scenario,
            "configuration_signature": json.loads(signature),
            **dashboard_metrics(bucket["rows"], bucket["cases"]),
        }
        for (model, scenario, signature), bucket in cells.items()
    ]
    result = {
        "runs": sorted(run_rows, key=lambda r: (r["started_at"] or "", r["run_id"])),
        "cells": cell_rows,
        "confidence_level": confidence_level,
    }
    apply_confidence_level(result, confidence_level)
    return result
