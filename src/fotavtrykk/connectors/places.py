"""Google Places (New) — ratings and place summaries.

This is the connector that reaches the `ratings_reviews` field family, which
nothing else we have can touch: the registry has no ratings and company sites
cannot supply independent ones.

Two constraints shape it.

**Cost.** `rating` and `userRatingCount` live in the Enterprise field mask, the
most expensive SKU, and the batch budget is $10. Every call is priced against a
ledger and the connector stops before the cap rather than overrunning it.

**Identity.** Places matches on text, so its results are *candidates*. A name
match is the tier already measured at 84% precision and dropped, so a place is
published only when an independent registry fact agrees:

  * the place's website resolves to the same registered domain we verified, or
  * its phone number matches the registry switchboard.

Address matching was tried and removed: see PROOF_ADDRESS below. Otherwise the
place is discarded. No key configured means `not_available` at
zero cost — never a fabricated blank.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.parse import urlparse

from ..http import CostLedger, Fetcher
from ..models import Availability, Claim, Evidence, Observation, SourceClass, validate_observation

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
API_KEY_ENV = "GOOGLE_PLACES_API_KEY"

# Only what we need. The field mask selects the SKU, so asking for less costs less.
FIELD_MASK = ",".join((
    "places.id", "places.displayName", "places.formattedAddress",
    "places.nationalPhoneNumber", "places.internationalPhoneNumber",
    "places.websiteUri", "places.rating", "places.userRatingCount",
    "places.googleMapsUri", "places.primaryTypeDisplayName",
))

# Text Search **Enterprise** — $35 per 1,000 on the global list, so $0.035 each.
# rating/userRatingCount/websiteUri/phone live in that tier. The field mask
# deliberately omits reviews and editorialSummary, which would push the call into
# Enterprise + Atmosphere at $40 per 1,000. Text Search has no monthly free
# allowance (unlike Place Details), so every search is billed.
# Verify against your own billing before a paid run; override with
# --places-cost-per-search.
ESTIMATED_COST_PER_SEARCH_USD = 0.035

PROOF_DOMAIN = "places_website_matches_verified_domain"
PROOF_PHONE = "places_phone_matches_registry"

# `places_address_matches_registry` was removed after spot-checking it.
# It matched on postcode + town, which in Norway can cover a whole village or a
# city district, and it conflates a landlord with its tenants. Real results:
#   HØYRES STORTINGSGRUPPE  -> "Stortingsbygningen", the parliament building
#   HVAMSVINGEN 4 ANS       -> a property partnership named after its address,
#                              matched to whatever business occupies it
#   GELATO ASA              -> a multi-tenant office tower in Barcode
#   SANDVIK INTERIØR ANS    -> registry "Rute 511" vs place "Framgutua 27"
# Roughly 6 of 11 sampled were wrong: ~55% against a 95% floor. Tightening to
# the street would not save it, because a landlord and its tenant share a
# street address by definition. A domain and a phone number belong to an
# entity; an address belongs to a building.


def _host(url: str | None) -> str:
    return (urlparse(url or "").hostname or "").casefold().removeprefix("www.")


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


class PlacesSource:
    """One text search per company, published only on an independent match."""

    platform = "google_places"
    acquisition_mode = "official_api"

    def __init__(
        self, fetcher: Fetcher, *, api_key: str | None = None,
        ledger: CostLedger | None = None,
        cost_per_search_usd: float = ESTIMATED_COST_PER_SEARCH_USD,
    ) -> None:
        self.fetcher = fetcher
        self.api_key = api_key or os.environ.get(API_KEY_ENV) or None
        self.ledger = ledger or CostLedger()
        self.cost_per_search = cost_per_search_usd
        self.searches = 0
        self.published = 0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def collect(
        self, org: str, legal_name: str, identity: dict[str, Any],
        verified_domain: str | None = None,
    ) -> tuple[list[Claim], list[Evidence], list[Observation]]:
        if not self.enabled:
            return self._unresolved(
                Availability.NOT_AVAILABLE,
                f"Google Places not configured; set {API_KEY_ENV} to enable ratings and place data",
            ), [], []

        if not self.ledger.can_afford(self.cost_per_search):
            return self._unresolved(
                Availability.NOT_AVAILABLE,
                "Google Places skipped: third-party cost cap for this batch would be exceeded",
            ), [], []

        query = " ".join(p for p in (
            legal_name, identity.get("postcode"), identity.get("city"),
        ) if p) + " Norge"

        result = await self.fetcher.post_json(
            SEARCH_URL,
            payload={"textQuery": query, "regionCode": "NO", "maxResultCount": 5,
                     "languageCode": "no"},
            headers={"X-Goog-Api-Key": self.api_key, "X-Goog-FieldMask": FIELD_MASK},
        )
        self.searches += 1
        self.ledger.charge("google_places", self.cost_per_search)

        if not result.ok:
            state = Availability.BLOCKED if result.blocked else Availability.FAILED
            return self._unresolved(state, f"Places search failed: {result.error}"), [], []

        try:
            places = json.loads(result.text).get("places", []) or []
        except json.JSONDecodeError:
            return self._unresolved(Availability.FAILED, "Places returned an unusable payload"), [], []

        match, proof, span = self._verify(places, identity, verified_domain)
        if not match:
            return self._unresolved(
                Availability.AMBIGUOUS if places else Availability.NOT_AVAILABLE,
                "no Places result could be tied to this legal entity by domain, phone or address"
                if places else "Places returned no candidate for this company",
            ), [], []

        maps_uri = match.get("googleMapsUri") or f"https://maps.google.com/?cid={match.get('id')}"
        rating = match.get("rating")
        rating_count = match.get("userRatingCount")

        ev = Evidence(
            id="ev-places", source_url=maps_uri, source_class=SourceClass.CUSTOMER_REVIEW,
            retrieved_at=result.retrieved_at, content_sha256=result.content_sha256,
            claim_span=span, extraction_method="places_text_search_v1",
        )
        claims = [
            Claim(field="places.rating", value=rating,
                  availability=Availability.AVAILABLE if rating is not None else Availability.NOT_AVAILABLE,
                  confidence=0.95 if rating is not None else 0.0, evidence_ids=[ev.id],
                  qualifiers={"identity_proof": proof, "scale": "1-5"} if rating is not None else {},
                  note=None if rating is not None else "place found but Google holds no rating for it"),
            Claim(field="places.rating_count", value=rating_count,
                  availability=Availability.AVAILABLE if rating_count is not None else Availability.NOT_AVAILABLE,
                  confidence=0.95 if rating_count is not None else 0.0, evidence_ids=[ev.id]),
            Claim(field="places.address", value=match.get("formattedAddress"),
                  availability=Availability.AVAILABLE if match.get("formattedAddress") else Availability.NOT_AVAILABLE,
                  confidence=0.95, evidence_ids=[ev.id]),
        ]

        observations = [Observation(
            id=f"{org}-google_places-place",
            organisation_number=org, platform=self.platform, signal_type="place_summary",
            source_url=maps_uri, retrieved_at=result.retrieved_at,
            content_sha256=result.content_sha256, exact_entity=True,
            identity_proof=proof, acquisition_mode=self.acquisition_mode,
            rights_status="approved", source_class=SourceClass.CUSTOMER_REVIEW,
            evidence_span=span,
            metrics={"rating": rating, "rating_count": rating_count,
                     "place_id": match.get("id"),
                     "category": (match.get("primaryTypeDisplayName") or {}).get("text")},
        )]
        self.published += 1
        return claims, [ev], [o for o in observations if not validate_observation(o)]

    # -- identity --------------------------------------------------------

    def _verify(
        self, places: list[dict[str, Any]], identity: dict[str, Any], verified_domain: str | None
    ) -> tuple[dict[str, Any] | None, str, str | None]:
        """An entity-specific match, or nothing. A shared address is not one."""
        registry_phone = _digits(identity.get("phone"))

        for place in places:
            if verified_domain and _host(place.get("websiteUri")) == _host(verified_domain):
                return place, PROOF_DOMAIN, f"place website {place.get('websiteUri')} matches the verified company domain"

        if len(registry_phone) >= 8:
            for place in places:
                for field in ("nationalPhoneNumber", "internationalPhoneNumber"):
                    if _digits(place.get(field)).endswith(registry_phone[-8:]):
                        return place, PROOF_PHONE, f"place phone {place.get(field)} matches the registry switchboard"

        return None, "", None

    @staticmethod
    def _unresolved(state: Availability, note: str) -> list[Claim]:
        return [
            Claim(field=field, value=None, availability=state, confidence=0.0, note=note)
            for field in ("places.rating", "places.rating_count", "places.address")
        ]
