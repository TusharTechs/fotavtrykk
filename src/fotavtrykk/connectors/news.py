"""Dated public activity.

Targets the `buzz_engagement` family (`public_post`, `public_mention`,
`profile_metrics`).

Why not Google News
-------------------
`news.google.com/robots.txt` is `Disallow: /` with a short allow-list that does
not include `/rss/`, and the file names ClaudeBot and anthropic-ai explicitly.
The source policy forbids scraping against a robots policy, so the RSS search
endpoint is not usable however convenient it is. Its article links are Google
redirects too, which would require another disallowed fetch to resolve.

What this does instead
----------------------
Reads the company's **own** news, press or blog page — on a site that already
passed the organisation-number gate. Identity is inherited from that proof, so
there is no namesake risk at all: a company writing on its own verified domain
is unambiguously that company.

The trade is that this is company-owned copy. It is a legitimate dated activity
signal and it counts toward `buzz_engagement`, but the source policy is explicit
that company-owned promotional copy cannot supply an independent sentiment
claim, so these observations never carry a sentiment label.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse

from selectolax.parser import HTMLParser

from ..http import Fetcher
from ..models import Availability, Claim, Evidence, Observation, SourceClass, validate_observation

# Norwegian first, then English. `aktuelt` and `nyheter` are the common ones.
NEWS_HINT = re.compile(
    r"nyhet|aktuelt|presse|press|news|blogg|blog|artikler|arkiv|media", re.I
)
NEWS_PATHS = ("/nyheter", "/aktuelt", "/news", "/presse", "/blogg", "/blog")

ISO_DATE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")
# "14. september 2026" and "14.09.2026"
NO_MONTHS = {m: i + 1 for i, m in enumerate((
    "januar", "februar", "mars", "april", "mai", "juni", "juli",
    "august", "september", "oktober", "november", "desember"))}
NO_DATE = re.compile(r"(\d{1,2})\.?\s+(" + "|".join(NO_MONTHS) + r")\s+(20\d{2})", re.I)
NUMERIC_DATE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(20\d{2})")

MAX_PAGES = 2
MAX_POSTS = 10


class CompanyNewsSource:
    """Dated posts from a company's own verified site."""

    platform = "company_site"
    acquisition_mode = "permitted_public_page"

    def __init__(self, fetcher: Fetcher, *, max_pages: int = MAX_PAGES) -> None:
        self.fetcher = fetcher
        self.max_pages = max_pages

    async def collect(
        self, org: str, site_url: str | None, site_html: str | None,
        identity_proof: str | None,
    ) -> tuple[list[Claim], list[Evidence], list[Observation]]:
        if not site_url or not identity_proof:
            return self._unresolved(
                Availability.NOT_AVAILABLE,
                "no verified company website, so no company-owned activity can be attributed",
            ), [], []

        # Fetched together, not one after another: this runs after the site
        # request, so serial candidates push p95 towards the latency gate.
        candidates = self._candidates(site_html or "", site_url)[: self.max_pages]
        pages = await asyncio.gather(
            *(self.fetcher.get(url) for url in candidates), return_exceptions=True
        )
        for page in pages:
            if isinstance(page, BaseException) or not page.ok:
                continue
            posts = self._extract(page.text, page.final_url or page.url)
            if not posts:
                continue

            ev = Evidence(
                id="ev-activity", source_url=page.final_url or page.url,
                source_class=SourceClass.COMPANY_OWNED, retrieved_at=page.retrieved_at,
                content_sha256=page.content_sha256, final_url=page.final_url,
                http_status=page.status,
                claim_span=f"{len(posts)} dated posts; latest: {posts[0]['title'][:90]}",
                extraction_method="company_news_page_v1", tls_verified=page.tls_verified,
            )
            observations = [
                Observation(
                    id=f"{org}-company_site-post-{index}",
                    organisation_number=org, platform=self.platform,
                    signal_type="public_post",
                    source_url=post["url"] or (page.final_url or page.url),
                    retrieved_at=page.retrieved_at, content_sha256=page.content_sha256,
                    exact_entity=True,
                    identity_proof=f"published_on_verified_company_site:{identity_proof}",
                    acquisition_mode=self.acquisition_mode, rights_status="approved",
                    source_class=SourceClass.COMPANY_OWNED,
                    # Required for public_post, and the reviewable quote.
                    evidence_span=post["title"][:300],
                    observed_at=post["date"],
                    # No sentiment_label, ever: company-owned copy is not an
                    # independent sentiment source under the policy.
                    metrics={"published": post["date"]},
                )
                for index, post in enumerate(posts[:MAX_POSTS])
            ]
            claims = [
                Claim(field="activity.latest_post_date", value=posts[0]["date"],
                      availability=Availability.AVAILABLE if posts[0]["date"] else Availability.NOT_AVAILABLE,
                      confidence=0.9, evidence_ids=[ev.id]),
                Claim(field="activity.posts", value=posts[:MAX_POSTS],
                      availability=Availability.AVAILABLE, confidence=0.9,
                      evidence_ids=[ev.id],
                      qualifiers={"source": "company_owned",
                                  "sentiment_eligible": False}),
            ]
            return claims, [ev], [o for o in observations if not validate_observation(o)]

        return self._unresolved(
            Availability.NOT_AVAILABLE,
            "verified company site has no dated news, press or blog page we could read",
        ), [], []

    # -- discovery -------------------------------------------------------

    @staticmethod
    def _candidates(html: str, base: str) -> list[str]:
        host = (urlparse(base).hostname or "").removeprefix("www.")
        found: list[str] = []
        for anchor in HTMLParser(html or "").css("a[href]"):
            href = anchor.attributes.get("href") or ""
            label = anchor.text(strip=True) or ""
            if not href or not (NEWS_HINT.search(href) or NEWS_HINT.search(label)):
                continue
            absolute = urljoin(base, href)
            if (urlparse(absolute).hostname or "").removeprefix("www.") == host:
                found.append(absolute)
        found += [urljoin(base, path) for path in NEWS_PATHS]

        seen: dict[str, str] = {}
        root = urldefrag(base)[0].rstrip("/").casefold()
        for url in found:
            key = urldefrag(url)[0].rstrip("/").casefold()
            if key and key != root and key not in seen:
                seen[key] = urldefrag(url)[0]
        return list(seen.values())

    # -- extraction ------------------------------------------------------

    def _extract(self, html: str, base: str) -> list[dict[str, Any]]:
        """Structured data first, then dated DOM elements. Never invent a date."""
        posts = self._from_structured_data(html, base) or self._from_dom(html, base)
        dated = [p for p in posts if p["date"]]
        dated.sort(key=lambda p: p["date"], reverse=True)
        return dated

    @staticmethod
    def _from_structured_data(html: str, base: str) -> list[dict[str, Any]]:
        posts: list[dict[str, Any]] = []
        for node in HTMLParser(html or "").css('script[type="application/ld+json"]'):
            try:
                payload = json.loads(node.text())
            except (json.JSONDecodeError, TypeError):
                continue
            stack = [payload]
            while stack:
                item = stack.pop()
                if isinstance(item, list):
                    stack.extend(item)
                    continue
                if not isinstance(item, dict):
                    continue
                stack.extend(v for v in item.values() if isinstance(v, (dict, list)))
                types = item.get("@type")
                types = types if isinstance(types, list) else [types]
                if not any(str(t) in {"Article", "NewsArticle", "BlogPosting"} for t in types):
                    continue
                headline = item.get("headline") or item.get("name")
                published = item.get("datePublished") or item.get("dateCreated")
                if headline and published:
                    match = ISO_DATE.search(str(published))
                    posts.append({
                        "title": str(headline).strip(),
                        "date": match.group(0) if match else None,
                        "url": urljoin(base, str(item.get("url") or "")) if item.get("url") else None,
                    })
        return posts

    def _from_dom(self, html: str, base: str) -> list[dict[str, Any]]:
        tree = HTMLParser(html or "")
        for node in tree.css("script, style, noscript"):
            node.decompose()

        posts: list[dict[str, Any]] = []
        blocks = tree.css("article") or tree.css("li, div.news-item, div.post")
        for block in blocks[:40]:
            text = re.sub(r"\s+", " ", block.text(separator=" ", strip=True) or "")
            if len(text) < 20:
                continue
            date = self._find_date(block, text)
            if not date:
                continue
            heading = block.css_first("h1, h2, h3, h4") or block.css_first("a")
            title = (heading.text(strip=True) if heading else text)[:200].strip()
            link = block.css_first("a[href]")
            href = link.attributes.get("href") if link else None
            posts.append({
                "title": title or text[:120],
                "date": date,
                "url": urljoin(base, href) if href else None,
            })
        return posts

    @staticmethod
    def _find_date(block: Any, text: str) -> str | None:
        time_node = block.css_first("time")
        if time_node:
            stamp = time_node.attributes.get("datetime") or time_node.text(strip=True) or ""
            match = ISO_DATE.search(stamp)
            if match:
                return match.group(0)
        match = ISO_DATE.search(text)
        if match:
            return match.group(0)
        match = NO_DATE.search(text)
        if match:
            day, month, year = match.group(1), NO_MONTHS[match.group(2).casefold()], match.group(3)
            return f"{year}-{month:02d}-{int(day):02d}"
        match = NUMERIC_DATE.search(text)
        if match:
            day, month, year = int(match.group(1)), int(match.group(2)), match.group(3)
            try:
                datetime(int(year), month, day)
            except ValueError:
                return None
            return f"{year}-{month:02d}-{day:02d}"
        return None

    @staticmethod
    def _unresolved(state: Availability, note: str) -> list[Claim]:
        return [
            Claim(field=field, value=None, availability=state, confidence=0.0, note=note)
            for field in ("activity.latest_post_date", "activity.posts")
        ]
