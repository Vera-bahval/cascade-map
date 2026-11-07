"""Create shared result tables or import saved experiments without running models."""

import argparse
import hashlib
import json
from pathlib import Path
from uuid import UUID

from finbot.config import settings
from finbot.experiments.archive import PostgresArchive, error_message, sync_once


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create shared PostgreSQL tables and views")
    check = commands.add_parser("check", help="Check archive connection, TLS and schema")
    check.add_argument(
        "--write-probe",
        action="store_true",
        help="Verify a linked test write and always roll it back",
    )
    load = commands.add_parser("import", help="Import existing SQLite or legacy JSON results")
    load.add_argument("--sqlite", type=Path, action="append", default=[])
    load.add_argument("--legacy-dir", type=Path, action="append", default=[])
    load.add_argument("--dry-run", action="store_true")
    commands.add_parser("sync", help="Send pending local results once; never run a model")
    analysis = commands.add_parser("reanalyze", help="Recompute reports using archived traces only")
    analysis.add_argument("--dry-run", action="store_true")
    analysis.add_argument("--case-id", type=UUID, action="append", default=[])
    args = parser.parse_args()
    try:
        if args.command == "check":
            from finbot.experiments.archive_health import check_archive

            result = check_archive(PostgresArchive(), write_probe=args.write_probe)
            print(json.dumps(result))
            return 0 if result["ok"] else 1
        if args.command == "init":
            PostgresArchive().initialize()
            print(
                "Initialized result archive, series, agent iterations, separate evaluations and result views."
            )
            return 0
        if args.command == "reanalyze":
            from finbot.experiments.archive_reanalysis import reanalyze

            result = reanalyze(PostgresArchive(), dry_run=args.dry_run, case_ids=args.case_id)
            print(json.dumps(result))
            return 0
        if args.command == "sync":
            from finbot.experiments.store import Store

            store = Store()
            archive = PostgresArchive()
            while sync_once(store, archive):
                pass
            states = list(store.result_sync_statuses().values())
            print(
                json.dumps(
                    {
                        "synced": sum(x["status"] == "synced" for x in states),
                        "pending_or_error": sum(x["status"] != "synced" for x in states),
                    }
                )
            )
            return int(any(x["status"] == "error" for x in states))
        if not args.sqlite and not args.legacy_dir:
            parser.error("Provide --sqlite or --legacy-dir")
        from finbot.experiments.archive_import import iter_legacy_exports, iter_sqlite_exports
        from finbot.experiments.archive_projection import build_archive

        archive = None if args.dry_run else PostgresArchive()
        sources = [iter_sqlite_exports(path) for path in args.sqlite]
        sources += [iter_legacy_exports(path) for path in args.legacy_dir]
        seen = {}
        totals = {
            "runs": 0,
            "cases": 0,
            "agents": 0,
            "calls": 0,
            "events": 0,
            "skipped_demo": 0,
            "duplicate_sources": 0,
        }
        for source in sources:
            for item in source:
                identity = (item["source_kind"], item["source_run_id"])
                bundle = build_archive(**item, submitted_by=settings.FINBOT_RESULTS_PG_USER or None)
                fingerprint = hashlib.sha256(
                    json.dumps(
                        {"job": item["job"], "events": item["events"]},
                        sort_keys=True,
                        ensure_ascii=False,
                        default=str,
                    ).encode()
                ).hexdigest()
                if identity in seen:
                    if seen[identity] != fingerprint:
                        raise ValueError(
                            "Conflicting duplicate sources; choose one canonical source for this run"
                        )
                    totals["duplicate_sources"] += 1
                    continue
                seen[identity] = fingerprint
                if bundle["run"]["is_demo"]:
                    totals["skipped_demo"] += 1
                    continue
                if archive:
                    archive.save(bundle)
                totals["runs"] += 1
                for key in ("cases", "agents", "calls", "events"):
                    totals[key] += len(bundle[key])
                print(
                    json.dumps(
                        {
                            "source_run_id": item["source_run_id"],
                            "model": bundle["run"]["model"],
                            "cases": len(bundle["cases"]),
                            "agents": len(bundle["agents"]),
                            "calls": len(bundle["calls"]),
                            "dry_run": args.dry_run,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        print(json.dumps({"totals": totals, "dry_run": args.dry_run}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(error_message(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
