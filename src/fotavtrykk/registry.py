"""Official Norwegian records: Enhetsregisteret, roles, Regnskapsregisteret, subunits.

This is the 15-point foundation. It is free, exact and near-100% available, so
it should never be the reason an envelope is incomplete. Open data under NLOD 2.0.

Two traps are handled explicitly here:
  * accounts are filed per statement type -- SELSKAP (the company) and KONSERN
    (the group). Collapsing them is the parent/subsidiary error the contract
    disqualifies for.
  * accounts are not always in NOK. Equinor files in USD. Currency travels with
    every financial claim.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .http import Fetcher, FetchResult
from .models import Availability, Claim, Evidence, SourceClass

ENTITY_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/{org}"
ROLES_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/{org}/roller"
ACCOUNTS_URL = "https://data.brreg.no/regnskapsregisteret/regnskap/{org}"
SUBUNITS_URL = (
    "https://data.brreg.no/enhetsregisteret/api/underenheter"
    "?overordnetEnhet={org}&size=100"
)

# Role codes we publish. Deliberately excludes auditors and accountants, which
# are service providers rather than company leadership.
# Every envelope emits this exact key set, present or not. A field that appears
# only when it has a value would read as an added/removed field on refresh and
# would make per-field coverage depend on availability.
FINANCIAL_FIELDS = (
    "financials.revenue",
    "financials.operating_profit",
    "financials.profit_before_tax",
    "financials.net_result",
    "financials.total_assets",
    "financials.equity",
)

ROLE_LABELS = {
    "DAGL": "managing_director",
    "LEDE": "board_chair",
    "NEST": "board_deputy_chair",
    "MEDL": "board_member",
    "INNH": "proprietor",
}


class RegistryCollector:
    """Collects official records for one company and turns them into claims."""

    def __init__(self, fetcher: Fetcher) -> None:
        self.fetcher = fetcher

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _evidence(ev_id: str, result: FetchResult, *, span: str | None = None,
                  method: str = "json_field") -> Evidence:
        return Evidence(
            id=ev_id,
            source_url=result.url,
            source_class=SourceClass.OFFICIAL_REGISTRY,
            retrieved_at=result.retrieved_at,
            content_sha256=result.content_sha256,
            final_url=result.final_url,
            http_status=result.status,
            claim_span=span,
            extraction_method=method,
        )

    @staticmethod
    def _unavailable(field: str, result: FetchResult, note: str) -> Claim:
        """Absence is a state, never a zero."""
        if result.blocked:
            state = Availability.BLOCKED
        elif result.status == 404:
            state = Availability.NOT_AVAILABLE
        elif result.ok:
            state = Availability.NOT_AVAILABLE
        else:
            state = Availability.FAILED
        return Claim(field=field, value=None, availability=state, confidence=0.0, note=note)

    @staticmethod
    def _parse(result: FetchResult) -> Any | None:
        if not result.json_ok:
            return None
        try:
            return json.loads(result.text)
        except json.JSONDecodeError:
            return None

    # -- entity ----------------------------------------------------------

    async def entity(self, org: str) -> tuple[FetchResult, dict[str, Any] | None]:
        result = await self.fetcher.get(ENTITY_URL.format(org=org), accept="application/json")
        data = self._parse(result)
        return result, data if isinstance(data, dict) else None

    def entity_claims(self, org: str, result: FetchResult,
                      data: dict[str, Any] | None) -> tuple[list[Claim], list[Evidence]]:
        if not data or str(data.get("organisasjonsnummer")) != str(org):
            note = "registry entity not returned for this organisation number"
            return [self._unavailable("legal_name", result, note)], []

        ev = self._evidence("ev-registry", result, span=f"{data.get('navn')} ({org})")
        ok = lambda field, value, **kw: Claim(  # noqa: E731
            field=field, value=value,
            availability=Availability.AVAILABLE if value not in (None, "", []) else Availability.NOT_AVAILABLE,
            confidence=1.0 if value not in (None, "", []) else 0.0,
            evidence_ids=[ev.id], **kw,
        )

        business = data.get("forretningsadresse") or {}
        postal = data.get("postadresse") or {}
        nace = data.get("naeringskode1") or {}
        form = data.get("organisasjonsform") or {}

        claims = [
            ok("legal_name", data.get("navn")),
            ok("legal_form", f"{form.get('kode')} - {form.get('beskrivelse')}" if form.get("kode") else None),
            ok("registration_date", data.get("registreringsdatoEnhetsregisteret")),
            ok("industry_code", f"{nace.get('kode')} {nace.get('beskrivelse')}" if nace.get("kode") else None),
            ok("business_address", self._address(business)),
            ok("postal_address", self._address(postal)),
            ok("municipality", business.get("kommune") or postal.get("kommune")),
            ok("latest_submitted_accounts", data.get("sisteInnsendteAarsregnskap")),
            ok("registry_website", self._normalise_site(data.get("hjemmeside"))),
            ok("registry_phone", (data.get("telefon") or "").strip() or None),
        ]

        # Employee count is genuinely absent for ~79% of this universe. Say so.
        if data.get("harRegistrertAntallAnsatte"):
            claims.append(Claim(
                field="employees_registered", value=data.get("antallAnsatte"),
                availability=Availability.AVAILABLE, confidence=1.0,
                evidence_ids=[ev.id],
                reporting_period=data.get("registreringsdatoAntallAnsatteEnhetsregisteret"),
            ))
        else:
            claims.append(Claim(
                field="employees_registered", value=None,
                availability=Availability.NOT_AVAILABLE, confidence=0.0,
                evidence_ids=[ev.id],
                note="no employee count registered in Enhetsregisteret; not zero employees",
            ))

        claims.append(Claim(
            field="operating_status",
            value={
                "bankruptcy": bool(data.get("konkurs")),
                "under_liquidation": bool(data.get("underAvvikling")),
                "compulsory_liquidation": bool(data.get("underTvangsavviklingEllerTvangsopplosning")),
                "in_group": bool(data.get("erIKonsern")),
            },
            availability=Availability.AVAILABLE, confidence=1.0, evidence_ids=[ev.id],
        ))
        return claims, [ev]

    @staticmethod
    def identity_bundle(data: dict[str, Any]) -> dict[str, Any]:
        """Registry facts the website gate can corroborate against.

        These come from a different source than the page, so matching them is
        evidence independent of the legal-name token match.
        """
        address = data.get("forretningsadresse") or data.get("postadresse") or {}
        return {
            "legal_name": data.get("navn") or "",
            "postcode": (address.get("postnummer") or "").strip() or None,
            "city": (address.get("poststed") or "").strip() or None,
            "phone": re.sub(r"\D", "", data.get("telefon") or "") or None,
        }

    @staticmethod
    def _address(block: dict[str, Any]) -> str | None:
        if not block:
            return None
        lines = [line for line in (block.get("adresse") or []) if line]
        tail = " ".join(p for p in (block.get("postnummer"), block.get("poststed")) if p)
        joined = ", ".join(part for part in (", ".join(lines), tail, block.get("land")) if part)
        return joined or None

    @staticmethod
    def _normalise_site(value: str | None) -> str | None:
        raw = (value or "").strip()
        if not raw or raw.lower() in {"-", "n/a", "ingen"}:
            return None
        return raw if raw.startswith(("http://", "https://")) else f"https://{raw}"

    # -- roles -----------------------------------------------------------

    async def roles(self, org: str) -> tuple[list[Claim], list[Evidence]]:
        result = await self.fetcher.get(ROLES_URL.format(org=org), accept="application/json")
        data = self._parse(result)
        if not isinstance(data, dict):
            return [self._unavailable("leadership", result, "roles endpoint returned no usable payload")], []

        ev = self._evidence("ev-roles", result, method="json_roles")
        people: list[dict[str, str]] = []
        for group in data.get("rollegrupper") or []:
            for role in group.get("roller") or []:
                if role.get("avregistrert"):
                    continue  # A resigned officer is not current leadership.
                code = (role.get("type") or {}).get("kode")
                label = ROLE_LABELS.get(code)
                if not label:
                    continue
                holder = self._role_holder(role)
                if holder:
                    people.append({"role": label, "role_code": code, "name": holder})

        if not people:
            return [Claim(
                field="leadership", value=None, availability=Availability.NOT_AVAILABLE,
                confidence=0.0, evidence_ids=[ev.id],
                note="no current registered leadership roles of a published type",
            )], [ev]

        claims = [Claim(
            field="leadership", value=people, availability=Availability.AVAILABLE,
            confidence=1.0, evidence_ids=[ev.id],
        )]
        chief = next((p["name"] for p in people if p["role_code"] == "DAGL"), None)
        chair = next((p["name"] for p in people if p["role_code"] == "LEDE"), None)
        for field, value in (("managing_director", chief), ("board_chair", chair)):
            claims.append(Claim(
                field=field, value=value,
                availability=Availability.AVAILABLE if value else Availability.NOT_AVAILABLE,
                confidence=1.0 if value else 0.0, evidence_ids=[ev.id],
            ))
        return claims, [ev]

    @staticmethod
    def _role_holder(role: dict[str, Any]) -> str | None:
        # Only the name is published. Birth dates are personal data we do not need.
        person = role.get("person") or {}
        name = person.get("navn") or {}
        if name:
            parts = [name.get("fornavn"), name.get("mellomnavn"), name.get("etternavn")]
            joined = " ".join(p for p in parts if p)
            if joined:
                return joined
        unit = role.get("enhet") or {}
        return unit.get("navn") or None

    # -- accounts --------------------------------------------------------

    async def accounts(self, org: str) -> tuple[list[Claim], list[Evidence]]:
        result = await self.fetcher.get(ACCOUNTS_URL.format(org=org), accept="application/json")
        data = self._parse(result)
        if not isinstance(data, list) or not data:
            note = "no annual accounts filed or returned for this entity; not zero"
            missing = [self._unavailable(field, result, note) for field in FINANCIAL_FIELDS]
            missing.append(self._unavailable("financial_history.years", result, note))
            return missing, []

        ev = self._evidence("ev-accounts", result, method="json_accounts")
        company_filings = [f for f in data if f.get("regnskapstype") == "SELSKAP"] or data
        latest = max(company_filings, key=lambda f: (f.get("regnskapsperiode") or {}).get("tilDato") or "")

        period = latest.get("regnskapsperiode") or {}
        period_label = f"{period.get('fraDato')}/{period.get('tilDato')}"
        qualifiers = {
            "currency": latest.get("valuta"),
            "statement_type": latest.get("regnskapstype"),
            "accounting_rules": (latest.get("regnkapsprinsipper") or {}).get("regnskapsregler"),
            "audited": not (latest.get("revisjon") or {}).get("ikkeRevidertAarsregnskap", False),
        }

        result_block = latest.get("resultatregnskapResultat") or {}
        operating = result_block.get("driftsresultat") or {}
        balance = latest.get("egenkapitalGjeld") or {}
        figures = {
            "financials.revenue": (operating.get("driftsinntekter") or {}).get("sumDriftsinntekter"),
            "financials.operating_profit": operating.get("driftsresultat"),
            "financials.profit_before_tax": result_block.get("ordinaertResultatFoerSkattekostnad"),
            "financials.net_result": result_block.get("aarsresultat"),
            "financials.total_assets": (latest.get("eiendeler") or {}).get("sumEiendeler"),
            "financials.equity": (balance.get("egenkapital") or {}).get("sumEgenkapital"),
        }

        claims: list[Claim] = []
        for field in FINANCIAL_FIELDS:
            value = figures.get(field)
            present = isinstance(value, (int, float)) and not isinstance(value, bool)
            # A filed 0 is a real reported figure, not a missing value. Marking it
            # explicitly is how an auditor tells the two apart.
            line_qualifiers = dict(qualifiers)
            if present and value == 0:
                line_qualifiers["filed_zero"] = True
            claims.append(Claim(
                field=field,
                value=value if present else None,
                availability=Availability.AVAILABLE if present else Availability.NOT_AVAILABLE,
                confidence=1.0 if present else 0.0,
                reporting_period=period_label if present else None,
                evidence_ids=[ev.id],
                qualifiers=line_qualifiers if present else {},
                note=None if present else "line item not present in the filed statement; not zero",
            ))

        years = sorted({
            (f.get("regnskapsperiode") or {}).get("tilDato", "")[:4]
            for f in data if (f.get("regnskapsperiode") or {}).get("tilDato")
        }, reverse=True)
        claims.append(Claim(
            field="financial_history.years", value=years,
            availability=Availability.AVAILABLE if years else Availability.NOT_AVAILABLE,
            confidence=1.0 if years else 0.0, evidence_ids=[ev.id],
        ))
        return claims, [ev]

    # -- subunits --------------------------------------------------------

    async def subunits(self, org: str) -> tuple[list[Claim], list[Evidence]]:
        result = await self.fetcher.get(SUBUNITS_URL.format(org=org), accept="application/json")
        data = self._parse(result)
        if not isinstance(data, dict):
            return [self._unavailable("locations", result, "subunit endpoint returned no usable payload")], []

        ev = self._evidence("ev-subunits", result, method="json_subunits")
        units = ((data.get("_embedded") or {}).get("underenheter")) or []
        locations = [{
            "organisation_number": u.get("organisasjonsnummer"),
            "name": u.get("navn"),
            "address": self._address(u.get("beliggenhetsadresse") or u.get("forretningsadresse") or {}),
            "municipality": (u.get("beliggenhetsadresse") or {}).get("kommune"),
        } for u in units]

        total = (data.get("page") or {}).get("totalElements", len(locations))
        return [Claim(
            field="locations", value=locations,
            availability=Availability.AVAILABLE if locations else Availability.NOT_AVAILABLE,
            confidence=1.0 if locations else 0.0, evidence_ids=[ev.id],
            qualifiers={"registered_subunits": total} if locations else {},
            note=None if locations else "no registered subunits; the company has no separate registered workplaces",
        )], [ev]
