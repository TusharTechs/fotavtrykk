"""Company website: the org-number proof gate.

This module decides whether a domain may be attributed to a legal entity. It is
the highest-risk code in the system -- one wrong-company publication ends
qualification -- so it abstains by default and publishes only on proof.

Proof ladder, strongest first:
  1. The organisation number appears on the page next to an org-number label.
  2. A MOD-11-valid organisation number matching ours appears anywhere on it.
  3. The registry itself declares this website AND the full legal-name token set
     appears in the homepage identity evidence.
Anything weaker returns `ambiguous`.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser

from . import orgnr
from .http import Fetcher, FetchResult
from .models import (
    Availability, Claim, Evidence, Observation, SourceClass, validate_observation,
)

SOCIAL_HOSTS = {
    "linkedin.com": "linkedin",
    "facebook.com": "facebook",
    "fb.com": "facebook",
    "instagram.com": "instagram",
    "twitter.com": "x",
    "x.com": "x",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "tiktok.com": "tiktok",
}

# Pages worth one extra request each once identity is proven. Norwegian first.
PRIORITY_PATHS = ("/om-oss", "/kontakt", "/about", "/contact")

PARKED_MARKERS = (
    "domain is for sale", "domain for sale", "hugedomains", "parked at",
    "this domain may be for sale", "buy this domain", "under construction",
    # Default hosting pages. The domain may well be the company's, but there is
    # no profile on it to publish.
    "something amazing will be constructed", "upload your website",
    "public_html", "index of /", "welcome to nginx", "apache2 ubuntu default",
)


# Emitted by every envelope regardless of outcome, so a field never appears or
# disappears between runs. See registry.FINANCIAL_FIELDS for the same rule.
WEBSITE_FIELDS = ("official_website", "website_title", "website_description", "social_handles")


class SiteResolver:
    def __init__(self, fetcher: Fetcher) -> None:
        self.fetcher = fetcher

    @staticmethod
    def _unresolved(
        state: Availability, note: str, *, value=None, confidence: float = 0.0,
        evidence_ids: list[str] | None = None,
    ) -> list[Claim]:
        """The full website key set when identity was not established."""
        ids = evidence_ids or []
        head = Claim(
            field="official_website", value=value, availability=state,
            confidence=confidence, evidence_ids=ids, note=note,
        )
        rest = [
            Claim(field=field, value=None, availability=state, confidence=0.0,
                  evidence_ids=ids, note=note)
            for field in WEBSITE_FIELDS[1:]
        ]
        return [head, *rest]

    async def resolve(
        self, org: str, legal_name: str, seed_url: str | None,
        identity: dict | None = None,
    ) -> tuple[list[Claim], list[Evidence], list[Observation]]:
        if not seed_url:
            return self._unresolved(
                Availability.NOT_AVAILABLE,
                "no website registered in Enhetsregisteret and search discovery is not enabled in v1",
            ), [], []

        result = await self.fetcher.get(seed_url)
        if not result.ok:
            state = Availability.BLOCKED if result.blocked else Availability.FAILED
            return self._unresolved(
                state, f"registry website could not be retrieved: {result.error}",
            ), [], []

        page = self._parse(result.text)
        identity_text = " ".join(filter(None, [
            page["title"], page["description"], page["identity_excerpt"], page["structured_names"],
        ]))
        full_text = f"{identity_text} {page['body_text']}"

        proof, span = orgnr.find_org_number_proof(full_text, org)
        if not proof:
            proof, span = self._name_fallback(org, legal_name, identity_text, page, identity or {})

        ev = Evidence(
            id="ev-website",
            source_url=result.url,
            source_class=SourceClass.COMPANY_OWNED,
            retrieved_at=result.retrieved_at,
            content_sha256=result.content_sha256,
            final_url=result.final_url,
            http_status=result.status,
            claim_span=span,
            extraction_method="html_identity_gate_v1",
            tls_verified=result.tls_verified,
        )

        if not proof:
            # The registry pointed here, but the page does not prove it. Abstain.
            return self._unresolved(
                Availability.AMBIGUOUS,
                "registry-declared site lacks exact-entity evidence; not published as verified",
                value=result.final_url or result.url, confidence=0.5, evidence_ids=[ev.id],
            ), [ev], []

        confidence = orgnr.proof_confidence(proof)
        claims = [
            Claim(field="official_website", value=result.final_url or result.url,
                  availability=Availability.AVAILABLE, confidence=confidence,
                  evidence_ids=[ev.id], qualifiers={"identity_proof": proof}),
            Claim(field="website_title", value=page["title"] or None,
                  availability=Availability.AVAILABLE if page["title"] else Availability.NOT_AVAILABLE,
                  confidence=confidence if page["title"] else 0.0, evidence_ids=[ev.id]),
            Claim(field="website_description", value=page["description"] or None,
                  availability=Availability.AVAILABLE if page["description"] else Availability.NOT_AVAILABLE,
                  confidence=confidence if page["description"] else 0.0, evidence_ids=[ev.id]),
        ]

        observations = [Observation(
            id=f"{org}-company_site-home",
            organisation_number=org,
            platform="company_site",
            signal_type="company_profile",
            source_url=result.final_url or result.url,
            retrieved_at=result.retrieved_at,
            content_sha256=result.content_sha256,
            exact_entity=True,
            identity_proof=proof,
            acquisition_mode="permitted_public_page",
            rights_status="approved",
            source_class=SourceClass.COMPANY_OWNED,
            evidence_span=span,
            metrics={"title": page["title"][:200]} if page["title"] else None,
        )]

        handles = self._social_handles(page["links"], result.final_url or result.url)
        claims.append(Claim(
            field="social_handles", value=handles or None,
            availability=Availability.AVAILABLE if handles else Availability.NOT_AVAILABLE,
            confidence=confidence if handles else 0.0, evidence_ids=[ev.id],
            note=None if handles else "no social profiles linked from the verified company site",
        ))

        for index, handle in enumerate(handles):
            # Encoding note: source_url is the company page we actually captured
            # and hashed. The destination platform is named in `platform`, and the
            # handle URL rides in metrics. We never fetch the destination itself.
            observations.append(Observation(
                id=f"{org}-{handle['platform']}-handle-{index}",
                organisation_number=org,
                platform=handle["platform"],
                signal_type="profile_handle",
                source_url=result.final_url or result.url,
                retrieved_at=result.retrieved_at,
                content_sha256=result.content_sha256,
                exact_entity=True,
                identity_proof=f"declared_on_verified_company_site:{proof}",
                acquisition_mode="permitted_public_page",
                rights_status="approved",
                source_class=SourceClass.COMPANY_OWNED,
                evidence_span=handle["url"],
                metrics={"declared_url": handle["url"]},
            ))

        return claims, [ev], [o for o in observations if not validate_observation(o)]

    # -- identity fallback ----------------------------------------------

    @staticmethod
    def _corroboration(page: dict, identity: dict) -> str | None:
        """Does the page carry registry facts other than the name?

        The postcode, town and switchboard number come from Enhetsregisteret,
        not from the page, so finding them is evidence independent of the
        legal-name match. Phone is the distinctive one — a postcode alone is
        shared by every company in the same town, so it only counts alongside
        the town name and the name match that already passed.

        Measured on the audit corpus: 56% of registry-declared sites corroborate.
        The rest are mostly large listed companies whose marketing homepage
        carries no postal address or switchboard number.
        """
        haystack = f"{page['body_text']} {page['identity_excerpt']}"
        digits = re.sub(r"\D", "", haystack)
        phone = str(identity.get("phone") or "")
        if len(phone) >= 8 and phone[-8:] in digits:
            return f"registry switchboard {phone[-8:]} appears on the page"
        postcode, city = identity.get("postcode"), identity.get("city")
        if postcode and city and postcode in haystack:
            lowered = haystack.casefold()
            # "KRISTIANSAND S" is filed with a directional suffix the page drops.
            forms = {city.casefold(), re.sub(r"\s+[a-zæøå]$", "", city.casefold()).strip()}
            if any(form and form in lowered for form in forms):
                return f"registry address {postcode} {city} appears on the page"
        return None

    def _name_fallback(
        self, org: str, legal_name: str, identity_text: str, page: dict, identity: dict
    ) -> tuple[str, str | None]:
        """Registry-declared site plus the complete legal-name token set,
        upgraded when an independent registry fact also appears on the page."""
        lowered = page["body_text"].casefold()
        if any(marker in lowered for marker in PARKED_MARKERS):
            return orgnr.PROOF_NONE, None

        # Negative evidence beats a name match. If the page identifies itself as
        # a different legal entity, it is not ours however well the name reads.
        # Found by audit: the name fallback alone would publish a parent or
        # group site that happens to mention the subsidiary.
        if orgnr.find_conflicting_org_numbers(page["body_text"], org):
            return orgnr.PROOF_NONE, None

        core = set(orgnr.name_tokens(legal_name))
        if not core:
            return orgnr.PROOF_NONE, None
        present = set(orgnr.name_tokens(identity_text))
        if not core.issubset(present):
            return orgnr.PROOF_NONE, None
        # A one-token name is weak evidence unless the page has real content.
        if len(core) == 1 and len(page["body_text"].strip()) < 100:
            return orgnr.PROOF_NONE, None

        corroboration = self._corroboration(page, identity)
        if corroboration:
            return orgnr.PROOF_CORROBORATED, corroboration

        # A full legal-name token match on a registry-declared site is NOT
        # sufficient. Audited at 84% exact-entity precision (8 wrong of 50)
        # against a 99.5% floor: it publishes group and holding sites such as
        # klaveness.com for a listed subsidiary, and a parent's consumer portal
        # for the parent. Abstain; the claim becomes `ambiguous`.
        return orgnr.PROOF_NONE, None

    # -- parsing ---------------------------------------------------------

    @staticmethod
    def _parse(html: str) -> dict:
        tree = HTMLParser(html or "")
        for node in tree.css("script, style, noscript, svg"):
            node.decompose()

        title = (tree.css_first("title").text(strip=True) if tree.css_first("title") else "") or ""
        description = ""
        for selector in ('meta[name="description"]', 'meta[property="og:description"]'):
            node = tree.css_first(selector)
            if node and node.attributes.get("content"):
                description = node.attributes["content"].strip()
                break

        body = tree.body.text(separator=" ", strip=True) if tree.body else ""
        body = re.sub(r"\s+", " ", body)

        # Footers carry org numbers far more often than headers do.
        identity_excerpt = " ".join(
            node.text(separator=" ", strip=True)
            for node in tree.css("footer, address, .footer, #footer")
        )[:4000]

        structured = " ".join(
            node.text(strip=True)
            for node in HTMLParser(html or "").css('script[type="application/ld+json"]')
        )[:4000]

        links = [a.attributes.get("href", "") for a in tree.css("a[href]")]
        return {
            "title": title[:300],
            "description": description[:600],
            "body_text": body[:200_000],
            "identity_excerpt": identity_excerpt,
            "structured_names": structured,
            "links": links,
        }

    @staticmethod
    def _social_handles(links: list[str], base: str) -> list[dict[str, str]]:
        found: dict[str, str] = {}
        for href in links:
            if not href or href.startswith(("mailto:", "tel:", "javascript:")):
                continue
            absolute = urljoin(base, href)
            host = (urlparse(absolute).hostname or "").lower().removeprefix("www.")
            for domain, platform in SOCIAL_HOSTS.items():
                if host == domain or host.endswith(f".{domain}"):
                    path = urlparse(absolute).path.strip("/")
                    if path and platform not in found:
                        found[platform] = absolute
                    break
        return [{"platform": p, "url": u} for p, u in sorted(found.items())]
