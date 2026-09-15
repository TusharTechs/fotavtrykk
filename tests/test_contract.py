"""Output-contract guarantees. These encode hard gates, so they must not be relaxed."""

import json

import pytest

from fotavtrykk.batch import _fallback_envelope, _percentile, _tally
from fotavtrykk.models import (
    Availability, Claim, Envelope, Observation, publishable, validate_observation,
)

HASH = "a" * 64


def observation(**overrides) -> Observation:
    base = dict(
        id="987654321-brreg-entity",
        organisation_number="987654321",
        platform="brreg",
        signal_type="company_profile",
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/987654321",
        retrieved_at="2026-09-15T10:00:00Z",
        content_sha256=HASH,
        exact_entity=True,
        identity_proof="organisation_number_primary_key",
        acquisition_mode="official_api",
        rights_status="approved",
    )
    return Observation(**{**base, **overrides})


class TestAvailabilityStates:
    def test_only_the_six_contract_states_exist(self):
        assert {s.value for s in Availability} == {
            "available", "not_available", "blocked",
            "not_applicable", "ambiguous", "failed",
        }

    def test_invented_states_are_rejected(self):
        with pytest.raises(Exception):
            Claim(field="x", availability="verified")


class TestObservationGate:
    def test_a_well_formed_registry_observation_publishes(self):
        assert publishable(observation())

    def test_unproven_entity_is_rejected(self):
        assert "exact legal entity is not verified" in validate_observation(
            observation(exact_entity=False)
        )

    def test_unapproved_acquisition_mode_is_rejected(self):
        reasons = validate_observation(observation(acquisition_mode="unofficial_api_experiment"))
        assert "acquisition mode is not approved for publication" in reasons

    def test_unknown_platform_is_rejected(self):
        assert "unsupported platform" in validate_observation(observation(platform="myspace"))

    def test_review_without_evidence_span_is_rejected(self):
        reasons = validate_observation(observation(
            platform="google_places", signal_type="review", acquisition_mode="official_api",
        ))
        assert "missing evidence span" in reasons

    def test_sentiment_requires_independent_source_and_model_version(self):
        reasons = validate_observation(observation(
            platform="news", signal_type="public_mention", evidence_span="text",
            sentiment_label="positive", source_class="company_owned",
        ))
        assert "sentiment source is not independent" in reasons
        assert "missing sentiment model version" in reasons

    def test_short_hash_is_rejected(self):
        assert "missing content hash" in validate_observation(observation(content_sha256="abc"))


class TestTerminalEnvelopeGuarantee:
    def test_fallback_envelope_is_still_terminal_and_serialisable(self):
        env = _fallback_envelope("987654321", "run-1", RuntimeError("boom"))
        assert env.organisation_number == "987654321"
        assert env.run.terminal_status == "failed"
        assert env.claims[0].availability is Availability.FAILED
        assert env.errors and "boom" in env.errors[0]["message"]
        json.loads(env.model_dump_json())

    def test_failure_never_becomes_a_zero_value(self):
        env = _fallback_envelope("987654321", "run-1", RuntimeError("boom"))
        assert env.claims[0].value is None

    def test_envelope_rejects_unknown_fields(self):
        with pytest.raises(Exception):
            Envelope(organisation_number="1", run={
                "run_id": "r", "started_at": "t", "completed_at": "t",
                "terminal_status": "completed",
            }, surprise=True)


class TestReportHelpers:
    def test_tally_counts_terminal_states(self):
        envs = [_fallback_envelope(str(i), "r", RuntimeError("x")) for i in range(3)]
        assert _tally(envs) == {"failed": 3}

    def test_percentile_is_stable_on_small_samples(self):
        assert _percentile([], 95) == 0
        assert _percentile([5], 95) == 5
        assert _percentile([1, 2, 3, 4, 100], 95) == 100
