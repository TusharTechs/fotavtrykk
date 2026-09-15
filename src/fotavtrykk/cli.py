"""One command, as the submission contract requires."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .batch import DEFAULT_REQUEST_BUDGET, run_batch
from .orgnr import is_valid, normalise


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fotavtrykk", description="Signalpost company research agent")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Research a batch of organisation numbers")
    run.add_argument("--organisations", required=True, type=Path)
    run.add_argument("--output", required=True, type=Path, help="JSONL of terminal envelopes")
    run.add_argument("--report", type=Path, help="Machine-readable run report")
    run.add_argument("--snapshots", type=Path, help="Directory for raw response snapshots")
    run.add_argument("--run-id", default="local-001")
    run.add_argument("--expected-count", type=int, help="Fail if the envelope count differs")
    run.add_argument("--request-budget", type=int, default=DEFAULT_REQUEST_BUDGET)
    run.add_argument("--concurrency", type=int, default=8)
    run.add_argument("--limit", type=int, help="Process only the first N organisations")

    args = parser.parse_args(argv)
    if args.command != "run":
        parser.error("unknown command")

    organisations = read_organisations(args.organisations)
    if args.limit:
        organisations = organisations[: args.limit]
    if not organisations:
        print("no valid organisation numbers in input", file=sys.stderr)
        return 2

    suspect = [o for o in organisations if not is_valid(o)]
    if suspect:
        print(f"warning: {len(suspect)} organisation numbers fail the MOD-11 check", file=sys.stderr)

    envelopes, report = asyncio.run(run_batch(
        organisations,
        run_id=args.run_id,
        request_budget=args.request_budget,
        snapshot_dir=args.snapshots,
        concurrency=args.concurrency,
    ))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for envelope in envelopes:
            handle.write(envelope.model_dump_json(exclude_none=False) + "\n")

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


if __name__ == "__main__":
    raise SystemExit(main())
