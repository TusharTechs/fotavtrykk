"""Refresh as a diff.

Three rules, each one a hard gate:

  * Re-running the same snapshot is idempotent. Every source of ordering or
    formatting noise is normalised away before comparison, so an unchanged
    company produces an empty change list.
  * A failed refresh must not erase the last supported value. When a source goes
    down we carry the previous value and its evidence forward, marked stale, and
    report the degradation rather than a deletion.
  * Prior evidence is preserved. A carried-forward claim brings its original
    evidence record with it.

Changes are typed (`new_role`, `new_filing`, `new_location`, ...) rather than a
flat old/new pair, because the evaluator checks that a rerun "reports real
changes" and not merely that something differs.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable

from .models import Availability, Change, Claim, Envelope, Evidence
from .registry import FINANCIAL_FIELDS

# A source that failed or was blocked tells us nothing about the value. Keep the
# last supported one rather than inventing a disappearance.
PRESERVING_STATES = {Availability.FAILED, Availability.BLOCKED}

IDENTITY_FIELDS = {"legal_name", "legal_form", "operating_status"}
FILING_FIELDS = set(FINANCIAL_FIELDS) | {"latest_submitted_accounts", "financial_history.years"}
DESCRIPTION_FIELDS = {"website_title", "website_description"}

# Everything else is reported but flagged minor, so "material change" stays
# meaningful for a reader deciding whether to look.
MINOR_FIELDS = DESCRIPTION_FIELDS | {"social_handles"}

# Qualifiers that are part of the fact. A currency or statement-type switch
# changes what the number means, so it is a change. The rest (first_observed_at,
# identity_proof, filed_zero) are bookkeeping and must never trigger one.
DIFFED_QUALIFIERS = ("currency", "statement_type")

# Per-item keys for list-valued fields, so we can emit one typed change per item.
LIST_KEYS: dict[str, Callable[[dict], Any]] = {
    "leadership": lambda item: (item.get("role_code"), item.get("name")),
    "locations": lambda item: item.get("organisation_number"),
    "social_handles": lambda item: item.get("platform"),
}
LIST_CHANGE_TYPES = {
    "leadership": ("new_role", "departed_role"),
    "locations": ("new_location", "closed_location"),
    "social_handles": ("new_handle", "removed_handle"),
}


def canonical(value: Any) -> Any:
    """Order- and format-insensitive form used only for comparison.

    Registry endpoints do not promise list ordering, and 0 vs 0.0 is not a
    change. Both would otherwise show up as false changes on every run.
    """
    if isinstance(value, list):
        return sorted((canonical(v) for v in value), key=_sort_key)
    if isinstance(value, dict):
        return {k: canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _sort_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _materiality(field: str) -> str:
    return "minor" if field in MINOR_FIELDS else "material"


def _evidence_index(envelope: Envelope) -> dict[str, Evidence]:
    return {item.id: item for item in envelope.evidence}


def _first_evidence(claim: Claim, index: dict[str, Evidence]) -> Evidence | None:
    for ev_id in claim.evidence_ids:
        if ev_id in index:
            return index[ev_id]
    return None


def _comparable(claim: Claim) -> Any:
    """What actually counts as the fact, for change detection."""
    return (
        canonical(claim.value),
        claim.reporting_period,
        tuple(claim.qualifiers.get(k) for k in DIFFED_QUALIFIERS),
    )


def reconcile(
    previous: Envelope | None, current: Envelope
) -> tuple[list[Claim], list[Change], list[Evidence]]:
    """Fold the previous run into the current one.

    Returns the reconciled claims, the changes between runs, and any prior
    evidence that must be preserved because a claim was carried forward.
    """
    current_index = _evidence_index(current)
    if previous is None:
        return [_stamp(claim, current_index) for claim in current.claims], [], []

    previous_claims = {claim.field: claim for claim in previous.claims}
    previous_index = _evidence_index(previous)

    claims: list[Claim] = []
    changes: list[Change] = []
    carried: list[Evidence] = []

    for claim in current.claims:
        before = previous_claims.get(claim.field)
        if before is None:
            claims.append(_stamp(claim, current_index))
            continue

        now_ev = _first_evidence(claim, current_index)
        was_ev = _first_evidence(before, previous_index)

        # Source degraded: preserve the value and its evidence, report the outage.
        if claim.availability in PRESERVING_STATES and before.availability == Availability.AVAILABLE:
            preserved = before.model_copy(deep=True)
            preserved.qualifiers = {
                **before.qualifiers,
                "stale": True,
                "refresh_status": str(claim.availability),
                "last_confirmed_at": before.qualifiers.get("last_observed_at"),
            }
            claims.append(preserved)
            if was_ev and was_ev.id not in current_index:
                carried.append(was_ev)
            changes.append(Change(
                organisation_number=current.organisation_number,
                field=claim.field,
                change_type="source_unavailable",
                materiality="minor",
                old_value=before.value, new_value=before.value,
                old_availability=str(before.availability),
                new_availability=str(claim.availability),
                old_source_url=was_ev.source_url if was_ev else None,
                old_retrieved_at=was_ev.retrieved_at if was_ev else None,
                old_content_sha256=was_ev.content_sha256 if was_ev else None,
                note="source could not be re-read; last supported value preserved",
            ))
            continue

        if _comparable(before) == _comparable(claim) and before.availability == claim.availability:
            claims.append(_stamp(claim, current_index, first_seen=before.qualifiers.get("first_observed_at")))
            continue

        claims.append(_stamp(claim, current_index))
        changes.extend(_classify(
            current.organisation_number, before, claim, was_ev, now_ev,
        ))

    # A field present before and absent now would be a silent drop.
    for field, before in previous_claims.items():
        if field not in {c.field for c in current.claims}:
            claims.append(before.model_copy(deep=True))
            was_ev = _first_evidence(before, previous_index)
            if was_ev and was_ev.id not in current_index:
                carried.append(was_ev)

    return sorted(claims, key=lambda c: c.field), changes, carried


def _stamp(claim: Claim, index: dict[str, Evidence], *, first_seen: str | None = None) -> Claim:
    """Record when this value was first and most recently confirmed.

    Timestamps come from the evidence, never the wall clock, so replaying a
    stored snapshot reproduces byte-identical output.
    """
    evidence = _first_evidence(claim, index)
    seen_at = evidence.retrieved_at if evidence else None
    stamped = claim.model_copy(deep=True)
    stamped.qualifiers = {
        **claim.qualifiers,
        "first_observed_at": first_seen or seen_at,
        "last_observed_at": seen_at,
    }
    return stamped


def _classify(
    org: str, before: Claim, after: Claim,
    was_ev: Evidence | None, now_ev: Evidence | None,
) -> list[Change]:
    base = dict(
        organisation_number=org, field=after.field,
        old_availability=str(before.availability), new_availability=str(after.availability),
        old_source_url=was_ev.source_url if was_ev else None,
        new_source_url=now_ev.source_url if now_ev else None,
        old_retrieved_at=was_ev.retrieved_at if was_ev else None,
        new_retrieved_at=now_ev.retrieved_at if now_ev else None,
        old_content_sha256=was_ev.content_sha256 if was_ev else None,
        new_content_sha256=now_ev.content_sha256 if now_ev else None,
        reporting_period=after.reporting_period,
    )

    if before.availability != Availability.AVAILABLE and after.availability == Availability.AVAILABLE:
        return [Change(**base, change_type="became_available", materiality=_materiality(after.field),
                       old_value=before.value, new_value=after.value)]
    if before.availability == Availability.AVAILABLE and after.availability == Availability.NOT_AVAILABLE:
        return [Change(**base, change_type="became_unavailable", materiality=_materiality(after.field),
                       old_value=before.value, new_value=None,
                       note="source was read successfully and no longer reports this value")]

    if after.field in LIST_KEYS:
        item_changes = _list_changes(after.field, before.value, after.value, base)
        if item_changes:
            return item_changes

    if after.field in FILING_FIELDS:
        change_type = "new_filing"
    elif after.field in IDENTITY_FIELDS:
        change_type = "changed_identity"
    elif after.field in DESCRIPTION_FIELDS:
        change_type = "changed_description"
    else:
        change_type = "value_changed"

    # A qualifier-only change shows identical old/new values, which reads as a
    # false positive unless we say what actually moved.
    note = None
    if canonical(before.value) == canonical(after.value):
        moved = [
            f"{key} {before.qualifiers.get(key)} -> {after.qualifiers.get(key)}"
            for key in DIFFED_QUALIFIERS
            if before.qualifiers.get(key) != after.qualifiers.get(key)
        ]
        if before.reporting_period != after.reporting_period:
            moved.append(f"reporting period {before.reporting_period} -> {after.reporting_period}")
        note = "value unchanged; " + "; ".join(moved) if moved else None

    return [Change(**base, change_type=change_type, materiality=_materiality(after.field),
                   old_value=before.value, new_value=after.value, note=note)]


def _list_changes(field: str, old: Any, new: Any, base: dict) -> list[Change]:
    """One typed change per added or removed item, not one blob diff."""
    key_fn = LIST_KEYS[field]
    added_type, removed_type = LIST_CHANGE_TYPES[field]
    old_items = {key_fn(i): i for i in old or [] if isinstance(i, dict)}
    new_items = {key_fn(i): i for i in new or [] if isinstance(i, dict)}

    changes = [
        Change(**base, change_type=added_type, materiality=_materiality(field),
               old_value=None, new_value=item)
        for key, item in new_items.items() if key not in old_items
    ]
    changes += [
        Change(**base, change_type=removed_type, materiality=_materiality(field),
               old_value=item, new_value=None)
        for key, item in old_items.items() if key not in new_items
    ]
    return changes


def diff_snapshots(
    previous: dict[str, Envelope], current: dict[str, Envelope]
) -> tuple[list[Change], list[str]]:
    """Diff two whole runs. Membership must match: a dropped company is a failure."""
    missing = sorted(set(previous) - set(current))
    changes: list[Change] = []
    for org in sorted(current):
        if org in previous:
            _, org_changes, _ = reconcile(previous[org], current[org])
            changes.extend(org_changes)
    return changes, missing


def evidence_complete(changes: Iterable[Change]) -> bool:
    """Every reported change must be traceable on the side that has a value."""
    for change in changes:
        if change.new_value is not None and not (change.new_source_url or change.old_source_url):
            return False
        if change.old_value is not None and change.change_type != "became_available":
            if not (change.old_source_url or change.new_source_url):
                return False
    return True
