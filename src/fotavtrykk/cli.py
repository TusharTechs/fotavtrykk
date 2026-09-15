"""One command, as the submission contract requires."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import audit as audit_mod
from . import sampling
from .batch import DEFAULT_REQUEST_BUDGET, run_batch
from .diff import diff_snapshots, evidence_complete
from .orgnr import is_valid, normalise
from .snapshots import load_envelopes, manifest, write_envelopes, write_manifest


def load_env_file(path: Path = Path(".env")) -> list[str]:
    """Read KEY=value lines into the environment without overwriting real ones.

    Keeps credentials out of shell history, out of the repository and out of any
    transcript: the value is read from disk and never echoed.
    """
    import os

    loaded: list[str] = []
    if not path.exists():
        return loaded
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def read_company_names(path: Path) -> dict[str, str]:
    """Legal names from the input file, used to seed candidate lookups."""
    names: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        org = normalise(row.get("organisation_number") or row.get("organisasjonsnummer"))
        name = row.get("name") or row.get("navn")
        if len(org) == 9 and name:
            names[org] = str(name)
    return names


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
    run.add_argument("--no-jobs", action="store_true", help="Skip the NAV vacancy feed")
    run.add_argument("--no-places", action="store_true", help="Skip Google Places")
    run.add_argument("--no-news", action="store_true", help="Skip company activity pages")
    run.add_argument("--no-wikidata", action="store_true", help="Skip Wikidata")
    run.add_argument("--places-cost-per-search", type=float,
                     help="Override the declared Places price per search (USD)")
    run.add_argument("--cost-limit", type=float, default=10.0,
                     help="Third-party spend cap for this batch (USD)")

    sm = sub.add_parser(
        "summarise",
        help="Regenerate summaries over stored envelopes. Makes no requests.",
    )
    sm.add_argument("--envelopes", required=True, type=Path)
    sm.add_argument("--output", required=True, type=Path)

    vw = sub.add_parser("viewer", help="Build the static evidence viewer")
    vw.add_argument("--envelopes", required=True, type=Path)
    vw.add_argument("--report", type=Path)
    vw.add_argument("--output", required=True, type=Path)

    pc = sub.add_parser(
        "places-check",
        help="Validate the Places key with a single search before spending a batch",
    )
    pc.add_argument("--organisations", required=True, type=Path)
    pc.add_argument("--limit", type=int, default=3)

    refresh = sub.add_parser(
        "refresh",
        help="Diff two stored snapshots offline. Makes zero outbound requests.",
    )
    refresh.add_argument("--previous", required=True, type=Path)
    refresh.add_argument("--current", required=True, type=Path)
    refresh.add_argument("--output", required=True, type=Path, help="JSONL of change events")
    refresh.add_argument("--report", type=Path)

    select = sub.add_parser("select", help="Choose companies from the frozen universe")
    select.add_argument("--universe", required=True, type=Path)
    select.add_argument("--count", required=True, type=int)
    select.add_argument("--output", required=True, type=Path)
    select.add_argument("--seed", default="fotavtrykk-1")
    select.add_argument("--website-fraction", type=float,
                        help="Risk-weighted selection: target share with a website")
    select.add_argument("--exclude", type=Path, help="JSONL of organisations to keep out")

    au = sub.add_parser("audit", help="Build and score the blind-label audit set")
    au_sub = au.add_subparsers(dest="audit_command", required=True)

    aq = au_sub.add_parser("queue", help="Sample a risk-weighted audit queue")
    aq.add_argument("--envelopes", required=True, type=Path, nargs="+")
    aq.add_argument("--count", type=int, default=120)
    aq.add_argument("--seed", default="audit-1")
    aq.add_argument("--risky-fraction", type=float, default=0.75)
    aq.add_argument("--output", required=True, type=Path)
    aq.add_argument("--labels", type=Path, help="Existing labels to write machine rows into")
    aq.add_argument("--snapshots", type=Path, help="Snapshot dir for machine verification")
    aq.add_argument("--review-sheet", type=Path, help="Human-readable queue for review")
    aq.add_argument("--top-up", type=int, default=0,
                    help="Machine-verify extra primary-key rows outside the queue "
                         "until this many labels exist, so humans only review risky rows")

    ar = au_sub.add_parser("review", help="Label the queue interactively (resumable)")
    ar.add_argument("--queue", required=True, type=Path)
    ar.add_argument("--envelopes", required=True, type=Path, nargs="+")
    ar.add_argument("--labels", required=True, type=Path)
    ar.add_argument("--reviewer", default="", help="Recorded on each label")
    ar.add_argument("--limit", type=int, help="Stop after N decisions this session")
    ar.add_argument("--relabel", nargs="+", default=[],
                    help="Observation ids to review again, overwriting their label")
    ar.add_argument("--briefs", type=Path, help="Verification briefs from `audit brief`")
    ar.add_argument("--no-cascade", action="store_true",
                    help="Do not apply a site verdict to handles declared on it")
    ar.add_argument("--tier", nargs="+", default=[],
                    help="Review only these risk tiers, weakest evidence first "
                         "(inferred, corroborated, proven_on_page, primary_key)")

    ab = au_sub.add_parser("brief", help="Gather verification evidence for pending rows")
    ab.add_argument("--queue", required=True, type=Path)
    ab.add_argument("--envelopes", required=True, type=Path, nargs="+")
    ab.add_argument("--labels", type=Path)
    ab.add_argument("--snapshots", type=Path)
    ab.add_argument("--output", required=True, type=Path)
    ab.add_argument("--request-budget", type=int, default=400)

    al = au_sub.add_parser("label", help="Apply adjudications from a decisions file")
    al.add_argument("--queue", required=True, type=Path)
    al.add_argument("--decisions", required=True, type=Path,
                    help="JSONL of {org|id, verdict, basis}")
    al.add_argument("--labels", required=True, type=Path)
    al.add_argument("--by", default=audit_mod.LABELLED_BY_ASSISTED,
                    choices=[audit_mod.LABELLED_BY_ASSISTED, audit_mod.LABELLED_BY_HUMAN])
    al.add_argument("--reviewer", default="")

    asc = au_sub.add_parser("score", help="Score observations against labels")
    asc.add_argument("--envelopes", required=True, type=Path, nargs="+")
    asc.add_argument("--labels", required=True, type=Path)
    asc.add_argument("--report", type=Path)
    asc.add_argument("--minimum-audit", type=int, default=100)
    return parser


def _summarise(args: argparse.Namespace) -> int:
    """Rebuild summaries from stored claims — no refetching, no cost."""
    from .snapshots import write_envelopes
    from .synthesis import summarise

    envelopes = list(load_envelopes(args.envelopes).values())
    if not envelopes:
        print(f"no envelopes in {args.envelopes}", file=sys.stderr)
        return 2
    for envelope in envelopes:
        envelope.summary = summarise(envelope)
    digest = write_envelopes(args.output, envelopes)
    words = [len(e.summary.get("text", "").split()) for e in envelopes]
    unknowns = [len(e.summary.get("unknowns", [])) for e in envelopes]
    print(json.dumps({
        "envelopes": len(envelopes),
        "median_words": sorted(words)[len(words) // 2],
        "median_unknowns_listed": sorted(unknowns)[len(unknowns) // 2],
        "with_no_external_source": sum(
            1 for e in envelopes if "rests on official records alone" in e.summary.get("text", "")),
        "content_sha256": digest,
    }, indent=2))
    return 0


def _viewer(args: argparse.Namespace) -> int:
    from . import viewer as viewer_mod

    envelopes = list(load_envelopes(args.envelopes).values())
    if not envelopes:
        print(f"no envelopes in {args.envelopes}", file=sys.stderr)
        return 2
    report = json.loads(args.report.read_text(encoding="utf-8")) if args.report else {}
    size = viewer_mod.write(args.output, envelopes, run_report=report)
    print(json.dumps({"companies": len(envelopes), "output": str(args.output),
                      "bytes": size}, indent=2))
    return 0


def _places_check(args: argparse.Namespace) -> int:
    """Spend a few cents to prove the key works before committing a batch."""
    import asyncio as _asyncio

    from .connectors import PlacesSource
    from .http import CostLedger, Fetcher, RequestBudget
    from .registry import RegistryCollector

    loaded = load_env_file()
    names = read_company_names(args.organisations)
    orgs = list(names)[: args.limit]
    if not orgs:
        print("no organisations with names in input", file=sys.stderr)
        return 2

    async def main() -> int:
        ledger = CostLedger(limit_usd=10.0)
        async with Fetcher(RequestBudget(50)) as fetcher:
            places = PlacesSource(fetcher, ledger=ledger)
            if not places.enabled:
                print(json.dumps({
                    "configured": False,
                    "env_file_keys_loaded": loaded,
                    "hint": "create a .env file containing GOOGLE_PLACES_API_KEY=<your key>",
                }, indent=2))
                return 2
            registry = RegistryCollector(fetcher)
            rows = []
            for org in orgs:
                _, entity = await registry.entity(org)
                identity = registry.identity_bundle(entity) if entity else {}
                claims, _, observations = await places.collect(
                    org, names[org], identity, None)
                rating = next((c for c in claims if c.field == "places.rating"), None)
                rows.append({
                    "organisation_number": org, "name": names[org],
                    "state": str(rating.availability) if rating else None,
                    "rating": rating.value if rating else None,
                    "proof": (observations[0].identity_proof if observations else None),
                    "note": rating.note if rating else None,
                })
            print(json.dumps({
                "configured": True,
                "searches": places.searches,
                "published": places.published,
                "spent_usd": round(ledger.spent_usd, 4),
                "cost_per_search_usd": places.cost_per_search,
                "projected_per_100_companies_usd": round(places.cost_per_search * 100, 2),
                "results": rows,
            }, ensure_ascii=False, indent=2))
            return 0
    return _asyncio.run(main())


def _run(args: argparse.Namespace) -> int:
    load_env_file()
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
        company_names=read_company_names(args.organisations),
        enable_jobs=not args.no_jobs,
        enable_places=not args.no_places,
        enable_news=not args.no_news,
        enable_wikidata=not args.no_wikidata,
        cost_limit_usd=args.cost_limit,
        places_cost_per_search=args.places_cost_per_search,
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


def _select(args: argparse.Namespace) -> int:
    exclude = set()
    if args.exclude and args.exclude.exists():
        exclude = {str(r.get("organisation_number")) for r in
                   (json.loads(l) for l in args.exclude.read_text(encoding="utf-8").splitlines() if l.strip())}

    rows = list(sampling.load_universe(args.universe))
    if args.website_fraction is not None:
        chosen = sampling.select_risk_weighted(
            rows, args.count, seed=args.seed,
            website_fraction=args.website_fraction, exclude=exclude,
        )
    else:
        chosen = sampling.select_representative(rows, args.count, seed=args.seed, exclude=exclude)

    sampling.write_jsonl(args.output, chosen)
    with_site = sum(1 for r in chosen if r.get("website"))
    report = {
        "universe": len(rows),
        "selected": len(chosen),
        "seed": args.seed,
        "with_website": with_site,
        "with_website_share": round(with_site / len(chosen), 4) if chosen else 0.0,
        "strata": len({sampling.stratum_key(r) for r in chosen}),
        "excluded": len(exclude),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if len(chosen) == args.count else 1


def _audit_queue(args: argparse.Namespace) -> int:
    pool, envelopes = audit_mod.load_pool(args.envelopes)
    queue = audit_mod.sample_queue(
        pool, args.count, seed=args.seed, risky_fraction=args.risky_fraction
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(o.model_dump_json(exclude_none=False) + "\n" for o in queue), encoding="utf-8"
    )

    labels = audit_mod.load_labels(args.labels) if args.labels else {}
    machine_added = 0
    for observation in queue:
        if observation.id in labels:
            continue
        verdict = audit_mod.machine_verify(observation, envelopes, args.snapshots)
        if verdict:
            labels[observation.id] = verdict
            machine_added += 1
    if args.labels:
        audit_mod.write_labels(args.labels, labels)

    if args.top_up and args.labels:
        extra = 0
        for observation in pool:
            if len(labels) >= args.top_up:
                break
            if observation.id in labels:
                continue
            verdict = audit_mod.machine_verify(observation, envelopes, args.snapshots)
            if verdict:
                labels[observation.id] = verdict
                extra += 1
        machine_added += extra
        audit_mod.write_labels(args.labels, labels)

    pending = [o for o in queue if o.id not in labels]
    if args.review_sheet:
        args.review_sheet.parent.mkdir(parents=True, exist_ok=True)
        blocks = [
            audit_mod.render_for_review(o, envelopes.get(o.organisation_number))
            for o in pending
        ]
        args.review_sheet.write_text(
            ("\n" + "-" * 78 + "\n").join(blocks) + "\n", encoding="utf-8"
        )

    tiers: dict[str, int] = {}
    for observation in queue:
        tier = audit_mod.risk_tier(observation)
        tiers[tier] = tiers.get(tier, 0) + 1
    print(json.dumps({
        "pool_observations": len(pool),
        "queued": len(queue),
        "risk_tiers": dict(sorted(tiers.items())),
        "machine_labelled": machine_added,
        "awaiting_human_review": len(pending),
        "review_sheet": str(args.review_sheet) if args.review_sheet else None,
    }, ensure_ascii=False, indent=2))
    return 0


def _audit_brief(args: argparse.Namespace) -> int:
    """Fetch what a reviewer would otherwise look up by hand, once, for all rows."""
    import asyncio as _asyncio

    from .http import Fetcher, RequestBudget
    from .models import Observation
    from .registry import RegistryCollector
    from .orgnr import name_tokens
    from .site import SiteResolver

    queue = [
        Observation.model_validate_json(line)
        for line in args.queue.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    labels = audit_mod.load_labels(args.labels) if args.labels else {}
    _, envelopes = audit_mod.load_pool(args.envelopes)

    # Only company sites need fetching; handles inherit their site's verdict.
    sites = [
        o for o in queue
        if o.signal_type == "company_profile" and o.platform == "company_site"
        and o.id not in labels
    ]
    # A Wikidata entity is not tautological the way a registry lookup is: the
    # P2333 statement is community-edited and could name the wrong company. So
    # it gets a brief too, cross-checking the entity against registry facts.
    wikis = [o for o in queue if o.platform == "wikidata" and o.id not in labels]

    async def gather() -> list[dict]:
        budget = RequestBudget(limit=args.request_budget)
        out: list[dict] = []
        async with Fetcher(budget, snapshot_dir=args.snapshots) as fetcher:
            collector = RegistryCollector(fetcher)

            async def one(observation: Observation) -> dict:
                org = observation.organisation_number
                brief = {
                    "id": observation.id,
                    "organisation_number": org,
                    "registry_lookup": audit_mod.BRREG_LOOKUP.format(org=org),
                    "site": observation.source_url,
                    "pages": [],
                    "ours_found_anywhere": False,
                    "conflicting_anywhere": [],
                    "span": None,
                }
                home = await fetcher.get(observation.source_url)
                if not home.ok:
                    brief["error"] = home.error
                    return brief

                parsed = SiteResolver._parse(home.text)
                summary = audit_mod.summarise_page(parsed["body_text"], org)
                brief["pages"].append({"url": home.final_url or home.url, **summary})
                brief["ours_found_anywhere"] |= summary["ours_found"]
                brief["span"] = brief["span"] or summary["span"]
                brief["conflicting_anywhere"] += summary["conflicting"]

                _, entity = await collector.entity(org)
                identity = collector.identity_bundle(entity) if entity else {}
                corroboration = SiteResolver._corroboration(parsed, identity)
                brief["corroborated"] = bool(corroboration)
                brief["corroboration"] = corroboration

                if not brief["ours_found_anywhere"]:
                    for url in audit_mod.contact_candidates(home.text, home.final_url or home.url):
                        page = await fetcher.get(url)
                        if not page.ok:
                            continue
                        inner = audit_mod.summarise_page(SiteResolver._parse(page.text)["body_text"], org)
                        brief["pages"].append({"url": page.final_url or page.url, **inner})
                        brief["conflicting_anywhere"] += inner["conflicting"]
                        if inner["ours_found"]:
                            brief["ours_found_anywhere"] = True
                            brief["span"] = inner["span"]
                            break
                brief["conflicting_anywhere"] = sorted(set(brief["conflicting_anywhere"]))
                return brief

            async def wiki(observation: Observation) -> dict:
                org = observation.organisation_number
                envelope = envelopes.get(org)
                claims = {c.field: c for c in (envelope.claims if envelope else [])}
                registry_name = (claims.get("legal_name").value
                                 if claims.get("legal_name") else None) or ""
                registry_site = (claims.get("registry_website").value
                                 if claims.get("registry_website") else None)
                label = (observation.metrics or {}).get("label") or ""
                wd_site = (claims.get("wikidata.website").value
                           if claims.get("wikidata.website") else None)
                shared = set(name_tokens(label)) & set(name_tokens(registry_name))
                return {
                    "id": observation.id, "organisation_number": org,
                    "registry_lookup": audit_mod.BRREG_LOOKUP.format(org=org),
                    "site": observation.source_url,
                    "pages": [],
                    "ours_found_anywhere": True,
                    "conflicting_anywhere": [],
                    "span": f"P2333 = {org}",
                    "corroborated": bool(shared) or bool(
                        wd_site and registry_site
                        and wd_site.split("//")[-1].strip("/").removeprefix("www.")
                        == registry_site.split("//")[-1].strip("/").removeprefix("www.")),
                    "corroboration": (
                        f"Wikidata label '{label}' shares {sorted(shared)} with the registry name"
                        if shared else
                        (f"Wikidata website {wd_site} matches the registry website"
                         if wd_site and registry_site else
                         f"Wikidata label '{label}' shares no tokens with registry name "
                         f"'{registry_name}' — check this one")),
                }

            out = list(await _asyncio.gather(
                *[one(o) for o in sites], *[wiki(o) for o in wikis]))
        return out

    briefs = _asyncio.run(gather())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(b, ensure_ascii=False) + "\n" for b in briefs), encoding="utf-8"
    )
    hints: dict[str, int] = {}
    for brief in briefs:
        key = audit_mod.brief_hint(brief).split(" - ")[0]
        hints[key] = hints.get(key, 0) + 1
    print(json.dumps({
        "sites_briefed": len(briefs),
        "organisation_number_found_on_site": sum(1 for b in briefs if b["ours_found_anywhere"]),
        "conflicting_number_seen": sum(1 for b in briefs if b["conflicting_anywhere"]),
        "hints": dict(sorted(hints.items())),
        "output": str(args.output),
    }, ensure_ascii=False, indent=2))
    return 0


def _audit_review(args: argparse.Namespace) -> int:
    """Label pending observations one at a time. Writes after every decision, so
    a session can be interrupted and resumed without losing work."""
    from .models import Observation

    queue = [
        Observation.model_validate_json(line)
        for line in args.queue.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    _, envelopes = audit_mod.load_pool(args.envelopes)
    labels = audit_mod.load_labels(args.labels)
    for observation_id in args.relabel:
        labels.pop(observation_id, None)
    if args.relabel:
        audit_mod.write_labels(args.labels, labels)
        print(f"cleared {len(args.relabel)} label(s) for re-review")
    pending = [o for o in queue if o.id not in labels]
    if args.tier:
        pending = [o for o in pending if audit_mod.risk_tier(o) in set(args.tier)]
    # Sites first: judging one cascades to every handle declared on it, so the
    # reviewer never decides a handle whose site is still an open question.
    pending.sort(key=lambda o: (o.signal_type != "company_profile", o.organisation_number, o.id))

    if not pending:
        print(f"nothing pending: all {len(queue)} queued observations are labelled")
        return 0

    briefs = {}
    if args.briefs and args.briefs.exists():
        for line in args.briefs.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                briefs[row["id"]] = row

    print(f"{len(pending)} of {len(queue)} awaiting review "
          f"({len(labels)} already labelled)\n"
          "  y = belongs to this exact entity        n = wrong company\n"
          "  m = right company, metric/value wrong   s = skip\n"
          "  q = save and quit\n")

    decided = 0
    for index, observation in enumerate(pending, 1):
        if args.limit and decided >= args.limit:
            break
        print("=" * 78)
        print(f"[{index}/{len(pending)}]")
        print(audit_mod.render_for_review(observation, envelopes.get(observation.organisation_number)))
        if observation.id in briefs:
            print(audit_mod.render_brief(briefs[observation.id]))
        try:
            answer = input("  [y/n/m/s/q] > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\ninterrupted")
            break
        if answer == "q":
            break
        if answer == "s" or answer not in {"y", "n", "m"}:
            continue

        labels[observation.id] = {
            "id": observation.id,
            "exact_entity": answer in {"y", "m"},
            "metric_correct": answer == "y",
            "sentiment_correct": None,
            "labelled_by": audit_mod.LABELLED_BY_HUMAN,
            "labelled_at": audit_mod.utc_now(),
            "reviewer": args.reviewer or None,
            "risk_tier": audit_mod.risk_tier(observation),
        }
        # A verdict on a root settles everything that inherited from it: handles
        # the company links from its own verified site, and the sitelinks and
        # statements hanging off a Wikidata entity.
        if not args.no_cascade:
            for other in audit_mod.dependents(observation, queue):
                if other.id in labels:
                    continue
                labels[other.id] = {
                    **labels[observation.id], "id": other.id,
                    "risk_tier": audit_mod.risk_tier(other),
                    "derived_from": observation.id,
                    "basis": f"inherited from {observation.platform} root judged by the reviewer",
                }
        audit_mod.write_labels(args.labels, labels)  # crash-safe: persist each decision
        decided += 1

    remaining = sum(1 for o in queue if o.id not in labels)
    print(f"\n{decided} labelled this session · {len(labels)} total · {remaining} remaining")
    return 0


def _audit_label(args: argparse.Namespace) -> int:
    """Apply recorded adjudications, cascading a site verdict to its handles."""
    from .models import Observation

    queue = [
        Observation.model_validate_json(line)
        for line in args.queue.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    decisions = [
        json.loads(line) for line in args.decisions.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    labels = audit_mod.load_labels(args.labels)
    applied = cascaded = 0

    for decision in decisions:
        verdict = decision["verdict"]
        targets = [
            o for o in queue
            # `platform` matters as well as `signal_type`: a brreg entity row is
            # also a company_profile, and a verdict about a website must never
            # land on the registry lookup that anchors the company.
            if (o.id == decision.get("id")
                or (decision.get("org") == o.organisation_number
                    and o.signal_type == "company_profile"
                    and o.platform == "company_site"))
        ]
        for target in targets:
            labels[target.id] = {
                "id": target.id,
                "exact_entity": verdict in {"y", "m"},
                "metric_correct": verdict == "y",
                "sentiment_correct": None,
                "labelled_by": args.by,
                "labelled_at": audit_mod.utc_now(),
                "reviewer": args.reviewer or None,
                "risk_tier": audit_mod.risk_tier(target),
                "basis": decision.get("basis"),
            }
            applied += 1
            for other in audit_mod.dependents(target, queue):
                labels[other.id] = {
                    **labels[target.id], "id": other.id,
                    "risk_tier": audit_mod.risk_tier(other),
                    "derived_from": target.id,
                    "basis": f"inherited from the {target.platform} root adjudicated above",
                }
                cascaded += 1

    audit_mod.write_labels(args.labels, labels)
    print(json.dumps({
        "decisions": len(decisions), "sites_labelled": applied,
        "handles_cascaded": cascaded, "labelled_by": args.by,
        "total_labels": len(labels),
    }, ensure_ascii=False, indent=2))
    return 0


def _audit_score(args: argparse.Namespace) -> int:
    pool, _ = audit_mod.load_pool(args.envelopes)
    labels = audit_mod.load_labels(args.labels)
    report = audit_mod.score_audit(pool, labels, minimum_audit=args.minimum_audit)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["qualification_passed"] else 1


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "run":
        return _run(args)
    if args.command == "refresh":
        return _refresh(args)
    if args.command == "select":
        return _select(args)
    if args.command == "places-check":
        return _places_check(args)
    if args.command == "viewer":
        return _viewer(args)
    if args.command == "summarise":
        return _summarise(args)
    if args.command == "audit":
        if args.audit_command == "queue":
            return _audit_queue(args)
        if args.audit_command == "brief":
            return _audit_brief(args)
        if args.audit_command == "review":
            return _audit_review(args)
        if args.audit_command == "label":
            return _audit_label(args)
        return _audit_score(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
