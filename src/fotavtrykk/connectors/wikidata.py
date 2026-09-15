"""Wikidata — exact identity, extra platforms, curated handles.

Wikidata carries property **P2333, the Norwegian organisation number**, for
10,311 entities (7,326 with an official website). That makes entity resolution
decisive rather than inferred: the organisation number is the match key, so
there is no namesake risk.

It is also cheap. `haswbstatement:P2333=A|P2333=B|...` batches many companies
into one search, and `wbgetentities` takes 50 ids at a time, so a 100-company
batch costs roughly six requests in total rather than two per company.

Licence and access: Wikidata content is CC0. `www.wikidata.org/robots.txt`
restricts only named misbehaving crawlers, and the MediaWiki API is the
supported programmatic interface — unlike `query.wikidata.org/sparql`, which is
`Disallow: /sparql` and is therefore not used here.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Sequence

from ..http import Fetcher
from ..models import Availability, Claim, Evidence, Observation, validate_observation
from ..orgnr import normalise

API = "https://www.wikidata.org/w/api.php"
ENTITY_URL = "https://www.wikidata.org/wiki/{qid}"

ORG_NUMBER_PROPERTY = "P2333"
SEARCH_BATCH = 50   # organisation numbers per search
ENTITY_BATCH = 50   # entity ids per wbgetentities call

# Scalar facts worth publishing.
VALUE_PROPERTIES = {
    "P856": "wikidata.website",
    "P571": "wikidata.inception",
    "P1128": "wikidata.employees",
}

# Social handles the community has curated onto the entity. Each becomes a
# profile_handle on its own platform, which is what `two_platforms` counts.
HANDLE_PROPERTIES = {
    "P2002": ("x", "https://x.com/{}"),
    "P2013": ("facebook", "https://www.facebook.com/{}"),
    "P2003": ("instagram", "https://www.instagram.com/{}"),
    "P2397": ("youtube", "https://www.youtube.com/channel/{}"),
    "P4264": ("linkedin", "https://www.linkedin.com/company/{}"),
}

WIKIPEDIA_SITES = {"nowiki": "no", "enwiki": "en"}


def _claim_value(entity: dict[str, Any], prop: str) -> Any:
    try:
        snak = entity["claims"][prop][0]["mainsnak"]
        if snak.get("snaktype") != "value":
            return None
        value = snak["datavalue"]["value"]
    except (KeyError, IndexError, TypeError):
        return None
    if isinstance(value, dict):
        # Times arrive as "+2004-01-01T00:00:00Z"; amounts as {"amount": "+120"}.
        if "time" in value:
            return str(value["time"]).lstrip("+")[:10]
        if "amount" in value:
            return str(value["amount"]).lstrip("+")
        return None
    return value


class WikidataSource:
    """Batch-level Wikidata lookup. Primed once, then read per company."""

    platform = "wikidata"
    acquisition_mode = "official_api"

    def __init__(self, fetcher: Fetcher) -> None:
        self.fetcher = fetcher
        self.by_org: dict[str, dict[str, Any]] = {}
        self.primed = False
        self.error: str | None = None
        self.stats: dict[str, Any] = {}

    async def prime(self, organisations: Iterable[str]) -> None:
        self.primed = True
        orgs = [normalise(o) for o in organisations]
        orgs = [o for o in orgs if len(o) == 9]
        if not orgs:
            self.error = "no organisation numbers to look up"
            return

        qids: list[str] = []
        searches = 0
        for chunk in _chunks(orgs, SEARCH_BATCH):
            query = "haswbstatement:" + "|".join(f"{ORG_NUMBER_PROPERTY}={o}" for o in chunk)
            result = await self.fetcher.get(
                f"{API}?action=query&list=search&srsearch={_quote(query)}"
                f"&srlimit={SEARCH_BATCH}&format=json",
                accept="application/json",
            )
            searches += 1
            if not result.ok:
                self.error = self.error or f"Wikidata search failed: {result.error}"
                continue
            try:
                hits = json.loads(result.text).get("query", {}).get("search", [])
            except json.JSONDecodeError:
                continue
            qids.extend(h["title"] for h in hits if h.get("title", "").startswith("Q"))

        wanted = set(orgs)
        fetches = 0
        for chunk in _chunks(sorted(set(qids)), ENTITY_BATCH):
            result = await self.fetcher.get(
                f"{API}?action=wbgetentities&ids={'|'.join(chunk)}"
                f"&props=claims|labels|sitelinks&languages=nb|en"
                f"&sitefilter={'|'.join(WIKIPEDIA_SITES)}&format=json",
                accept="application/json",
            )
            fetches += 1
            if not result.ok:
                continue
            try:
                entities = json.loads(result.text).get("entities", {})
            except json.JSONDecodeError:
                continue
            for qid, entity in entities.items():
                org = normalise(_claim_value(entity, ORG_NUMBER_PROPERTY))
                # The organisation number is the match key. No name is consulted.
                if org not in wanted:
                    continue
                self.by_org[org] = {
                    "qid": qid,
                    "label": (entity.get("labels", {}).get("nb")
                              or entity.get("labels", {}).get("en") or {}).get("value"),
                    "values": {field: _claim_value(entity, prop)
                               for prop, field in VALUE_PROPERTIES.items()},
                    "handles": [
                        {"platform": platform,
                         "url": template.format(_claim_value(entity, prop)),
                         "property": prop}
                        for prop, (platform, template) in HANDLE_PROPERTIES.items()
                        if _claim_value(entity, prop)
                    ],
                    "wikipedia": [
                        {"lang": lang,
                         "url": f"https://{lang}.wikipedia.org/wiki/"
                                f"{(entity.get('sitelinks', {}).get(site) or {}).get('title', '').replace(' ', '_')}"}
                        for site, lang in WIKIPEDIA_SITES.items()
                        if (entity.get("sitelinks", {}) or {}).get(site)
                    ],
                    "retrieved_at": result.retrieved_at,
                    "content_sha256": result.content_sha256,
                }

        self.stats = {
            "organisations": len(orgs),
            "searches": searches,
            "entity_fetches": fetches,
            "entities_matched": len(self.by_org),
            "requests": searches + fetches,
        }

    def collect(self, org: str) -> tuple[list[Claim], list[Evidence], list[Observation]]:
        if not self.primed or self.error:
            note = f"Wikidata unavailable: {self.error or 'not primed'}"
            return (
                [Claim(field=f, value=None, availability=Availability.FAILED,
                       confidence=0.0, note=note)
                 for f in ("wikidata.qid", *VALUE_PROPERTIES.values(), "wikidata.wikipedia")],
                [], [],
            )

        entry = self.by_org.get(org)
        if not entry:
            note = "no Wikidata entity carries this organisation number"
            return (
                [Claim(field=f, value=None, availability=Availability.NOT_AVAILABLE,
                       confidence=0.0, note=note)
                 for f in ("wikidata.qid", *VALUE_PROPERTIES.values(), "wikidata.wikipedia")],
                [], [],
            )

        qid = entry["qid"]
        entity_url = ENTITY_URL.format(qid=qid)
        ev = Evidence(
            id="ev-wikidata", source_url=entity_url, source_class="public_mention",
            retrieved_at=entry["retrieved_at"], content_sha256=entry["content_sha256"],
            claim_span=f"{ORG_NUMBER_PROPERTY} = {org} on {qid} ({entry['label']})",
            extraction_method="wikidata_wbgetentities_v1",
        )

        claims = [Claim(field="wikidata.qid", value=qid, availability=Availability.AVAILABLE,
                        confidence=1.0, evidence_ids=[ev.id],
                        qualifiers={"identity_proof": "wikidata_p2333_organisation_number"})]
        for field, value in entry["values"].items():
            claims.append(Claim(
                field=field, value=value,
                availability=Availability.AVAILABLE if value else Availability.NOT_AVAILABLE,
                confidence=0.9 if value else 0.0, evidence_ids=[ev.id],
                note=None if value else "not recorded on the Wikidata entity",
            ))
        wikipedia = entry["wikipedia"]
        claims.append(Claim(
            field="wikidata.wikipedia", value=wikipedia or None,
            availability=Availability.AVAILABLE if wikipedia else Availability.NOT_AVAILABLE,
            confidence=0.9 if wikipedia else 0.0, evidence_ids=[ev.id]))

        observations = [Observation(
            id=f"{org}-wikidata-entity", organisation_number=org, platform=self.platform,
            signal_type="company_profile", source_url=entity_url,
            retrieved_at=entry["retrieved_at"], content_sha256=entry["content_sha256"],
            exact_entity=True, identity_proof="wikidata_p2333_organisation_number",
            acquisition_mode=self.acquisition_mode, rights_status="approved",
            source_class="public_mention",
            metrics={"qid": qid, "label": entry["label"]},
        )]

        for page in wikipedia:
            observations.append(Observation(
                id=f"{org}-wikipedia-{page['lang']}", organisation_number=org,
                platform="wikipedia", signal_type="company_profile", source_url=page["url"],
                retrieved_at=entry["retrieved_at"], content_sha256=entry["content_sha256"],
                exact_entity=True,
                identity_proof=f"wikidata_p2333_sitelink:{qid}",
                acquisition_mode=self.acquisition_mode, rights_status="approved",
                source_class="public_mention", metrics={"language": page["lang"]},
            ))

        for index, handle in enumerate(entry["handles"]):
            observations.append(Observation(
                id=f"{org}-{handle['platform']}-wikidata-{index}", organisation_number=org,
                platform=handle["platform"], signal_type="profile_handle",
                # The captured payload is the Wikidata entity, so that is the
                # source URL; the destination rides in metrics and is not fetched.
                source_url=entity_url,
                retrieved_at=entry["retrieved_at"], content_sha256=entry["content_sha256"],
                exact_entity=True,
                identity_proof=f"wikidata_p2333_statement:{handle['property']}",
                acquisition_mode=self.acquisition_mode, rights_status="approved",
                source_class="public_mention", evidence_span=handle["url"],
                metrics={"declared_url": handle["url"], "property": handle["property"]},
            ))

        return claims, [ev], [o for o in observations if not validate_observation(o)]


def _chunks(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value)
