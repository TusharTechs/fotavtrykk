"""Company selection from the frozen 411,160-company universe.

Two different jobs, deliberately separated:

  * `select_representative` mirrors the universe, for measuring what a real
    evaluation batch will look like.
  * `select_risk_weighted` deliberately over-samples companies that have a
    website, because those are the only ones that generate the observations an
    entity-precision audit can actually falsify. Only 10.9% of the universe has
    a site, so a representative sample would be ~89% registry rows and would
    audit nothing risky.

Selection is deterministic given a seed: the same seed always yields the same
companies, so a development and a validation split never drift.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Iterator


def load_universe(path: Path) -> Iterator[dict]:
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def employee_band(value: object) -> str:
    if value is None:
        return "missing"
    try:
        count = int(value)
    except (TypeError, ValueError):
        return "missing"
    if count < 5:
        return "1-4"
    if count < 20:
        return "5-19"
    if count < 100:
        return "20-99"
    return "100+"


def stratum_key(row: dict) -> str:
    """Legal form, size, industry and web presence — the axes that change how
    hard a company is to research."""
    return "|".join((
        str(row.get("legal_form") or "??"),
        employee_band(row.get("employees")),
        (str(row.get("industry_code") or "??"))[:2],
        "web" if row.get("website") else "no-web",
    ))


def _rank(seed: str, org: str) -> str:
    """Stable pseudo-random ordering. Deterministic across machines and runs."""
    return hashlib.sha256(f"{seed}:{org}".encode()).hexdigest()


def _round_robin(buckets: dict[str, list[dict]], count: int) -> list[dict]:
    """Take one per stratum per pass, so small strata are not crowded out."""
    selected: list[dict] = []
    order = sorted(buckets)
    depth = 0
    while len(selected) < count:
        added = False
        for key in order:
            if depth < len(buckets[key]):
                selected.append(buckets[key][depth])
                added = True
                if len(selected) == count:
                    return selected
        if not added:
            break
        depth += 1
    return selected


def _bucket(rows: Iterable[dict], seed: str, exclude: set[str]) -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        org = str(row.get("organisation_number") or "")
        if not org or org in exclude:
            continue
        buckets[stratum_key(row)].append(row)
    for key in buckets:
        buckets[key].sort(key=lambda r: _rank(seed, str(r["organisation_number"])))
    return buckets


def select_representative(
    rows: Iterable[dict], count: int, *, seed: str, exclude: Iterable[str] = ()
) -> list[dict]:
    """A stratified sample that mirrors the universe's own shape."""
    buckets = _bucket(rows, seed, set(exclude))
    return _round_robin(buckets, count)


def select_risk_weighted(
    rows: Iterable[dict], count: int, *, seed: str,
    website_fraction: float = 0.6, exclude: Iterable[str] = (),
) -> list[dict]:
    """Over-sample companies with a website so the audit has something to falsify.

    The resulting precision estimate is a conservative lower bound: it is
    measured on a harder-than-average population, so the true population
    precision should be at least this good, never worse.
    """
    excluded = set(exclude)
    with_site: list[dict] = []
    without_site: list[dict] = []
    for row in rows:
        org = str(row.get("organisation_number") or "")
        if not org or org in excluded:
            continue
        (with_site if row.get("website") else without_site).append(row)

    want_web = min(len(with_site), int(round(count * website_fraction)))
    want_rest = count - want_web
    chosen = _round_robin(_bucket(with_site, seed, set()), want_web)
    chosen += _round_robin(_bucket(without_site, seed, set()), want_rest)
    return sorted(chosen, key=lambda r: _rank(seed, str(r["organisation_number"])))


def split(rows: list[dict], sizes: dict[str, int]) -> dict[str, list[dict]]:
    """Carve a selection into named, zero-overlap splits."""
    out: dict[str, list[dict]] = {}
    cursor = 0
    for name, size in sizes.items():
        out[name] = rows[cursor : cursor + size]
        cursor += size
    return out


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
