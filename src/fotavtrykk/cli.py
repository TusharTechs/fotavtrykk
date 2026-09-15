"""One command, as the submission contract requires."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .batch import DEFAULT_REQUEST_BUDGET, run_batch
from .diff import diff_snapshots, evidence_complete
from .orgnr import is_valid, normalise
from .snapshots import load_envelopes, manifest, write_envelopes, write_manifest


def read_organisations(path: Path) -> list[str]:
    """Accepts JSONL with an `organisation_number` field, or one number per line."""
    numbers: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("{"):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            candidate = row.get("organisation_number") or row.get("organisasjonsnummer")
        else:
            candidate = line
        digits = normalise(candidate)
        if len(digits) == 9:
            numbers.append(digits)
    # Preserve input order, drop duplicates: one envelope per input.
    return list(dict.fromkeys(numbers))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fotavtrykk", description="Signalpost company research agent")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Research a batch of organisation numbers")
    run.add_argument("--organisations", required=True, type=Path)
    run.add_argument("--output", required=True, type=Path, help="JSONL of terminal envelopes")
    run.add_argument("--previous", type=Path, help="Prior envelope snapshot to refresh against")
    run.add_argument("--report", type=Path, help="Machine-readable run report")
    run.add_argument("--manifest", type=Path, help="Organisation-number manifest for the output")
    run.add_argument("--snapshots", type=Path, help="Directory for raw response snapshots")
    run.add_argument("--run-id", default="local-001")
    run.add_argument("--expected-count", type=int, help="Fail if the envelope count differs")
    run.add_argument("--request-budget", type=int, default=DEFAULT_REQUEST_BUDGET)
    run.add_argument("--concurrency", type=int, default=8)
    run.add_argument("--limit", type=int, help="Process only the first N organisations")

    refresh = sub.add_parser(
        "refresh",
        help="Diff two stored snapshots offline. Makes zero outbound requests.",
    )
    refresh.add_argument("--previous", required=True, type=Path)
    refresh.add_argument("--current", required=True, type=Path)
    refresh.add_argument("--output", required=True, type=Path, help="JSONL of change events")
    refresh.add_argument("--report", type=Path)
    return parser


def _run(args: argparse.Namespace) -> int:
    organisations = read_organisations(args.organisations)
    if args.limit:
        organisations = organisations[: args.limit]
    if not organisations:
        print("no valid organisation numbers in input", file=sys.stderr)
        return 2

    suspect = [o for o in organisations if not is_valid(o)]
    if suspect:
        print(f"warning: {len(suspect)} organisation numbers fail the MOD-11 check", file=sys.stderr)

    previous = load_envelopes(args.previous) if args.previous else {}

    envelopes, report = asyncio.run(run_batch(
        organisations,
        run_id=args.run_id,
        request_budget=args.request_budget,
        snapshot_dir=args.snapshots,
        concurrency=args.concurrency,
        previous=previous,
    ))

    content_sha = write_envelopes(args.output, envelopes)
    report["content_sha256"] = content_sha
    report["previous_snapshot"] = str(args.previous) if args.previous else None

    if args.manifest:
        write_manifest(args.manifest, manifest(envelopes, run_id=args.run_id, content_sha256=content_sha))

    if args.expected_count is not None:
        report["expected_count"] = args.expected_count
        matched = len(envelopes) == args.expected_count
        report["validation"]["passed"] = bool(report["validation"]["passed"] and matched)
        if not matched:
            report["validation"]["reason"] = (
                f"expected {args.expected_count} envelopes, emitted {len(envelopes)}"
            )

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["validation"]["passed"] else 1


def _refresh(args: argparse.Namespace) -> int:
    previous = load_envelopes(args.previous)
    current = load_envelopes(args.current)
    if not current:
        print(f"no envelopes in {args.current}", file=sys.stderr)
        return 2

    changes, missing = diff_snapshots(previous, current)
    # Idempotency is the hard gate: a snapshot diffed against itself must be silent.
    self_changes, _ = diff_snapshots(current, current)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(c.model_dump_json(exclude_none=False) + "\n" for c in changes), encoding="utf-8"
    )

    by_type: dict[str, int] = {}
    for change in changes:
        by_type[change.change_type] = by_type.get(change.change_type, 0) + 1

    complete = evidence_complete(changes)
    idempotent = not self_changes
    report = {
        "scorer": "fotavtrykk_refresh_replay_v1",
        "previous_profiles": len(previous),
        "current_profiles": len(current),
        "profiles_fetched_this_run": 0,
        "changes": len(changes),
        "material_changes": sum(1 for c in changes if c.materiality == "material"),
        "change_types": dict(sorted(by_type.items())),
        "false_changes": len(self_changes),
        "idempotent_rerun": idempotent,
        "evidence_complete": complete,
        "dropped_organisations": missing,
        "preserved_on_failure": sum(1 for c in changes if c.change_type == "source_unavailable"),
        "qualification_passed": bool(idempotent and complete and not missing),
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["qualification_passed"] else 1


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return _run(args) if args.command == "run" else _refresh(args)


if __name__ == "__main__":
    raise SystemExit(main())
