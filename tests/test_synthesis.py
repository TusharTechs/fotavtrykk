"""Synthesis. The rubric asks for explanations 'without making unsupported
claims', so most of these tests are about what must NOT appear."""

from fotavtrykk.models import (
    Availability, Change, Claim, Envelope, Evidence, Observation, Operations, RunInfo,
)
from fotavtrykk.synthesis import summarise

ORG = "987654321"


def claim(field, value, state=Availability.AVAILABLE, **kw):
    return Claim(field=field, value=value, availability=state,
                 confidence=1.0 if state == Availability.AVAILABLE else 0.0,
                 evidence_ids=["ev-registry"], **kw)


def envelope(claims, observations=(), changes=()):
    return Envelope(
        organisation_number=ORG,
        run=RunInfo(run_id="r", started_at="t", completed_at="t", terminal_status="completed"),
        legal_identity={"legal_name": "NORDLYS VERKSTED AS"},
        claims=list(claims),
        evidence=[Evidence(id="ev-registry", source_url="https://data.brreg.no/x",
                           source_class="official_registry", retrieved_at="t",
                           content_sha256="a" * 64)],
        observations=list(observations),
        changes=list(changes),
        operations=Operations(),
    )


def observation(platform="google_places", signal="place_summary"):
    return Observation(
        id=f"{ORG}-{platform}", organisation_number=ORG, platform=platform,
        signal_type=signal, source_url="https://maps/", retrieved_at="t",
        content_sha256="a" * 64, exact_entity=True, identity_proof="places_phone_matches_registry",
        acquisition_mode="official_api", rights_status="approved",
    )


BASE = [
    claim("legal_name", "NORDLYS VERKSTED AS"),
    claim("legal_form", "AS - Aksjeselskap"),
    claim("municipality", "TROMSØ"),
    claim("industry_code", "43.210 Elektrisk installasjonsarbeid"),
]


class TestNoUnsupportedClaims:
    def test_unavailable_values_never_appear(self):
        env = envelope(BASE + [
            claim("financials.revenue", None, Availability.NOT_AVAILABLE),
            claim("employees_registered", None, Availability.NOT_AVAILABLE),
            claim("places.rating", None, Availability.NOT_AVAILABLE),
        ])
        text = summarise(env)["text"]
        assert "revenue" not in text.lower()
        assert "employees are registered" not in text
        assert "rating" not in text.lower()

    def test_ambiguous_website_is_not_presented_as_verified(self):
        env = envelope(BASE + [
            claim("official_website", "https://parent-group.no",
                  Availability.AMBIGUOUS, note="lacks exact-entity evidence"),
        ])
        assert "parent-group.no" not in summarise(env)["text"]

    def test_a_preserved_stale_value_is_still_reported(self):
        """A failed refresh keeps the value; the summary should keep it too."""
        env = envelope(BASE + [claim("employees_registered", 12)])
        assert "12 employees are registered" in summarise(env)["text"]

    def test_every_cited_evidence_id_exists_in_the_envelope(self):
        env = envelope(BASE + [claim("employees_registered", 12)])
        summary = summarise(env)
        known = {e.id for e in env.evidence}
        assert summary["evidence_ids"]
        assert set(summary["evidence_ids"]).issubset(known)


class TestFaithfulness:
    def test_currency_travels_with_the_figure(self):
        env = envelope(BASE + [claim(
            "financials.revenue", 67_956_000_000.0,
            reporting_period="2025-01-01/2025-12-31",
            qualifiers={"currency": "USD", "statement_type": "SELSKAP"})])
        text = summarise(env)["text"]
        assert "USD 68.0bn" in text
        assert "NOK" not in text

    def test_group_accounts_are_labelled_as_such(self):
        env = envelope(BASE + [claim(
            "financials.revenue", 5_000_000.0,
            reporting_period="2025-01-01/2025-12-31",
            qualifiers={"currency": "NOK", "statement_type": "KONSERN"})])
        assert "group accounts" in summarise(env)["text"]

    def test_a_filed_zero_is_explained_not_hidden(self):
        env = envelope(BASE + [claim(
            "financials.revenue", 0, reporting_period="2025-01-01/2025-12-31",
            qualifiers={"currency": "NOK", "statement_type": "SELSKAP", "filed_zero": True})])
        assert "reported value rather than a missing one" in summarise(env)["text"]

    def test_bankruptcy_is_stated(self):
        env = envelope(BASE + [claim("operating_status", {
            "bankruptcy": True, "under_liquidation": False,
            "compulsory_liquidation": False, "in_group": False})])
        assert "registered as bankrupt" in summarise(env)["text"]

    def test_one_person_in_both_roles_is_not_repeated(self):
        env = envelope(BASE + [claim("managing_director", "Kari Nordmann"),
                               claim("board_chair", "Kari Nordmann")])
        text = summarise(env)["text"]
        assert "both registered managing director and board chair" in text
        assert text.count("Kari Nordmann") == 1

    def test_singular_and_plural_posts(self):
        one = envelope(BASE + [claim("activity.latest_post_date", "2026-01-22"),
                               claim("activity.posts", [{"title": "a"}])])
        many = envelope(BASE + [claim("activity.latest_post_date", "2026-01-22"),
                                claim("activity.posts", [{"title": "a"}, {"title": "b"}])])
        assert "1 dated post of its own" in summarise(one)["text"]
        assert "2 dated posts of its own" in summarise(many)["text"]


class TestUnknowns:
    def test_unknowns_carry_the_reason(self):
        env = envelope(BASE + [claim(
            "official_website", None, Availability.NOT_AVAILABLE,
            note="no website registered in Enhetsregisteret")])
        unknowns = {u["field"]: u for u in summarise(env)["unknowns"]}
        assert "no website registered" in unknowns["official_website"]["reason"]
        assert unknowns["official_website"]["state"] == "not_available"

    def test_available_fields_are_not_listed_as_unknown(self):
        env = envelope(BASE + [claim("employees_registered", 12)])
        assert "employees_registered" not in {u["field"] for u in summarise(env)["unknowns"]}

    def test_a_company_with_no_external_source_says_so(self):
        env = envelope(BASE)
        assert "rests on official records alone" in summarise(env)["text"]

    def test_a_company_with_external_sources_does_not(self):
        env = envelope(BASE + [claim("places.rating", 4.6)], [observation()])
        assert "rests on official records alone" not in summarise(env)["text"]


class TestDeterminism:
    """A sampled model would break the idempotent-refresh gate."""

    def test_same_envelope_yields_identical_text(self):
        env = envelope(BASE + [claim("employees_registered", 12)])
        assert summarise(env)["text"] == summarise(env)["text"]

    def test_material_changes_are_narrated(self):
        env = envelope(BASE, changes=[Change(
            organisation_number=ORG, field="leadership", change_type="new_role",
            materiality="material", new_value={"name": "Ola Hansen", "role": "managing_director"})])
        assert "Ola Hansen joined as managing director" in summarise(env)["text"]

    def test_minor_changes_are_not_narrated(self):
        env = envelope(BASE, changes=[Change(
            organisation_number=ORG, field="website_description",
            change_type="changed_description", materiality="minor")])
        assert "Since the previous run" not in summarise(env)["text"]
