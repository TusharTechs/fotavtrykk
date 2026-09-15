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

    def test_name_only_when_nothing_corroborates(self):
        resolver = SiteResolver(fetcher=None)
        proof, _ = resolver._name_fallback(
            "987654321", IDENTITY["legal_name"],
            "Nordlys Verksted AS", page("Velkommen"), IDENTITY,
        )
        assert proof == orgnr.PROOF_REGISTRY_SITE

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
