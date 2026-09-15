"""Website identity gate: corroboration and the tiers below hard proof."""

from fotavtrykk import orgnr
from fotavtrykk.site import SiteResolver

IDENTITY = {"legal_name": "NORDLYS VERKSTED AS", "postcode": "9008",
            "city": "TROMSØ", "phone": "77601234"}


def page(body="", excerpt="", title="Nordlys Verksted"):
    return {"title": title, "description": "", "body_text": body,
            "identity_excerpt": excerpt, "structured_names": "", "links": []}


class TestCorroboration:
    def test_switchboard_number_corroborates(self):
        found = SiteResolver._corroboration(page("Ring oss på 77 60 12 34"), IDENTITY)
        assert found and "switchboard" in found

    def test_postcode_with_town_corroborates(self):
        found = SiteResolver._corroboration(page("Storgata 1, 9008 Tromsø"), IDENTITY)
        assert found and "address" in found

    def test_postcode_alone_does_not_corroborate(self):
        """Every company in a town shares its postcode."""
        assert SiteResolver._corroboration(page("Postboks 9008"), IDENTITY) is None

    def test_town_alone_does_not_corroborate(self):
        assert SiteResolver._corroboration(page("Vi holder til i Tromsø"), IDENTITY) is None

    def test_unrelated_page_does_not_corroborate(self):
        assert SiteResolver._corroboration(page("Velkommen til nettsiden"), IDENTITY) is None

    def test_missing_registry_facts_never_corroborate(self):
        assert SiteResolver._corroboration(page("9008 Tromsø"), {}) is None

    def test_footer_evidence_counts(self):
        found = SiteResolver._corroboration(page("", "Storgata 1, 9008 Tromsø"), IDENTITY)
        assert found is not None


class TestFallbackTiers:
    def test_corroborated_outranks_name_only(self):
        resolver = SiteResolver(fetcher=None)
        proof, span = resolver._name_fallback(
            "987654321", IDENTITY["legal_name"],
            "Nordlys Verksted AS", page("Storgata 1, 9008 Tromsø"), IDENTITY,
        )
        assert proof == orgnr.PROOF_CORROBORATED
        assert "9008" in span

    def test_name_only_abstains(self):
        """Was PROOF_REGISTRY_SITE until the audit measured that tier at 84%."""
        resolver = SiteResolver(fetcher=None)
        proof, _ = resolver._name_fallback(
            "987654321", IDENTITY["legal_name"],
            "Nordlys Verksted AS", page("Velkommen"), IDENTITY,
        )
        assert proof == orgnr.PROOF_NONE

    def test_conflicting_org_number_beats_corroboration(self):
        """Negative evidence wins even when the address matches."""
        resolver = SiteResolver(fetcher=None)
        proof, _ = resolver._name_fallback(
            "987654321", IDENTITY["legal_name"], "Nordlys Verksted AS",
            page("Storgata 1, 9008 Tromsø. Org.nr 923 609 016"), IDENTITY,
        )
        assert proof == orgnr.PROOF_NONE

    def test_name_mismatch_abstains_even_if_corroborated(self):
        resolver = SiteResolver(fetcher=None)
        proof, _ = resolver._name_fallback(
            "987654321", "HELT ANNET FIRMA AS", "Nordlys Verksted AS",
            page("Storgata 1, 9008 Tromsø"), IDENTITY,
        )
        assert proof == orgnr.PROOF_NONE

    def test_corroborated_confidence_sits_between_proof_and_name(self):
        assert (orgnr.proof_confidence(orgnr.PROOF_REGISTRY_SITE)
                < orgnr.proof_confidence(orgnr.PROOF_CORROBORATED)
                < orgnr.proof_confidence(orgnr.PROOF_BARE))


class TestContactCandidates:
    """Reviewer briefs fetch these, so duplicates cost requests and add noise."""

    HTML = """<a href="/kontakt/">Kontakt</a><a href="/kontakt/#skjema">Skjema</a>
              <a href="/kontakt">Kontakt oss</a><a href="https://annet.no/kontakt">Ekstern</a>
              <a href="/om-oss">Om oss</a>"""

    def test_fragments_and_trailing_slashes_collapse(self):
        from fotavtrykk.audit import contact_candidates
        found = contact_candidates(self.HTML, "https://firma.no/", limit=10)
        assert sum("kontakt" in u and "om-oss" not in u for u in found) == 1

    def test_other_hosts_are_excluded(self):
        from fotavtrykk.audit import contact_candidates
        assert all("annet.no" not in u for u in contact_candidates(self.HTML, "https://firma.no/", limit=10))

    def test_homepage_itself_is_not_refetched(self):
        from fotavtrykk.audit import contact_candidates
        found = contact_candidates('<a href="/">Hjem</a>', "https://firma.no/", limit=10)
        assert all(u.rstrip("/") != "https://firma.no" for u in found)

    def test_conventional_paths_are_appended(self):
        from fotavtrykk.audit import contact_candidates
        found = contact_candidates("", "https://firma.no/", limit=10)
        assert any(u.endswith("/kontakt") for u in found)


class TestAuditDrivenFixes:
    def test_directional_town_suffix_still_corroborates(self):
        """Registry files 'KRISTIANSAND S'; the page says 'Kristiansand'."""
        identity = {"postcode": "4621", "city": "KRISTIANSAND S", "phone": None}
        found = SiteResolver._corroboration(
            page("Lumberveien 27, NO-4621 Kristiansand, Norway"), identity)
        assert found is not None

    def test_wrong_town_still_fails(self):
        identity = {"postcode": "4621", "city": "KRISTIANSAND S", "phone": None}
        assert SiteResolver._corroboration(page("4621 Bergen"), identity) is None

    def test_default_hosting_placeholder_is_not_a_profile(self):
        resolver = SiteResolver(fetcher=None)
        proof, _ = resolver._name_fallback(
            "987654321", "FLYBOAT ANS", "flyboat.no",
            page("flyboat.no Something amazing will be constructed here... "
                 "To change this page, upload your website into the public_html directory." * 3),
            {},
        )
        assert proof == orgnr.PROOF_NONE

    def test_name_match_alone_no_longer_publishes(self):
        """Audited at 84% entity precision - below the qualification floor."""
        resolver = SiteResolver(fetcher=None)
        proof, _ = resolver._name_fallback(
            "987654321", "NORDLYS VERKSTED AS", "Nordlys Verksted AS",
            page("Nordlys Verksted leverer tjenester i hele landet." * 5), {},
        )
        assert proof == orgnr.PROOF_NONE

    def test_corroborated_still_publishes(self):
        resolver = SiteResolver(fetcher=None)
        proof, _ = resolver._name_fallback(
            "987654321", "NORDLYS VERKSTED AS", "Nordlys Verksted AS",
            page("Storgata 1, 9008 Tromsø" * 5), IDENTITY,
        )
        assert proof == orgnr.PROOF_CORROBORATED
