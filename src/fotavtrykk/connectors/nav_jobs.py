"""NAV job vacancies via the official stilling-feed API.

https://navikt.github.io/pam-stilling-feed/ — Norway's public employment
service. Bearer-token API; a public token is published for use and a stable
private one is free on request.

Why the feed rather than the site's search endpoint: `/stillinger/api/search`
is an undocumented internal endpoint that returns 429 after a handful of calls
and stays blocked for a long window. It cannot carry a 100-company batch.

Shape of the work
-----------------
The feed is a changelog, so it is walked **once per batch**, not once per
company. `If-Modified-Since` jumps straight to recent entries. Each page lists
1000 entries carrying a business *name* but no organisation number, so:

  1. Walk the feed → active entries (name only). Candidates.
  2. Name-match against the batch locally. Still candidates — costs requests,
     never precision.
  3. Fetch only matched entries. Each carries `employer.orgnr`.
  4. Publish only on an exact organisation-number match.

A name match alone is the tier already measured at 84% precision and dropped.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any, Iterable

from ..http import Fetcher
from ..models import Availability, Claim, Evidence, Observation, SourceClass, validate_observation
from ..orgnr import name_tokens, normalise

FEED_BASE = "https://pam-stilling-feed.nav.no"
FEED_URL = f"{FEED_BASE}/api/v1/feed"
PUBLIC_TOKEN_URL = f"{FEED_BASE}/api/publicToken"
TERMS_URL = "https://arbeidsplassen.nav.no/vilkar-api"

# The feed is a changelog, so a window only surfaces adverts *modified* inside
# it. An advert published 45 days ago and still open never appears in a 30-day
# walk. Measured against Norway's ~13,500 active adverts:
#   30 days / 25 pages -> 6,587 distinct active seen (48.8%)
#   60 days / 50 pages -> 13,359                     (99.0%)
#  120 days / 100 pages -> no further gain, just re-modified duplicates
# 51 requests for near-total visibility is cheap against a 2,000 cap, and job
# coverage is scored as recall against the companies that *have* a posting, so
# a missed advert costs far more than a spent request.
DEFAULT_WINDOW_DAYS = 60
DEFAULT_MAX_PAGES = 50
DEFAULT_MAX_ADS = 150
FEED_TIMEOUT = 30.0  # feed pages are ~500KB, unlike the homepages the default targets


class NavJobsSource:
    """Batch-level NAV vacancy lookup. Primed once, then read per company."""

    platform = "job_board"
    acquisition_mode = "official_api"

    def __init__(self, fetcher: Fetcher, *, token: str | None = None) -> None:
        self.fetcher = fetcher
        self.token = token
        self.by_org: dict[str, list[dict[str, Any]]] = {}
        self.evidence: dict[str, Evidence] = {}
        self.primed = False
        self.error: str | None = None
        self.stats: dict[str, Any] = {}

    # -- priming ---------------------------------------------------------

    async def prime(
        self, companies: Iterable[tuple[str, str]], *,
        window_days: int = DEFAULT_WINDOW_DAYS,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_ads: int = DEFAULT_MAX_ADS,
    ) -> None:
        """Walk the feed and resolve candidate adverts for this batch."""
        self.primed = True
        wanted = {org: set(name_tokens(name)) for org, name in companies if name}
        if not wanted:
            self.error = "no companies to match"
            return

        token = self.token or await self._public_token()
        if not token:
            self.error = "no NAV feed token available"
            return
        headers = {"Authorization": f"Bearer {token}"}

        since = format_datetime(datetime.now(timezone.utc) - timedelta(days=window_days))
        candidates: list[dict[str, Any]] = []
        url: str | None = FEED_URL
        pages = 0

        while url and pages < max_pages:
            page = await self.fetcher.get(
                url, accept="application/json", timeout=FEED_TIMEOUT,
                headers={**headers, "If-Modified-Since": since} if pages == 0 else headers,
            )
            pages += 1
            if not page.ok:
                self.error = self.error or f"feed page failed: {page.error}"
                break
            try:
                payload = json.loads(page.text)
            except json.JSONDecodeError:
                self.error = "feed page was not JSON"
                break

            for item in payload.get("items", []):
                entry = item.get("_feed_entry") or {}
                if entry.get("status") != "ACTIVE":
                    continue
                tokens = set(name_tokens(entry.get("businessName") or ""))
                if not tokens:
                    continue
                for org, want in wanted.items():
                    # Loose on purpose: this only selects what to verify. But a
                    # single generic token ("holding", "gruppen") matches half of
                    # Norway, so score the match and let the good ones sort first.
                    if not want or not want.issubset(tokens):
                        continue
                    # The feed's ACTIVE flag is the status at the time of that
                    # changelog event, not now: sampling 40 entries marked
                    # ACTIVE, 39 came back INACTIVE from the detail endpoint,
                    # which masks `employer` on inactive ads. So rank by how
                    # recently the advert changed -- the freshest are the ones
                    # most likely still open -- and let an exact name match win
                    # ties. This spends the fetch budget where it can resolve.
                    exact = want == tokens
                    score = (2 if exact else 1, str(entry.get("sistEndret") or ""))
                    candidates.append({
                        "org": org, "url": item.get("url"), "entry": entry, "score": score,
                    })
                    break

            nxt = payload.get("next_url")
            url = f"{FEED_BASE}{nxt}" if nxt else None

        # Dedupe *then* truncate, best match first. Truncating raw feed order
        # starved real matches behind hundreds of single-token collisions --
        # measured as 653 candidates collapsing to 79 fetches and 0 resolved.
        ranked: dict[str, dict[str, Any]] = {}
        for candidate in sorted(candidates, key=lambda c: c["score"], reverse=True):
            path = candidate["url"]
            if path and path not in ranked:
                ranked[path] = candidate

        seen: set[str] = set()
        resolved = 0
        for candidate in list(ranked.values())[:max_ads]:
            path = candidate["url"]
            seen.add(path)
            detail = await self.fetcher.get(
                f"{FEED_BASE}{path}", accept="application/json",
                headers=headers, timeout=FEED_TIMEOUT,
            )
            if not detail.ok:
                continue
            try:
                content = (json.loads(detail.text) or {}).get("ad_content") or {}
            except json.JSONDecodeError:
                continue
            employer = content.get("employer") or {}
            org = normalise(employer.get("orgnr"))
            if not org or org not in wanted:
                continue  # Someone else's advert. Never publish on a name match.

            location = (content.get("workLocations") or [{}])[0]
            self.by_org.setdefault(org, []).append({
                "uuid": content.get("uuid"),
                "title": content.get("title") or content.get("jobtitle"),
                "employer": employer.get("name"),
                "published": content.get("published"),
                "expires": content.get("expires"),
                "municipality": location.get("municipal") or location.get("city"),
                "positions": content.get("positioncount"),
                "url": content.get("link") or f"{FEED_BASE}{path}",
                "source_url": f"{FEED_BASE}{path}",
                "retrieved_at": detail.retrieved_at,
                "content_sha256": detail.content_sha256,
            })
            resolved += 1

        self.stats = {
            "pages_walked": pages,
            "active_candidates": len(candidates),
            "distinct_candidates": len(ranked),
            "adverts_fetched": len(seen),
            "adverts_resolved": resolved,
            "adverts_expired_or_masked": len(seen) - resolved,
            "companies_with_jobs": len(self.by_org),
            "window_days": window_days,
        }

    async def _public_token(self) -> str | None:
        import re

        result = await self.fetcher.get(PUBLIC_TOKEN_URL)
        if not result.ok:
            return None
        match = re.search(r"(eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)", result.text)
        return match.group(1) if match else None

    # -- per company -----------------------------------------------------

    def collect(self, org: str) -> tuple[list[Claim], list[Evidence], list[Observation]]:
        if not self.primed or self.error:
            note = f"NAV vacancy feed unavailable: {self.error or 'not primed'}"
            return (
                [Claim(field=f, value=None, availability=Availability.FAILED, confidence=0.0, note=note)
                 for f in ("jobs.active_count", "jobs.postings")],
                [], [],
            )

        postings = self.by_org.get(org, [])
        evidence: list[Evidence] = []
        observations: list[Observation] = []

        for index, posting in enumerate(postings):
            ev_id = f"ev-job-{index}"
            evidence.append(Evidence(
                id=ev_id, source_url=posting["source_url"],
                source_class=SourceClass.PUBLIC_MENTION,
                retrieved_at=posting["retrieved_at"],
                content_sha256=posting["content_sha256"],
                claim_span=f'employer.orgnr {org} — {posting["title"]}',
                extraction_method="nav_stilling_feed_v1",
            ))
            observations.append(Observation(
                id=f"{org}-job_board-posting-{index}",
                organisation_number=org,
                platform=self.platform,
                signal_type="job_posting",
                source_url=posting["source_url"],
                retrieved_at=posting["retrieved_at"],
                content_sha256=posting["content_sha256"],
                exact_entity=True,
                identity_proof="employer_org_number_in_official_feed",
                acquisition_mode=self.acquisition_mode,
                rights_status="approved",
                source_class=SourceClass.PUBLIC_MENTION,
                evidence_span=str(posting["title"])[:300],
                observed_at=posting["published"],
                metrics={k: posting[k] for k in ("published", "expires", "municipality", "positions")},
            ))

        # The feed was read. A checked zero is a fact and is not the same as
        # "not checked" — the contract asks for exactly that distinction.
        claims = [
            Claim(field="jobs.active_count", value=len(postings),
                  availability=Availability.AVAILABLE, confidence=1.0,
                  evidence_ids=[e.id for e in evidence],
                  note=None if postings else
                       "NAV vacancy feed was read; no active advert carries this organisation number"),
            Claim(field="jobs.postings", value=postings or None,
                  availability=Availability.AVAILABLE if postings else Availability.NOT_AVAILABLE,
                  confidence=1.0 if postings else 0.0,
                  evidence_ids=[e.id for e in evidence]),
        ]
        return claims, evidence, [o for o in observations if not validate_observation(o)]
