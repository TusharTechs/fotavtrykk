"""Registry extraction, driven by stubbed responses (no network)."""

import json

from fotavtrykk.http import FetchResult
from fotavtrykk.models import Availability
from fotavtrykk.registry import FINANCIAL_FIELDS, RegistryCollector

HASH = "b" * 64


class StubFetcher:
    """Returns one canned payload. RegistryCollector only ever calls .get()."""

    def __init__(self, payload, *, ok=True, status=200):
        self.payload = payload
        self.ok = ok
        self.status = status

    async def get(self, url, accept=None):
        body = json.dumps(self.payload) if self.payload is not None else ""
        return FetchResult(
            url=url, ok=self.ok, status=self.status, final_url=url,
            text=body, content_sha256=HASH if self.ok else "",
            retrieved_at="2026-09-15T10:00:00Z", requests_used=1,
            error=None if self.ok else f"HTTP {self.status}",
        )


def accounts_payload(revenue, *, currency="NOK", statement="SELSKAP"):
    return [{
        "regnskapstype": statement,
        "valuta": currency,
        "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
        "regnkapsprinsipper": {"regnskapsregler": "regnskapslovenAlminneligeRegler"},
        "revisjon": {"ikkeRevidertAarsregnskap": False},
        "resultatregnskapResultat": {
            "aarsresultat": -18167.0,
            "driftsresultat": {
                "driftsresultat": -18167.0,
                "driftsinntekter": {"sumDriftsinntekter": revenue},
            },
        },
        "eiendeler": {"sumEiendeler": 50000.0},
        "egenkapitalGjeld": {"egenkapital": {"sumEgenkapital": 30000.0}},
    }]


class TestFinancialKeyStability:
    """A field that appears only when populated breaks refresh diffs."""

    async def test_same_keys_whether_accounts_exist_or_not(self):
        present, _ = await RegistryCollector(StubFetcher(accounts_payload(1000.0))).accounts("987654321")
        missing, _ = await RegistryCollector(StubFetcher(None, ok=False, status=404)).accounts("987654321")
        assert {c.field for c in present} == {c.field for c in missing}
        assert set(FINANCIAL_FIELDS).issubset({c.field for c in present})

    async def test_missing_accounts_are_not_available_never_zero(self):
        claims, _ = await RegistryCollector(StubFetcher(None, ok=False, status=404)).accounts("987654321")
        for claim in claims:
            assert claim.availability is Availability.NOT_AVAILABLE
            assert claim.value is None
            assert "not zero" in (claim.note or "")


class TestFinancialFidelity:
    async def test_filed_zero_is_published_and_marked_as_filed(self):
        claims, _ = await RegistryCollector(StubFetcher(accounts_payload(0.0))).accounts("987654321")
        revenue = next(c for c in claims if c.field == "financials.revenue")
        assert revenue.availability is Availability.AVAILABLE
        assert revenue.value == 0
        assert revenue.qualifiers["filed_zero"] is True

    async def test_non_zero_revenue_is_not_marked_filed_zero(self):
        claims, _ = await RegistryCollector(StubFetcher(accounts_payload(1000.0))).accounts("987654321")
        revenue = next(c for c in claims if c.field == "financials.revenue")
        assert "filed_zero" not in revenue.qualifiers

    async def test_currency_and_statement_type_travel_with_every_figure(self):
        claims, _ = await RegistryCollector(
            StubFetcher(accounts_payload(5.0, currency="USD"))
        ).accounts("987654321")
        for claim in claims:
            if claim.field in FINANCIAL_FIELDS and claim.availability is Availability.AVAILABLE:
                assert claim.qualifiers["currency"] == "USD"
                assert claim.qualifiers["statement_type"] == "SELSKAP"
                assert claim.reporting_period == "2025-01-01/2025-12-31"

    async def test_company_accounts_preferred_over_group_accounts(self):
        payload = accounts_payload(10.0, statement="KONSERN") + accounts_payload(20.0)
        claims, _ = await RegistryCollector(StubFetcher(payload)).accounts("987654321")
        revenue = next(c for c in claims if c.field == "financials.revenue")
        assert revenue.value == 20.0
        assert revenue.qualifiers["statement_type"] == "SELSKAP"


class TestRoles:
    ROLES = {"rollegrupper": [{
        "type": {"kode": "DAGL"},
        "roller": [
            {"type": {"kode": "DAGL"}, "avregistrert": False,
             "person": {"navn": {"fornavn": "Kari", "etternavn": "Nordmann"},
                        "fodselsdato": "1980-01-01"}},
            {"type": {"kode": "DAGL"}, "avregistrert": True,
             "person": {"navn": {"fornavn": "Ola", "etternavn": "Gammel"}}},
        ],
    }]}

    async def test_resigned_officers_are_excluded(self):
        claims, _ = await RegistryCollector(StubFetcher(self.ROLES)).roles("987654321")
        names = [p["name"] for p in next(c for c in claims if c.field == "leadership").value]
        assert names == ["Kari Nordmann"]

    async def test_birth_dates_are_never_published(self):
        claims, _ = await RegistryCollector(StubFetcher(self.ROLES)).roles("987654321")
        assert "1980" not in json.dumps([c.model_dump() for c in claims])

    async def test_no_roles_is_not_available_not_empty_success(self):
        claims, _ = await RegistryCollector(StubFetcher({"rollegrupper": []})).roles("987654321")
        leadership = next(c for c in claims if c.field == "leadership")
        assert leadership.availability is Availability.NOT_AVAILABLE
        assert leadership.value is None
