# Fotavtrykk

Registry-anchored, org-number-proven company intelligence for the
[Signalpost challenge](https://builderr.ai/challenges/signalpost).

Give it a Norwegian organisation number; it returns one terminal envelope with
claims, the evidence behind each one, and an explicit availability state for
everything it could not establish.

**The rule the whole system is built around:** nothing is published about a
company without a deterministic, re-checkable link back to its
organisasjonsnummer. When identity cannot be proven, the answer is `ambiguous`
or `not_available` — never a guess. One wrong-company publication ends
qualification, so recall is always the thing we trade away, never precision.

## Run it

```bash
uv sync
uv run fotavtrykk run \
  --organisations data/smoke-companies.jsonl \
  --output out/envelopes.jsonl \
  --report out/run-report.json \
  --snapshots out/snapshots \
  --run-id local-001 \
  --expected-count 8
```

Input is JSONL with an `organisation_number` field, or one number per line.
Output is one JSON envelope per line, in input order. The process exits non-zero
if the envelope count does not match `--expected-count`.

```bash
uv run pytest -q
```

## Status

V1 covers the official foundation and the identity gate.

| Module | Does | State |
|---|---|---|
| `orgnr.py` | MOD-11 validation, proof search, name tokens | done |
| `http.py` | Budgeted fetch, retries, snapshots, per-company counters | done |
| `registry.py` | Entity, roles, annual accounts, subunits | done |
| `site.py` | Website crawl + org-number proof gate | done |
| `batch.py` | Orchestration, terminal-envelope guarantee | done |
| `diff.py` | Reconcile, typed change events, idempotency | done |
| `snapshots.py` | Snapshot load/save, manifest, content hash | done |
| `connectors/` | Places, NAV jobs, news, YouTube | **not built** |
| `viewer/` | Static evidence browser | **not built** |

External connectors are where the rubric's differentiating points live. The
foundation this V1 delivers is table stakes, not a competitive position.

### Refresh

```bash
# Refresh against a prior run: changes land in each envelope's `changes` list.
uv run fotavtrykk run --organisations data/smoke-companies.jsonl \
  --output out/run2.jsonl --previous out/run1.jsonl --run-id local-002

# Diff two stored snapshots offline. Zero outbound requests.
uv run fotavtrykk refresh --previous out/run1.jsonl --current out/run2.jsonl \
  --output out/changes.jsonl --report out/refresh-report.json
```

Two independent live runs over the same companies produced **0 changes and 0
material changes** — real-world idempotency, not just a fixture replay. Diffing
a snapshot against itself reports `idempotent_rerun: true`, `false_changes: 0`,
`evidence_complete: true`.

Change detection was verified by injecting realistic movements into a live
snapshot. All seven were caught with the right type and zero false positives:

| Injected | Reported |
|---|---|
| CEO replaced | `departed_role` + `new_role` (two events, not one blob) |
| Revenue moved | `new_filing`, material |
| New subunit | `new_location`, material |
| Legal name changed | `changed_identity`, material |
| Website unreachable | `source_unavailable`, minor, **previous value preserved** |
| Currency NOK → EUR at the same number | `new_filing` + `value unchanged; currency NOK -> EUR` |

Change types are typed (`new_role`, `new_filing`, `new_location`,
`changed_description`, `became_available`, `became_unavailable`,
`source_unavailable`) rather than a flat old/new pair, because the evaluator
checks that a rerun reports *real* changes, not merely that bytes differ.

### Measured on a live 8-company batch

8/8 terminal envelopes · 38 requests (4.75/company) · 5.5s wall · p95 5,526ms ·
$0.00. Extrapolated to 100 companies: ~475 requests, roughly 24% of the 2,000
cap, comfortably inside the 45-minute wall and the 10,000ms p95 latency gate.

## How identity is decided

`site.py` publishes a domain only on one of three proofs, strongest first:

1. `org_number_labelled_on_page` — the organisation number appears next to an
   org-number label (`Org.nr`, `Organisasjonsnummer`, `MVA`). Confidence 1.0.
2. `org_number_on_page` — a MOD-11-valid matching number appears anywhere on
   the page. Confidence 0.97.
3. `registry_declared_website` — the registry declares this site *and* the
   complete legal-name token set appears in the homepage identity evidence.
   Confidence 0.80.

Anything weaker is `ambiguous`. Social handles are quarantined unless the site
itself passed the gate.

We deliberately do **not** use the "strip every non-digit from the page and
substring-match" approach. Concatenating unrelated numbers manufactures false
positives — `test_concatenated_digits_do_not_manufacture_a_match` pins this.

Verified live: `1912 NÆRING 1 AS` declares `ragde.no`, a property group's site.
The gate returns `ambiguous` rather than attributing the group's content to the
subsidiary.

## Invariants the tests enforce

- Exactly one terminal envelope per input, including on crash or exhausted budget.
- Only the six contract availability states exist; a seventh is rejected.
- Every envelope carries the same claim key set, so a field never appears or
  vanishes between runs and per-field coverage never depends on availability.
- A missing value is never a zero. A **filed** zero is published and marked
  `filed_zero: true` so an auditor can tell the two apart.
- Currency, statement type (`SELSKAP` vs `KONSERN`) and reporting period travel
  with every financial figure. Equinor files in USD; assuming NOK would be a
  fabricated value.
- Resigned officers (`avregistrert: true`) are not current leadership.
- Personal birth dates are never published.
- Observations are validated against the starter kit's own publication gate
  before emission, so a rejected observation is caught locally rather than by
  the evaluator's audit.
- Re-running the same snapshot is silent. List ordering, `0` vs `0.0`, and
  bookkeeping qualifiers are normalised away before comparison — each is a
  false-change source pinned by a test.
- A failed or blocked source preserves the last supported value **and its
  evidence**, and reports `source_unavailable`. Only a source that was read
  successfully and no longer reports a value yields `became_unavailable`.
- Observation timestamps come from the evidence, never the wall clock, so
  replaying a stored snapshot reproduces byte-identical output.

## Sources, rights and cost

| Source | Access | Licence / basis | Cost |
|---|---|---|---|
| Enhetsregisteret (entity, roles, subunits) | Official REST API | NLOD 2.0 | $0 |
| Regnskapsregisteret (annual accounts) | Official REST API | NLOD 2.0 | $0 |
| Company websites | Direct HTTPS, 10s timeout, 1 retry, ≤2MB/page, 2 concurrent per host | Permitted public page | $0 |

No LLM or third-party paid API is used in V1. Declared third-party spend per
100-company batch: **$0.00**.

LinkedIn, Meta, Glassdoor and Indeed are never fetched. Where a verified company
site links to such a profile, the handle is recorded as declared by the company
with the company's own page as the captured evidence; the destination is not
retrieved.

### Encoding note for review

For a social handle discovered on a verified company site, `source_url` and
`content_sha256` refer to the **company page we actually captured**, while
`platform` names the destination and the handle URL rides in
`metrics.declared_url`. This keeps the hash consistent with the URL it
describes. Worth confirming with the organiser that this is the intended
encoding for declared-but-unfetched handles.

## Next

1. Google Places, NAV job feed, news — the external families that carry the
   differentiating points.
2. Hand-label ≥100 observations; the external audit gate depends on it and it is
   the critical path, since all external points are gated on passing it.
3. Static evidence viewer.
