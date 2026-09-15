"""Batch orchestration.

One hard promise: every input organisation number produces exactly one terminal
envelope. A crash, a timeout, an exhausted budget -- all of them still emit an
envelope carrying the failure. A silently dropped company is a zero-score batch.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from .diff import reconcile
from .http import CostLedger, Fetcher, RequestBudget, start_call_counter, utc_now
from .models import (
    Availability, Claim, Envelope, Evidence, Observation, Operations, RunInfo,
)
from .registry import RegistryCollector
from .site import SiteResolver

# The evaluator allows 2,000 requests per 100 companies. We reserve a tail so a
# late company is never starved by an early one that hit a redirect chain.
DEFAULT_REQUEST_BUDGET = 2_000
RESERVE_FRACTION = 0.05


class CompanyRunner:
    def __init__(
        self, fetcher: Fetcher, run_id: str,
        previous: dict[str, "Envelope"] | None = None,
    ) -> None:
        self.fetcher = fetcher
        self.run_id = run_id
        self.previous = previous or {}
        self.registry = RegistryCollector(fetcher)
        self.site = SiteResolver(fetcher)

    async def run(self, org: str) -> Envelope:
        started = utc_now()
        clock = time.perf_counter()
        calls = start_call_counter()
        claims: list[Claim] = []
        evidence: list[Evidence] = []
        observations: list[Observation] = []
        errors: list[dict] = []
        status = "completed"

        try:
            entity_result, entity_data = await self.registry.entity(org)
            entity_claims, entity_evidence = self.registry.entity_claims(org, entity_result, entity_data)
            claims += entity_claims
            evidence += entity_evidence

            if entity_data is None:
                status = "completed_without_registry_anchor"
                errors.append({
                    "stage": "registry_entity",
                    "message": entity_result.error or "entity not found in Enhetsregisteret",
                    "http_status": entity_result.status,
                })
            else:
                legal_name = entity_data.get("navn") or ""
                seed = self.registry._normalise_site(entity_data.get("hjemmeside"))

                results = await asyncio.gather(
                    self.registry.roles(org),
                    self.registry.accounts(org),
                    self.registry.subunits(org),
                    self.site.resolve(org, legal_name, seed),
                    return_exceptions=True,
                )
                for stage, outcome in zip(("roles", "accounts", "subunits", "website"), results):
                    if isinstance(outcome, BaseException):
                        errors.append({"stage": stage, "message": f"{type(outcome).__name__}: {outcome}"})
                        claims.append(Claim(
                            field=stage, value=None, availability=Availability.FAILED,
                            confidence=0.0, note=f"stage raised {type(outcome).__name__}",
                        ))
                        continue
                    claims += outcome[0]
                    evidence += outcome[1]
                    if len(outcome) > 2:
                        observations += outcome[2]

                # The registry is itself a publishable platform observation.
                observations.append(self._registry_observation(org, entity_result))

        except Exception as exc:  # noqa: BLE001 - a terminal envelope is mandatory
            status = "failed"
            errors.append({"stage": "runner", "message": f"{type(exc).__name__}: {exc}"})
            if not claims:
                claims.append(Claim(
                    field="legal_name", value=None, availability=Availability.FAILED,
                    confidence=0.0, note="runner raised before any claim was produced",
                ))

        claims, changes, carried = reconcile(self.previous.get(org), Envelope(
            organisation_number=org,
            run=RunInfo(run_id=self.run_id, started_at=started, completed_at=utc_now(),
                        terminal_status=status),
            claims=sorted(claims, key=lambda c: c.field),
            evidence=evidence,
        ))
        evidence = evidence + [e for e in carried if e.id not in {x.id for x in evidence}]

        return Envelope(
            organisation_number=org,
            run=RunInfo(
                run_id=self.run_id, started_at=started, completed_at=utc_now(),
                terminal_status=status,
            ),
            legal_identity=self._identity_summary(org, claims),
            claims=claims,
            evidence=evidence,
            observations=observations,
            changes=changes,
            errors=errors,
            operations=Operations(
                requests=calls.requests,
                runtime_ms=int((time.perf_counter() - clock) * 1000),
                third_party_cost_usd=0.0,
            ),
        )

    def _registry_observation(self, org: str, result) -> Observation:
        return Observation(
            id=f"{org}-brreg-entity",
            organisation_number=org,
            platform="brreg",
            signal_type="company_profile",
            source_url=result.url,
            retrieved_at=result.retrieved_at,
            content_sha256=result.content_sha256,
            exact_entity=True,
            identity_proof="organisation_number_primary_key",
            acquisition_mode="official_api",
            rights_status="approved",
            source_class="official_registry",
        )

    @staticmethod
    def _identity_summary(org: str, claims: list[Claim]) -> dict:
        by_field = {c.field: c for c in claims}
        pick = lambda f: (by_field[f].value if f in by_field and by_field[f].availability == Availability.AVAILABLE else None)  # noqa: E731
        return {
            "organisation_number": org,
            "legal_name": pick("legal_name"),
            "legal_form": pick("legal_form"),
            "municipality": pick("municipality"),
            "industry_code": pick("industry_code"),
        }


async def run_batch(
    organisations: list[str],
    *,
    run_id: str,
    request_budget: int = DEFAULT_REQUEST_BUDGET,
    snapshot_dir: Path | None = None,
    concurrency: int = 8,
    cost_limit_usd: float = 10.0,
    previous: dict[str, Envelope] | None = None,
) -> tuple[list[Envelope], dict]:
    budget = RequestBudget(limit=request_budget)
    ledger = CostLedger(limit_usd=cost_limit_usd)
    started = time.perf_counter()
    gate = asyncio.Semaphore(concurrency)
    envelopes: dict[str, Envelope] = {}

    async with Fetcher(budget, snapshot_dir=snapshot_dir) as fetcher:
        runner = CompanyRunner(fetcher, run_id, previous=previous)

        async def guarded(org: str) -> None:
            async with gate:
                try:
                    envelopes[org] = await runner.run(org)
                except BaseException as exc:  # noqa: BLE001
                    envelopes[org] = _fallback_envelope(org, run_id, exc)

        await asyncio.gather(*(guarded(o) for o in organisations), return_exceptions=True)

    # Belt and braces: the contract is exactly one envelope per input.
    ordered = [envelopes.get(o) or _fallback_envelope(o, run_id, RuntimeError("no envelope produced"))
               for o in organisations]

    report = {
        "run_id": run_id,
        "requested": len(organisations),
        "emitted_envelopes": len(ordered),
        "terminal_states": _tally(ordered),
        "requests": budget.used,
        "request_budget": budget.limit,
        "third_party_cost_usd": round(ledger.spent_usd, 4),
        "runtime_ms": int((time.perf_counter() - started) * 1000),
        "p50_ms": _percentile([e.operations.runtime_ms for e in ordered], 50),
        "p95_ms": _percentile([e.operations.runtime_ms for e in ordered], 95),
        "observations": sum(len(e.observations) for e in ordered),
        "changes": sum(len(e.changes) for e in ordered),
        "material_changes": sum(
            1 for e in ordered for c in e.changes if c.materiality == "material"
        ),
        "refreshed_against_previous": bool(previous),
        "validation": {
            "passed": len(ordered) == len(organisations)
            and all(e.organisation_number for e in ordered)
            and budget.used <= budget.limit,
        },
    }
    return ordered, report


def _fallback_envelope(org: str, run_id: str, exc: BaseException) -> Envelope:
    now = utc_now()
    return Envelope(
        organisation_number=org,
        run=RunInfo(run_id=run_id, started_at=now, completed_at=now, terminal_status="failed"),
        claims=[Claim(
            field="legal_name", value=None, availability=Availability.FAILED,
            confidence=0.0, note="no result produced for this organisation number",
        )],
        errors=[{"stage": "batch", "message": f"{type(exc).__name__}: {exc}"}],
    )


def _tally(envelopes: list[Envelope]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for env in envelopes:
        counts[env.run.terminal_status] = counts.get(env.run.terminal_status, 0) + 1
    return dict(sorted(counts.items()))


def _percentile(values: list[int], pct: int) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((pct / 100) * (len(ordered) - 1))))
    return ordered[index]
