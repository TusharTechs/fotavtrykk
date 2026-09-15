"""Output contract types.

Field names in `Observation` mirror the Signalpost starter kit's
`validate_observation()` exactly, so our output passes their validator by
construction rather than by coincidence.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Availability(StrEnum):
    """The six states the contract allows. There is no seventh."""

    AVAILABLE = "available"
    NOT_AVAILABLE = "not_available"
    BLOCKED = "blocked"
    NOT_APPLICABLE = "not_applicable"
    AMBIGUOUS = "ambiguous"
    FAILED = "failed"


class SourceClass(StrEnum):
    OFFICIAL_REGISTRY = "official_registry"
    COMPANY_OWNED = "company_owned"
    CUSTOMER_REVIEW = "customer_review"
    EMPLOYEE_REVIEW = "employee_review"
    PUBLIC_NEWS = "public_news"
    LICENSED_NEWS = "licensed_news"
    PUBLIC_MENTION = "public_mention"


# --- starter-kit vocabularies (must match exactly) -----------------------

PLATFORMS = {
    "company_site", "google_places", "linkedin", "facebook", "instagram", "x",
    "youtube", "tiktok", "glassdoor", "indeed", "job_board", "news",
    "openstreetmap", "brreg", "apple_app_store", "google_play", "wikidata",
    "wikipedia", "company_directory",
}

SIGNAL_TYPES = {
    "company_profile", "profile_handle", "profile_metrics", "place_summary",
    "review", "review_summary", "job_posting", "workforce_snapshot",
    "public_post", "public_mention", "buzz_metrics",
}

PUBLISHABLE_ACQUISITION_MODES = {
    "official_api", "licensed_api", "company_authorized_export",
    "permitted_public_page",
}

INDEPENDENT_SENTIMENT_CLASSES = {
    "customer_review", "employee_review", "licensed_news", "public_news",
    "public_mention",
}

SPAN_REQUIRED_SIGNALS = {"review", "public_post", "public_mention"}


class Evidence(BaseModel):
    """An immutable record of one retrieval. Claims point at these by id."""

    model_config = ConfigDict(extra="forbid")

    id: str
    source_url: str
    source_class: str
    retrieved_at: str
    content_sha256: str
    final_url: str | None = None
    http_status: int | None = None
    claim_span: str | None = None
    extraction_method: str | None = None


class Claim(BaseModel):
    """A fact about the company, or an explicit statement that we do not have one."""

    model_config = ConfigDict(extra="forbid")

    field: str
    value: Any = None
    availability: Availability
    confidence: float = 0.0
    reporting_period: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    note: str | None = None


class Observation(BaseModel):
    """An external footprint signal. This is where the rubric's 55 points live."""

    model_config = ConfigDict(extra="forbid")

    id: str
    organisation_number: str
    platform: str
    signal_type: str
    source_url: str
    retrieved_at: str
    content_sha256: str
    exact_entity: bool
    identity_proof: str
    acquisition_mode: str
    rights_status: str
    source_class: str | None = None
    evidence_span: str | None = None
    sentiment_label: str | None = None
    sentiment_model_version: str | None = None
    metrics: dict[str, Any] | None = None
    observed_at: str | None = None


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organisation_number: str
    field: str
    change_type: str
    old_value: Any = None
    new_value: Any = None
    source_url: str | None = None
    retrieved_at: str | None = None
    old_content_sha256: str | None = None
    new_content_sha256: str | None = None


class RunInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    started_at: str
    completed_at: str
    terminal_status: str


class Operations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requests: int = 0
    runtime_ms: int = 0
    third_party_cost_usd: float = 0.0


class Envelope(BaseModel):
    """Exactly one of these per input organisation number. No exceptions."""

    model_config = ConfigDict(extra="forbid")

    organisation_number: str
    run: RunInfo
    legal_identity: dict[str, Any] = Field(default_factory=dict)
    claims: list[Claim] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    changes: list[Change] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    operations: Operations = Field(default_factory=Operations)


def validate_observation(item: Observation | dict[str, Any]) -> list[str]:
    """Reimplementation of the starter kit's publication gate.

    We run this on ourselves before emitting anything, so a rejected
    observation is caught locally instead of by the evaluator's audit.
    """
    data = item.model_dump() if isinstance(item, Observation) else dict(item)
    reasons: list[str] = []

    if not str(data.get("id") or "").strip():
        reasons.append("missing observation id")
    if not str(data.get("organisation_number") or "").isdigit():
        reasons.append("missing or invalid organisation number")
    if data.get("platform") not in PLATFORMS:
        reasons.append("unsupported platform")
    if data.get("signal_type") not in SIGNAL_TYPES:
        reasons.append("unsupported signal type")
    if not data.get("source_url") or "://" not in str(data.get("source_url")):
        reasons.append("missing or invalid source URL")
    if not data.get("retrieved_at"):
        reasons.append("missing retrieval time")
    if len(str(data.get("content_sha256") or "")) != 64:
        reasons.append("missing content hash")
    if not data.get("exact_entity"):
        reasons.append("exact legal entity is not verified")
    if not data.get("identity_proof"):
        reasons.append("missing exact-entity proof")
    if data.get("acquisition_mode") not in PUBLISHABLE_ACQUISITION_MODES:
        reasons.append("acquisition mode is not approved for publication")
    if data.get("rights_status") != "approved":
        reasons.append("source rights are not approved")
    if data.get("signal_type") in SPAN_REQUIRED_SIGNALS and not data.get("evidence_span"):
        reasons.append("missing evidence span")
    if data.get("sentiment_label") is not None:
        if data["sentiment_label"] not in {"positive", "neutral", "negative", "mixed"}:
            reasons.append("unsupported sentiment label")
        if data.get("source_class") not in INDEPENDENT_SENTIMENT_CLASSES:
            reasons.append("sentiment source is not independent")
        if not data.get("sentiment_model_version"):
            reasons.append("missing sentiment model version")
    return reasons


def publishable(item: Observation | dict[str, Any]) -> bool:
    return not validate_observation(item)
