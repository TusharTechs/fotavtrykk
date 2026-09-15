"""Connector identity gates. Both publish only on an exact-entity match."""

import json

from fotavtrykk.connectors.nav_jobs import NavJobsSource
from fotavtrykk.connectors.places import PlacesSource
from fotavtrykk.http import CostLedger, FetchResult
from fotavtrykk.models import Availability

HASH = "c" * 64
IDENTITY = {"legal_name": "NORDLYS VERKSTED AS", "postcode": "9008",
            "city": "TROMSØ", "phone": "77601234"}


class StubFetcher:
    """Serves canned responses by URL substring."""

    def __init__(self, routes: dict[str, object], ok: bool = True):
        self.routes = routes
        self.ok = ok
        self.calls: list[str] = []

    def _result(self, url: str) -> FetchResult:
        self.calls.append(url)
        # Insertion order decides: "/api/v1/feedentry/u1" also contains
        # "/api/v1/feed", so the more specific route must be listed first.
        for key, payload in self.routes.items():
            if key in url:
                body = payload if isinstance(payload, str) else json.dumps(payload)
                return FetchResult(url=url, ok=True, status=200, final_url=url, text=body,
                                   content_sha256=HASH, retrieved_at="2026-09-16T08:00:00Z")
        return FetchResult(url=url, ok=False, status=404, retrieved_at="2026-09-16T08:00:00Z",
                           error="HTTP 404")

    async def get(self, url, accept=None, headers=None, timeout=None):
        return self._result(url)

    async def post_json(self, url, payload=None, headers=None, timeout=None):
        return self._result(url)


def feed_page(business_name, uuid="u1"):
    return {"items": [{"url": f"/api/v1/feedentry/{uuid}",
                       "_feed_entry": {"uuid": uuid, "status": "ACTIVE",
                                       "businessName": business_name}}],
            "next_url": None}


def ad(orgnr, name="NORDLYS VERKSTED AS"):
    return {"ad_content": {"uuid": "u1", "title": "Mekaniker søkes",
                           "employer": {"name": name, "orgnr": orgnr},
                           "published": "2026-09-01T00:00:00+02:00",
                           "expires": "2026-10-01T00:00:00+02:00",
                           "workLocations": [{"municipal": "TROMSØ"}]}}


class TestNavJobs:
    async def test_advert_published_on_exact_org_number(self):
        f = StubFetcher({"publicToken": "token eyJa.eyJb.eyJc",
                         "feedentry": ad("987654321"),
                         "/api/v1/feed": feed_page("NORDLYS VERKSTED AS")})
        src = NavJobsSource(f)
        await src.prime([("987654321", "NORDLYS VERKSTED AS")])
        claims, evidence, observations = src.collect("987654321")
        assert next(c for c in claims if c.field == "jobs.active_count").value == 1
        assert observations[0].identity_proof == "employer_org_number_in_official_feed"
        assert observations[0].signal_type == "job_posting"

    async def test_namesake_advert_is_rejected(self):
        """Same business name, different company. This is the whole point."""
        f = StubFetcher({"publicToken": "token eyJa.eyJb.eyJc",
                         "feedentry": ad("111111111"),
                         "/api/v1/feed": feed_page("NORDLYS VERKSTED AS")})
        src = NavJobsSource(f)
        await src.prime([("987654321", "NORDLYS VERKSTED AS")])
        claims, _, observations = src.collect("987654321")
        assert observations == []
        assert next(c for c in claims if c.field == "jobs.active_count").value == 0

    async def test_checked_zero_is_available_and_explained(self):
        f = StubFetcher({"publicToken": "token eyJa.eyJb.eyJc",
                         "/api/v1/feed": {"items": [], "next_url": None}})
        src = NavJobsSource(f)
        await src.prime([("987654321", "NORDLYS VERKSTED AS")])
        count = next(c for c in src.collect("987654321")[0] if c.field == "jobs.active_count")
        assert count.availability is Availability.AVAILABLE
        assert count.value == 0
        assert "no active advert" in count.note

    async def test_unprimed_source_fails_rather_than_reporting_zero(self):
        src = NavJobsSource(StubFetcher({}))
        claims, _, _ = src.collect("987654321")
        assert all(c.availability is Availability.FAILED for c in claims)
        assert all(c.value is None for c in claims)


def place(**over):
    base = {"id": "p1", "displayName": {"text": "Nordlys Verksted"},
            "formattedAddress": "Storgata 1, 9008 Tromsø, Norge",
            "nationalPhoneNumber": "776 01 234", "websiteUri": "https://nordlys.no",
            "rating": 4.6, "userRatingCount": 88, "googleMapsUri": "https://maps.google.com/?cid=1"}
    base.update(over)
    return base


class TestPlaces:
    def test_disabled_without_a_key_at_zero_cost(self):
        src = PlacesSource(StubFetcher({}), api_key=None)
        assert not src.enabled
        assert src.ledger.spent_usd == 0.0

    async def test_no_key_returns_not_available_not_a_blank(self):
        src = PlacesSource(StubFetcher({}), api_key=None)
        claims, _, observations = await src.collect("987654321", "NORDLYS VERKSTED AS", IDENTITY)
        assert observations == []
        assert all(c.availability is Availability.NOT_AVAILABLE and c.value is None for c in claims)
        assert "GOOGLE_PLACES_API_KEY" in claims[0].note

    async def test_domain_match_publishes_a_place_summary(self):
        f = StubFetcher({"searchText": {"places": [place()]}})
        src = PlacesSource(f, api_key="k")
        claims, _, observations = await src.collect(
            "987654321", "NORDLYS VERKSTED AS", IDENTITY, "https://nordlys.no")
        assert observations[0].signal_type == "place_summary"
        assert observations[0].identity_proof == "places_website_matches_verified_domain"
        assert observations[0].metrics["rating"] == 4.6

    async def test_phone_match_publishes_when_no_verified_domain(self):
        f = StubFetcher({"searchText": {"places": [place(websiteUri=None)]}})
        src = PlacesSource(f, api_key="k")
        _, _, observations = await src.collect("987654321", "NORDLYS VERKSTED AS", IDENTITY)
        assert observations[0].identity_proof == "places_phone_matches_registry"

    async def test_name_only_match_is_rejected(self):
        """A place with the right name but no registry agreement is ambiguous."""
        f = StubFetcher({"searchText": {"places": [place(
            websiteUri=None, nationalPhoneNumber=None,
            formattedAddress="Annen gate 9, 0150 Oslo, Norge")]}})
        src = PlacesSource(f, api_key="k")
        claims, _, observations = await src.collect("987654321", "NORDLYS VERKSTED AS", IDENTITY)
        assert observations == []
        assert claims[0].availability is Availability.AMBIGUOUS

    async def test_cost_cap_stops_the_connector(self):
        ledger = CostLedger(limit_usd=0.01)
        src = PlacesSource(StubFetcher({"searchText": {"places": [place()]}}),
                           api_key="k", ledger=ledger)
        claims, _, _ = await src.collect("987654321", "NORDLYS VERKSTED AS", IDENTITY)
        assert "cost cap" in claims[0].note
        assert src.searches == 0

    async def test_every_search_is_charged(self):
        ledger = CostLedger(limit_usd=10.0)
        src = PlacesSource(StubFetcher({"searchText": {"places": [place()]}}),
                           api_key="k", ledger=ledger)
        await src.collect("987654321", "NORDLYS VERKSTED AS", IDENTITY, "https://nordlys.no")
        assert ledger.spent_usd > 0
        assert ledger.by_provider["google_places"] > 0


LD_JSON = '''<html><body>
<script type="application/ld+json">
{"@type":"NewsArticle","headline":"Ny kontrakt i Nordland",
 "datePublished":"2026-09-10T08:00:00Z","url":"/nyheter/kontrakt"}
</script>
<script type="application/ld+json">
{"@type":"NewsArticle","headline":"Kvartalstall lagt fram",
 "datePublished":"2026-08-01T08:00:00Z","url":"/nyheter/q2"}
</script></body></html>'''

DOM_NEWS = '''<html><body>
<article><h2>Vi åpner nytt kontor</h2><time datetime="2026-07-04">4. juli 2026</time>
<a href="/nyheter/kontor">Les mer</a></article>
<article><h2>Uten dato her</h2><p>Ingen dato oppgitt.</p></article>
<article><h2>Norsk datoformat</h2><p>Publisert 12. mars 2026 av redaksjonen.</p></article>
</body></html>'''

HOME = '<html><body><a href="/nyheter">Nyheter</a><a href="/om-oss">Om oss</a></body></html>'


class TestCompanyNews:
    async def test_requires_a_verified_site(self):
        from fotavtrykk.connectors import CompanyNewsSource
        src = CompanyNewsSource(StubFetcher({}))
        claims, _, observations = await src.collect("987654321", None, None, None)
        assert observations == []
        assert all(c.availability is Availability.NOT_AVAILABLE for c in claims)

    async def test_unproven_site_publishes_nothing(self):
        """Identity is inherited from the site gate; no proof, no posts."""
        from fotavtrykk.connectors import CompanyNewsSource
        src = CompanyNewsSource(StubFetcher({"/nyheter": LD_JSON}))
        _, _, observations = await src.collect(
            "987654321", "https://firma.no/", HOME, identity_proof=None)
        assert observations == []

    async def test_structured_data_posts_are_published_newest_first(self):
        from fotavtrykk.connectors import CompanyNewsSource
        src = CompanyNewsSource(StubFetcher({"/nyheter": LD_JSON}))
        claims, _, observations = await src.collect(
            "987654321", "https://firma.no/", HOME, "org_number_labelled_on_page")
        assert len(observations) == 2
        assert observations[0].observed_at == "2026-09-10"
        latest = next(c for c in claims if c.field == "activity.latest_post_date")
        assert latest.value == "2026-09-10"

    async def test_identity_proof_records_the_inherited_chain(self):
        from fotavtrykk.connectors import CompanyNewsSource
        src = CompanyNewsSource(StubFetcher({"/nyheter": LD_JSON}))
        _, _, observations = await src.collect(
            "987654321", "https://firma.no/", HOME, "org_number_labelled_on_page")
        assert observations[0].identity_proof == (
            "published_on_verified_company_site:org_number_labelled_on_page")
        assert observations[0].signal_type == "public_post"

    async def test_company_owned_posts_never_carry_sentiment(self):
        """The source policy bars company copy as an independent sentiment claim."""
        from fotavtrykk.connectors import CompanyNewsSource
        src = CompanyNewsSource(StubFetcher({"/nyheter": LD_JSON}))
        claims, _, observations = await src.collect(
            "987654321", "https://firma.no/", HOME, "org_number_on_page")
        assert all(o.sentiment_label is None for o in observations)
        posts = next(c for c in claims if c.field == "activity.posts")
        assert posts.qualifiers["sentiment_eligible"] is False

    async def test_undated_items_are_dropped_not_guessed(self):
        from fotavtrykk.connectors import CompanyNewsSource
        src = CompanyNewsSource(StubFetcher({"/nyheter": DOM_NEWS}))
        _, _, observations = await src.collect(
            "987654321", "https://firma.no/", HOME, "org_number_on_page")
        titles = [o.evidence_span for o in observations]
        assert "Uten dato her" not in titles
        assert len(observations) == 2

    async def test_norwegian_date_format_is_parsed(self):
        from fotavtrykk.connectors import CompanyNewsSource
        src = CompanyNewsSource(StubFetcher({"/nyheter": DOM_NEWS}))
        _, _, observations = await src.collect(
            "987654321", "https://firma.no/", HOME, "org_number_on_page")
        assert {o.observed_at for o in observations} == {"2026-07-04", "2026-03-12"}

    async def test_missing_news_page_is_not_available_not_failed(self):
        from fotavtrykk.connectors import CompanyNewsSource
        src = CompanyNewsSource(StubFetcher({}))
        claims, _, observations = await src.collect(
            "987654321", "https://firma.no/", HOME, "org_number_on_page")
        assert observations == []
        assert all(c.availability is Availability.NOT_AVAILABLE for c in claims)
        assert "no dated news" in claims[0].note
