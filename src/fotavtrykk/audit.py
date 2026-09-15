"""The blind-label audit set.

All 55 external points in the starter kit's scorer are multiplied by
`external_qualified`, which needs at least 100 labelled observations at >=99.5%
exact-entity precision and >=98% metric correctness. So labels are the critical
path, not the connectors: an unlabelled connector scores zero.

The honesty problem this module is built around
-----------------------------------------------
An audit exists to catch the identity gate being *wrong*. Re-running the gate's
own logic over its own output cannot do that — it will agree with itself every
time. So labels are separated by how they were produced, and the scorer refuses
to claim qualification on the weak ones:

  machine   The claim is a primary-key lookup, not an inference. A Brreg record
            fetched from /enheter/{org} that returns that same organisation
            number is true by construction. Independently re-checked here
            against the stored payload.
  assisted  Adjudicated from the captured evidence by something other than the
            gate that produced it. Useful signal, NOT sufficient for the gate.
  human     A person read the evidence and decided. The only kind that counts
            toward qualification for a risky observation.

Sampling matters as much as labelling. Only 10.9% of the universe has a website,
so a representative observation sample would be ~89% registry rows — which
audit nothing falsifiable. `sample_queue` deliberately over-weights the risky
strata, making the resulting precision a conservative lower bound.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import Envelope, Observation, publishable, validate_observation

# Risk tiers, by how falsifiable the entity claim is.
TIER_PRIMARY_KEY = "primary_key"   # the source is keyed by the organisation number
TIER_PROVEN = "proven_on_page"     # an organisation number was found in captured content
TIER_INFERRED = "inferred"         # name match, or a handle declared by a verified site

RISKY_TIERS = {TIER_PROVEN, TIER_INFERRED}

LABELLED_BY_MACHINE = "machine"
LABELLED_BY_ASSISTED = "assisted"
LABELLED_BY_HUMAN = "human"
STRONG_LABELS = {LABELLED_BY_MACHINE, LABELLED_BY_HUMAN}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def risk_tier(observation: Observation) -> str:
    proof = observation.identity_proof or ""
    if observation.platform == "brreg" and proof == "organisation_number_primary_key":
        return TIER_PRIMARY_KEY
    if proof.startswith("org_number_") or ":org_number_" in proof:
        return TIER_PROVEN
    return TIER_INFERRED


def load_pool(paths: Iterable[Path]) -> tuple[list[Observation], dict[str, Envelope]]:
    """Every observation across one or more envelope files, plus their envelopes."""
    pool: list[Observation] = []
    envelopes: dict[str, Envelope] = {}
    seen: set[str] = set()
    for path in paths:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                envelope = Envelope.model_validate_json(line)
            except Exception:  # noqa: BLE001
                continue
            envelopes[envelope.organisation_number] = envelope
            for observation in envelope.observations:
                if observation.id not in seen:
                    seen.add(observation.id)
                    pool.append(observation)
    return pool, envelopes


def _rank(seed: str, key: str) -> str:
    return hashlib.sha256(f"{seed}:{key}".encode()).hexdigest()


def sample_queue(
    pool: list[Observation], count: int, *, seed: str = "audit-1",
    risky_fraction: float = 0.75,
) -> list[Observation]:
    """Stratified, risk-weighted audit sample.

    Deterministic for a given seed, so the same queue can be regenerated and a
    second reviewer can be given exactly the same rows.
    """
    publishable_pool = [o for o in pool if publishable(o)]
    risky = [o for o in publishable_pool if risk_tier(o) in RISKY_TIERS]
    safe = [o for o in publishable_pool if risk_tier(o) == TIER_PRIMARY_KEY]

    want_risky = min(len(risky), int(round(count * risky_fraction)))
    want_safe = min(len(safe), count - want_risky)
    # If there are not enough risky rows, backfill rather than under-filling the
    # audit: the 100-label gate is absolute.
    if want_risky + want_safe < count:
        want_safe = min(len(safe), count - want_risky)

    def take(items: list[Observation], n: int) -> list[Observation]:
        buckets: dict[str, list[Observation]] = defaultdict(list)
        for item in items:
            buckets[f"{item.platform}|{item.signal_type}|{risk_tier(item)}"].append(item)
        for key in buckets:
            buckets[key].sort(key=lambda o: _rank(seed, o.id))
        chosen: list[Observation] = []
        depth = 0
        while len(chosen) < n:
            added = False
            for key in sorted(buckets):
                if depth < len(buckets[key]):
                    chosen.append(buckets[key][depth])
                    added = True
                    if len(chosen) == n:
                        return chosen
            if not added:
                return chosen
            depth += 1
        return chosen

    queue = take(risky, want_risky) + take(safe, want_safe)
    return sorted(queue, key=lambda o: _rank(seed, o.id))


def machine_verify(
    observation: Observation, envelopes: dict[str, Envelope], snapshot_dir: Path | None = None
) -> dict[str, Any] | None:
    """Label only what is true by construction, re-checked against the payload.

    A Brreg entity record fetched from /enheter/{org} that returns that same
    organisation number is a primary-key lookup — there is no entity resolution
    to get wrong. Anything requiring inference returns None and goes to review.
    """
    if risk_tier(observation) != TIER_PRIMARY_KEY:
        return None

    org = observation.organisation_number
    if f"/enheter/{org}" not in observation.source_url:
        return None

    # Re-read the stored payload rather than trusting the observation.
    confirmed = False
    if snapshot_dir:
        blob = Path(snapshot_dir) / f"{observation.content_sha256}.bin"
        if blob.exists():
            try:
                payload = json.loads(blob.read_text(encoding="utf-8"))
                confirmed = str(payload.get("organisasjonsnummer")) == org
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                confirmed = False
    if not confirmed:
        return None

    return {
        "id": observation.id,
        "exact_entity": True,
        "metric_correct": True,
        "sentiment_correct": None,
        "labelled_by": LABELLED_BY_MACHINE,
        "labelled_at": utc_now(),
        "basis": "registry payload re-read from snapshot returns this organisation number",
        "risk_tier": TIER_PRIMARY_KEY,
    }


def render_for_review(observation: Observation, envelope: Envelope | None) -> str:
    """Everything a reviewer needs to decide, without opening another tool."""
    lines = [
        f"observation : {observation.id}",
        f"company     : {observation.organisation_number}"
        + (f"  {envelope.legal_identity.get('legal_name')}" if envelope else ""),
        f"platform    : {observation.platform} / {observation.signal_type}",
        f"risk tier   : {risk_tier(observation)}",
        f"proof       : {observation.identity_proof}",
        f"source      : {observation.source_url}",
        f"retrieved   : {observation.retrieved_at}",
        f"hash        : {observation.content_sha256[:16]}...",
    ]
    if envelope:
        registered = {
            c.field: c.value for c in envelope.claims
            if c.field in {"business_address", "municipality", "industry_code", "registry_website"}
        }
        for field, value in registered.items():
            if value:
                lines.append(f"registry    : {field} = {str(value)[:90]}")
    if observation.evidence_span:
        lines.append(f"evidence    : {observation.evidence_span[:300]}")
    if observation.metrics:
        lines.append(f"metrics     : {json.dumps(observation.metrics, ensure_ascii=False)[:300]}")
    lines.append("")
    lines.append("  Does this observation belong to THIS exact legal entity?")
    lines.append("  Not a parent, subsidiary, franchise, brand or namesake.")
    return "\n".join(lines)


def load_labels(path: Path) -> dict[str, dict[str, Any]]:
    if not path or not Path(path).exists():
        return {}
    labels: dict[str, dict[str, Any]] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            labels[str(row["id"])] = row
    return labels


def write_labels(path: Path, labels: dict[str, dict[str, Any]]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        "".join(json.dumps(labels[k], ensure_ascii=False) + "\n" for k in sorted(labels)),
        encoding="utf-8",
    )


def score_audit(
    pool: list[Observation], labels: dict[str, dict[str, Any]], *, minimum_audit: int = 100
) -> dict[str, Any]:
    """Reproduce the starter kit's audit arithmetic, plus the honesty gates it
    cannot express: which labels are strong enough to count."""
    by_id = {o.id: o for o in pool}
    audited = [by_id[i] for i in labels if i in by_id]
    published = [o for o in audited if publishable(o)]

    wrong_entity = sum(1 for o in published if not labels[o.id].get("exact_entity", False))
    wrong_metric = sum(1 for o in published if not labels[o.id].get("metric_correct", False))
    unsupported = sum(1 for o in published if validate_observation(o))

    entity_precision = (len(published) - wrong_entity) / len(published) if published else 0.0
    metric_precision = (len(published) - wrong_metric) / len(published) if published else 0.0

    provenance = Counter(labels[o.id].get("labelled_by", "unknown") for o in published)
    risky = [o for o in published if risk_tier(o) in RISKY_TIERS]
    risky_human = [o for o in risky if labels[o.id].get("labelled_by") == LABELLED_BY_HUMAN]
    risky_strong = [o for o in risky if labels[o.id].get("labelled_by") in STRONG_LABELS]

    audit_size_gate = len(labels) >= minimum_audit
    precision_gate = bool(
        published and wrong_entity == 0 and unsupported == 0
        and entity_precision >= 0.995 and metric_precision >= 0.98
    )
    # A machine cannot certify its own inference. Risky rows need a person.
    independence_gate = bool(risky) and len(risky_human) == len(risky)

    return {
        "scorer": "fotavtrykk_audit_v1",
        "pool_observations": len(pool),
        "labelled": len(labels),
        "audited_observations": len(audited),
        "published_audited": len(published),
        "wrong_entity_publications": wrong_entity,
        "wrong_metric_publications": wrong_metric,
        "unsupported_publications": unsupported,
        "entity_precision": round(entity_precision, 5),
        "metric_precision": round(metric_precision, 5),
        "label_provenance": dict(sorted(provenance.items())),
        "risk_tiers": dict(sorted(Counter(risk_tier(o) for o in published).items())),
        "risky_published": len(risky),
        "risky_human_labelled": len(risky_human),
        "risky_unlabelled_by_human": len(risky) - len(risky_human),
        "minimum_audit": minimum_audit,
        "gates": {
            "audit_size": audit_size_gate,
            "precision": precision_gate,
            "human_labelled_risky": independence_gate,
        },
        # What the kit's evaluator would conclude from these labels.
        "provisional_qualification": bool(audit_size_gate and precision_gate),
        # What we are entitled to claim: risky rows must be human-adjudicated.
        "qualification_passed": bool(audit_size_gate and precision_gate and independence_gate),
        "claim_boundary": (
            "Machine labels cover primary-key lookups only. Assisted labels are "
            "signal, not certification. Qualification requires human adjudication "
            "of every risky observation."
        ),
    }
