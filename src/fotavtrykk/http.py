"""Budgeted, snapshot-preserving HTTP.

The evaluator counts 2,000 outbound requests per 100-company batch "including
redirects and retries", so redirects and retries are counted here too. Every
response body is hashed and written to a content-addressed snapshot directory
before anything parses it: evidence must exist before extraction.
"""

from __future__ import annotations

import asyncio
import hashlib
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

USER_AGENT = (
    "Fotavtrykk/0.1 (Signalpost research agent; +https://builderr.ai/challenges/signalpost)"
)
DEFAULT_TIMEOUT = 10.0
MAX_BODY_BYTES = 2_000_000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class BudgetExhausted(RuntimeError):
    """Raised instead of silently exceeding the evaluator's request cap."""


@dataclass
class RequestBudget:
    limit: int
    used: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def reserve(self, count: int = 1) -> None:
        if self.used + count > self.limit:
            raise BudgetExhausted(f"request budget {self.limit} exhausted (used {self.used})")
        self.used += count

    def charge_extra(self, count: int) -> None:
        """Redirects are only knowable after the fact; charge them then."""
        self.used += max(0, count)


@dataclass
class CallCounter:
    """Per-company request tally."""

    requests: int = 0


# Companies run concurrently, so a global counter delta would attribute one
# company's requests to another. Each company task carries its own counter.
_CALL_COUNTER: ContextVar[CallCounter | None] = ContextVar("fotavtrykk_calls", default=None)


def start_call_counter() -> CallCounter:
    counter = CallCounter()
    _CALL_COUNTER.set(counter)
    return counter


def _record_calls(count: int) -> None:
    counter = _CALL_COUNTER.get()
    if counter is not None:
        counter.requests += count


@dataclass
class CostLedger:
    limit_usd: float = 10.0
    spent_usd: float = 0.0
    by_provider: dict[str, float] = field(default_factory=dict)

    def charge(self, provider: str, usd: float) -> None:
        self.spent_usd += usd
        self.by_provider[provider] = round(self.by_provider.get(provider, 0.0) + usd, 6)

    def can_afford(self, usd: float) -> bool:
        return self.spent_usd + usd <= self.limit_usd


@dataclass
class FetchResult:
    url: str
    ok: bool
    status: int | None = None
    final_url: str | None = None
    content: bytes = b""
    text: str = ""
    content_sha256: str = ""
    retrieved_at: str = ""
    requests_used: int = 0
    error: str | None = None
    blocked: bool = False

    @property
    def json_ok(self) -> bool:
        return self.ok and bool(self.text.strip())


class Fetcher:
    """One shared client per batch. Per-host concurrency keeps us polite."""

    def __init__(
        self,
        budget: RequestBudget,
        *,
        snapshot_dir: Path | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        per_host_concurrency: int = 2,
        retries: int = 1,
    ) -> None:
        self.budget = budget
        self.snapshot_dir = snapshot_dir
        self.timeout = timeout
        self.retries = retries
        self._per_host_concurrency = per_host_concurrency
        self._host_locks: dict[str, asyncio.Semaphore] = {}
        self._client: httpx.AsyncClient | None = None
        if snapshot_dir:
            snapshot_dir.mkdir(parents=True, exist_ok=True)

    async def __aenter__(self) -> "Fetcher":
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "nb-NO,nb;q=0.9,en;q=0.8"},
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client:
            await self._client.aclose()

    def _host_gate(self, url: str) -> asyncio.Semaphore:
        host = httpx.URL(url).host or ""
        if host not in self._host_locks:
            self._host_locks[host] = asyncio.Semaphore(self._per_host_concurrency)
        return self._host_locks[host]

    def _persist(self, digest: str, body: bytes) -> None:
        if not self.snapshot_dir or not digest:
            return
        target = self.snapshot_dir / f"{digest}.bin"
        if not target.exists():
            target.write_bytes(body)

    async def get(self, url: str, *, accept: str | None = None) -> FetchResult:
        """Fetch one URL. Never raises for HTTP or network errors — returns a
        result carrying the failure, so a company always reaches a terminal state."""
        assert self._client is not None, "Fetcher must be used as an async context manager"
        headers = {"Accept": accept} if accept else None
        spent = 0
        last_error: str | None = None
        last_status: int | None = None

        for attempt in range(self.retries + 1):
            try:
                self.budget.reserve(1)
            except BudgetExhausted as exc:
                return FetchResult(
                    url=url, ok=False, retrieved_at=utc_now(),
                    requests_used=spent, error=str(exc),
                )
            spent += 1
            _record_calls(1)
            try:
                async with self._host_gate(url):
                    response = await self._client.get(url, headers=headers)
                redirects = len(response.history)
                if redirects:
                    self.budget.charge_extra(redirects)
                    _record_calls(redirects)
                    spent += redirects

                body = response.content[:MAX_BODY_BYTES]
                digest = hashlib.sha256(body).hexdigest()
                last_status = response.status_code

                if response.status_code >= 400:
                    last_error = f"HTTP {response.status_code}"
                    # 4xx is a settled answer; only retry transient 5xx / 429.
                    if response.status_code not in (429, 500, 502, 503, 504):
                        return FetchResult(
                            url=url, ok=False, status=response.status_code,
                            final_url=str(response.url), retrieved_at=utc_now(),
                            requests_used=spent, error=last_error,
                            blocked=response.status_code in (401, 403, 451),
                        )
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue

                self._persist(digest, body)
                return FetchResult(
                    url=url, ok=True, status=response.status_code,
                    final_url=str(response.url), content=body,
                    text=body.decode(response.encoding or "utf-8", errors="replace"),
                    content_sha256=digest, retrieved_at=utc_now(), requests_used=spent,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.retries:
                    await asyncio.sleep(0.5 * (attempt + 1))

        return FetchResult(
            url=url, ok=False, status=last_status, retrieved_at=utc_now(),
            requests_used=spent, error=last_error or "unknown fetch failure",
        )
