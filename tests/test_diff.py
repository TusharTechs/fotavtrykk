"""Refresh behaviour. Each class here pins a hard gate — do not relax these."""

from fotavtrykk.diff import canonical, diff_snapshots, evidence_complete, reconcile
from fotavtrykk.models import (
    Availability, Claim, Envelope, Evidence, Operations, RunInfo,
)

ORG = "987654321"


def evidence(ev_id="ev-registry", *, url="https://data.brreg.no/x", at="2026-09-15T10:00:00Z",
             digest="a" * 64):
    return Evidence(
        id=ev_id, source_url=url, source_class="official_registry",
        retrieved_at=at, content_sha256=digest,
    )


def envelope(claims, *, ev=None, run_id="r1"):
    return Envelope(
        organisation_number=ORG,
        run=RunInfo(run_id=run_id, started_at="2026-09-15T10:00:00Z",
                    completed_at="2026-09-15T10:00:05Z", terminal_status="completed"),
        claims=claims,
        evidence=ev if ev is not None else [evidence()],
        operations=Operations(),
    )


def claim(field, value, state=Availability.AVAILABLE, ev_id="ev-registry", **kw):
    return Claim(field=field, value=value, availability=state,
                 confidence=1.0 if state == Availability.AVAILABLE else 0.0,
                 evidence_ids=[ev_id], **kw)


class TestIdempotency:
    """'Re-running the same snapshot is idempotent.'"""

    def test_identical_snapshot_produces_no_changes(self):
        env = envelope([claim("legal_name", "NORDLYS AS"), claim("financials.revenue", 100.0)])
        _, changes, _ = reconcile(env, env)
        assert changes == []

    def test_reordered_list_is_not_a_change(self):
        """Registry endpoints do not promise ordering."""
        before = envelope([claim("leadership", [
            {"role": "managing_director", "role_code": "DAGL", "name": "Kari Nordmann"},
            {"role": "board_member", "role_code": "MEDL", "name": "Ola Hansen"},
        ])])
        after = envelope([claim("leadership", [
            {"role": "board_member", "role_code": "MEDL", "name": "Ola Hansen"},
            {"role": "managing_director", "role_code": "DAGL", "name": "Kari Nordmann"},
        ])])
        _, changes, _ = reconcile(before, after)
        assert changes == []

    def test_int_float_equivalence_is_not_a_change(self):
        before = envelope([claim("financials.revenue", 0)])
        after = envelope([claim("financials.revenue", 0.0)])
        _, changes, _ = reconcile(before, after)
        assert changes == []

    def test_bookkeeping_qualifiers_do_not_trigger_changes(self):
        before = envelope([claim("official_website", "https://x.no", qualifiers={"identity_proof": "a"})])
        after = envelope([claim("official_website", "https://x.no", qualifiers={"identity_proof": "b"})])
        _, changes, _ = reconcile(before, after)
        assert changes == []

    def test_whole_snapshot_diffed_against_itself_is_silent(self):
        snap = {ORG: envelope([claim("legal_name", "NORDLYS AS")])}
        changes, missing = diff_snapshots(snap, snap)
        assert changes == [] and missing == []


class TestFailurePreservesValue:
    """'A failed refresh must not erase the last supported value.'"""

    def test_failed_source_keeps_previous_value(self):
        before = envelope([claim("official_website", "https://nordlys.no")])
        after = envelope([claim("official_website", None, Availability.FAILED)])
        claims, changes, _ = reconcile(before, after)
        website = next(c for c in claims if c.field == "official_website")
        assert website.value == "https://nordlys.no"
        assert website.availability is Availability.AVAILABLE
        assert website.qualifiers["stale"] is True
        assert website.qualifiers["refresh_status"] == "failed"

    def test_outage_is_reported_as_degradation_not_deletion(self):
        before = envelope([claim("official_website", "https://nordlys.no")])
        after = envelope([claim("official_website", None, Availability.FAILED)])
        _, changes, _ = reconcile(before, after)
        assert [c.change_type for c in changes] == ["source_unavailable"]
        assert changes[0].materiality == "minor"
        assert changes[0].new_value == "https://nordlys.no"

    def test_blocked_source_also_preserves(self):
        before = envelope([claim("official_website", "https://nordlys.no")])
        after = envelope([claim("official_website", None, Availability.BLOCKED)])
        claims, _, _ = reconcile(before, after)
        assert next(c for c in claims if c.field == "official_website").value == "https://nordlys.no"

    def test_prior_evidence_is_carried_forward(self):
        before = envelope([claim("official_website", "https://nordlys.no", ev_id="ev-website")],
                          ev=[evidence("ev-website", url="https://nordlys.no")])
        after = envelope([claim("official_website", None, Availability.FAILED)], ev=[])
        _, _, carried = reconcile(before, after)
        assert [e.id for e in carried] == ["ev-website"]

    def test_a_read_source_reporting_nothing_is_a_real_change(self):
        """not_available means we looked and it is gone — that is not an outage."""
        before = envelope([claim("official_website", "https://nordlys.no")])
        after = envelope([claim("official_website", None, Availability.NOT_AVAILABLE)])
        _, changes, _ = reconcile(before, after)
        assert [c.change_type for c in changes] == ["became_unavailable"]


class TestTypedChanges:
    def test_new_and_departed_roles_are_separate_events(self):
        before = envelope([claim("leadership", [
            {"role_code": "DAGL", "name": "Kari Nordmann"},
        ])])
        after = envelope([claim("leadership", [
            {"role_code": "DAGL", "name": "Ola Hansen"},
        ])])
        _, changes, _ = reconcile(before, after)
        assert sorted(c.change_type for c in changes) == ["departed_role", "new_role"]

    def test_new_location(self):
        before = envelope([claim("locations", [{"organisation_number": "1"}])])
        after = envelope([claim("locations", [
            {"organisation_number": "1"}, {"organisation_number": "2"},
        ])])
        _, changes, _ = reconcile(before, after)
        assert [c.change_type for c in changes] == ["new_location"]
        assert changes[0].new_value == {"organisation_number": "2"}

    def test_financial_movement_is_a_new_filing(self):
        before = envelope([claim("financials.revenue", 100.0, reporting_period="2024-01-01/2024-12-31")])
        after = envelope([claim("financials.revenue", 250.0, reporting_period="2025-01-01/2025-12-31")])
        _, changes, _ = reconcile(before, after)
        assert changes[0].change_type == "new_filing"
        assert changes[0].reporting_period == "2025-01-01/2025-12-31"

    def test_currency_switch_is_detected_even_at_the_same_number(self):
        before = envelope([claim("financials.revenue", 100.0, qualifiers={"currency": "NOK"})])
        after = envelope([claim("financials.revenue", 100.0, qualifiers={"currency": "USD"})])
        _, changes, _ = reconcile(before, after)
        assert len(changes) == 1

    def test_qualifier_only_change_explains_itself(self):
        """Identical old/new values read as a false positive without a reason."""
        before = envelope([claim("financials.revenue", 100.0, qualifiers={"currency": "NOK"})])
        after = envelope([claim("financials.revenue", 100.0, qualifiers={"currency": "USD"})])
        _, changes, _ = reconcile(before, after)
        assert changes[0].note == "value unchanged; currency NOK -> USD"

    def test_genuine_value_change_carries_no_qualifier_note(self):
        before = envelope([claim("financials.revenue", 100.0)])
        after = envelope([claim("financials.revenue", 250.0)])
        _, changes, _ = reconcile(before, after)
        assert changes[0].note is None

    def test_legal_name_change_is_material(self):
        before = envelope([claim("legal_name", "GAMMEL AS")])
        after = envelope([claim("legal_name", "NY AS")])
        _, changes, _ = reconcile(before, after)
        assert changes[0].change_type == "changed_identity"
        assert changes[0].materiality == "material"

    def test_description_tweak_is_minor(self):
        before = envelope([claim("website_description", "Gammel tekst")])
        after = envelope([claim("website_description", "Ny tekst")])
        _, changes, _ = reconcile(before, after)
        assert changes[0].change_type == "changed_description"
        assert changes[0].materiality == "minor"

    def test_appearing_value_is_became_available(self):
        before = envelope([claim("official_website", None, Availability.NOT_AVAILABLE)])
        after = envelope([claim("official_website", "https://nordlys.no")])
        _, changes, _ = reconcile(before, after)
        assert changes[0].change_type == "became_available"


class TestEvidenceAndHistory:
    def test_changes_carry_both_sides(self):
        before = envelope([claim("legal_name", "GAMMEL AS")],
                          ev=[evidence(at="2026-09-01T10:00:00Z", digest="b" * 64)])
        after = envelope([claim("legal_name", "NY AS")],
                         ev=[evidence(at="2026-09-15T10:00:00Z", digest="c" * 64)])
        _, changes, _ = reconcile(before, after)
        change = changes[0]
        assert change.old_retrieved_at == "2026-09-01T10:00:00Z"
        assert change.new_retrieved_at == "2026-09-15T10:00:00Z"
        assert change.old_content_sha256 != change.new_content_sha256
        assert evidence_complete(changes)

    def test_first_observed_survives_an_unchanged_value(self):
        before = envelope([claim("legal_name", "NORDLYS AS")],
                          ev=[evidence(at="2026-09-01T10:00:00Z")])
        first, _, _ = reconcile(None, before)
        later = envelope([claim("legal_name", "NORDLYS AS")],
                         ev=[evidence(at="2026-09-15T10:00:00Z")])
        claims, _, _ = reconcile(envelope(first, ev=before.evidence), later)
        name = next(c for c in claims if c.field == "legal_name")
        assert name.qualifiers["first_observed_at"] == "2026-09-01T10:00:00Z"
        assert name.qualifiers["last_observed_at"] == "2026-09-15T10:00:00Z"

    def test_timestamps_come_from_evidence_not_the_clock(self):
        env = envelope([claim("legal_name", "NORDLYS AS")])
        claims, _, _ = reconcile(None, env)
        assert claims[0].qualifiers["last_observed_at"] == "2026-09-15T10:00:00Z"


class TestSnapshotIntegrity:
    def test_a_dropped_company_is_reported(self):
        before = {ORG: envelope([claim("legal_name", "NORDLYS AS")])}
        _, missing = diff_snapshots(before, {})
        assert missing == [ORG]

    def test_a_vanished_field_is_carried_not_dropped(self):
        before = envelope([claim("legal_name", "NORDLYS AS"), claim("municipality", "OSLO")])
        after = envelope([claim("legal_name", "NORDLYS AS")])
        claims, _, _ = reconcile(before, after)
        assert {c.field for c in claims} == {"legal_name", "municipality"}


class TestCanonical:
    def test_nested_ordering_is_normalised(self):
        assert canonical([{"b": 1, "a": [3, 1, 2]}]) == canonical([{"a": [1, 2, 3], "b": 1}])

    def test_booleans_are_not_coerced_to_ints(self):
        assert canonical(True) is True
        assert canonical(1) == 1


class TestAuditCascade:
    """A root decision must settle everything that inherited from it."""

    @staticmethod
    def _obs(oid, platform, signal, proof, org="987654321"):
        from fotavtrykk.models import Observation
        return Observation(id=oid, organisation_number=org, platform=platform,
                           signal_type=signal, source_url="https://x/", retrieved_at="t",
                           content_sha256="a"*64, exact_entity=True, identity_proof=proof,
                           acquisition_mode="official_api", rights_status="approved")

    def test_site_handles_resolve_to_their_site(self):
        from fotavtrykk.audit import dependents, root_of
        site = self._obs("s", "company_site", "company_profile", "org_number_on_page")
        handle = self._obs("h", "linkedin", "profile_handle",
                           "declared_on_verified_company_site:org_number_on_page")
        pool = [site, handle]
        assert root_of(handle, pool) is site
        assert dependents(site, pool) == [handle]

    def test_wikidata_sitelinks_and_statements_resolve_to_the_entity(self):
        from fotavtrykk.audit import dependents, root_of
        entity = self._obs("w", "wikidata", "company_profile",
                           "wikidata_p2333_organisation_number")
        page = self._obs("p", "wikipedia", "company_profile", "wikidata_p2333_sitelink:Q1")
        handle = self._obs("x", "x", "profile_handle", "wikidata_p2333_statement:P2002")
        pool = [entity, page, handle]
        assert root_of(page, pool) is entity
        assert root_of(handle, pool) is entity
        assert len(dependents(entity, pool)) == 2

    def test_a_proven_observation_has_no_root(self):
        from fotavtrykk.audit import root_of
        site = self._obs("s", "company_site", "company_profile", "org_number_on_page")
        assert root_of(site, [site]) is None

    def test_roots_of_other_companies_are_not_matched(self):
        from fotavtrykk.audit import root_of
        mine = self._obs("h", "linkedin", "profile_handle",
                         "declared_on_verified_company_site:org_number_on_page")
        theirs = self._obs("s2", "company_site", "company_profile",
                           "org_number_on_page", org="111111111")
        assert root_of(mine, [theirs]) is None

    def test_sampler_includes_the_root_of_any_dependent(self):
        from fotavtrykk.audit import sample_queue
        site = self._obs("s", "company_site", "company_profile", "org_number_on_page")
        handles = [self._obs(f"h{i}", "linkedin", "profile_handle",
                             "declared_on_verified_company_site:org_number_on_page")
                   for i in range(5)]
        queue = sample_queue([site, *handles], 3, seed="t", risky_fraction=1.0)
        assert any(o.id == "s" for o in queue)
