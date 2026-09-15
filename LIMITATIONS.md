# Limitations

Everything here is measured, not estimated. Where a number comes from a
particular corpus, the corpus is named, because the two we use give very
different answers and only one of them is representative.

## Two corpora, two very different pictures

| | audit corpus | submission artifact |
|---|---|---|
| companies | 150 | 1,000 |
| with a website | 60% (deliberate) | 10.5% |
| purpose | measuring **precision** | measuring **coverage** |

The audit corpus is risk-weighted on purpose: only 10.9% of the universe has a
website, so a representative sample would be ~89% registry-only rows and would
audit nothing falsifiable. That makes it the right population for precision and
**the wrong one for quoting coverage**. Coverage numbers below come from the
representative 1,000 unless stated.

## Coverage that is structurally capped

| family | weight | representative coverage | why |
|---|---:|---:|---|
| `workforce_jobs` | 7 pts | **0.0%** | Norway has ~13,500 active job adverts against 411,160 companies in the universe. The absolute ceiling is ~3.3% and realistically 1–2%. Measured: 32 name candidates across 150 companies over a 75-day window, 10 fetched, 0 verified. |
| `buzz_engagement` | 7 pts | **0.6%** | Only reachable through a company's own dated news page, which requires a verified website — and 89.5% of the universe has none. |
| `ratings_reviews` | 8 pts | **38.6%** | Requires a Google Places listing tied to the entity by an independent registry fact. Dormant holding and property companies have no premises and no listing. |
| `two_platforms` | 10 pts | **39.3%** | Same ceiling: most of the universe has no external footprint of any kind. |

607 of the 1,000 profiles say plainly that no permitted external source could be
tied to the entity. That is the honest state of the universe, not a defect in
the agent — but it does cap what any agent can score on coverage.

## Not built

**Sentiment (10 points) has no source.** Company-owned copy is barred by the
source policy as an independent sentiment claim, which rules out the only text
we currently hold. The gate also needs ≥10 sentiment items across ≥2 independent
hosts per company before it emits anything. Reaching it needs licensed news, and
publishing weak sentiment risks the accuracy gates that protect every other
external point — so the agent abstains entirely rather than guessing.

**External news mentions.** See *Sources we cannot use*.

**A learned source router.** The learning harness explicitly defers this
("use a decision table rather than reinforcement learning"), and the request
budget is only ~52% used, so there is nothing for it to optimise yet.

## Sources we cannot use

- **Google News RSS.** `news.google.com/robots.txt` is `Disallow: /` with an
  allow-list that excludes `/rss/`, and names `ClaudeBot` and `anthropic-ai`
  directly. Its article links are Google redirects needing a second disallowed
  fetch. It works and returns excellent Norwegian results; it is still barred.
- **`query.wikidata.org/sparql`.** `Disallow: /sparql`. The MediaWiki API is
  used instead, which is the supported programmatic interface.
- **NAV `/stillinger/api/search`.** Undocumented internal endpoint that returns
  429 after a handful of calls and stays blocked for a long window. The official
  `pam-stilling-feed` API is used instead.
- **LinkedIn, Meta, Glassdoor, Indeed.** Never fetched. Where a verified company
  site or a Wikidata statement declares a handle, the handle is recorded with
  that declaration as the evidence; the destination is not retrieved.
- **GDELT.** Free and documented but throttled to one request per five seconds —
  ~500s of a 45-minute batch for 100 companies, with poor yield for small firms.

## Known risks in what we do publish

**The `declared` tier cascades.** 227 of 574 observations inherit their identity
from a proven root — a handle on a verified site, a Wikidata statement, a
Wikipedia sitelink. Identity is exact at the root, but a wrong root takes its
dependents with it. This is not hypothetical: two wrong sites previously
produced eight wrong observations. The audit sampler weights this tier most
heavily for that reason.

**Wikidata statements are community-edited.** A P2333 match is an
organisation-number match, so entity resolution is exact — but the social
handles and sitelinks hanging off an entity are only as good as the editor who
added them. They are published as `declared`, never as proven.

**Corroboration is not proof.** `registry_site_corroborated` means a registry
postcode+town or switchboard number appears on the page. A shared office
building or a switchboard listed for a group could in principle corroborate the
wrong entity. Measured at 56% of registry-declared sites; not yet audited by a
human.

**TLS verification is relaxed on a documented fallback.** Some Norwegian sites
serve an incomplete certificate chain; a browser recovers the missing
intermediate via AIA, Python does not. Two of 56 sites failed verification
*consistently* while serving valid content. Those are retried once without
verification and the evidence records `tls_verified: false`. A chain problem
says nothing about which company owns a page and the organisation-number proof
still has to pass — but this is a deliberate relaxation and it is visible in the
output. Disable with `Fetcher(insecure_tls_fallback=False)`.

**Deep pages carry subsidiary organisation numbers.** A corporate site
legitimately shows a subsidiary's number on `/mining/contact-us` or similar.
The negative-evidence gate therefore runs on the **homepage only**; running it
on deep pages would have rejected `norskeskog.com` and `tomra.com`, which are
correct.

## Verification status

**The blind-label audit is incomplete.** 100 machine-verified labels exist,
covering only primary-key registry lookups, which are tautological by
construction. **No risky observation has been adjudicated by a person**, so the
scorer reports `qualification_passed: false`. Its `entity_precision: 1.0` is
therefore a statement about registry lookups and nothing else.

Even when the queue is complete, ~100 clean labels bound the error rate at
roughly 3% with 95% confidence, not 0.5%. Demonstrating a 99.5% floor
statistically would need ~600 labels. Treat a clean audit as "no errors found in
the riskiest sample", never as proof of the precision floor.

## Operational limits

- **Request budget**: ~6.9 per company. A 100-company batch uses ~52% of the
  2,000 cap. The 1,000-profile artifact needed `--request-budget 12000`; the
  defaults are sized for the daily batch and will exhaust mid-run at scale.
- **Latency**: p95 6,644ms against a 10,000ms gate. The tail is company websites
  at per-host concurrency 2, which is deliberate politeness toward small servers.
- **Cost**: $3.50 per 100-company batch, entirely Google Places Text Search
  Enterprise at $0.035/search. ~$35 for a 1,000-profile artifact. Text Search has
  no monthly free allowance.
- **Single-region**: everything runs from one host. Sites that geo-block or
  rate-limit by IP will fail for the whole batch rather than degrade.

## Licences and rights

| Source | Basis | Cost |
|---|---|---|
| Enhetsregisteret, Regnskapsregisteret | Official API, NLOD 2.0 | $0 |
| NAV `pam-stilling-feed` | Official API, public token; stable token free on request | $0 |
| Wikidata MediaWiki API | CC0 content, supported API | $0 |
| Company websites | Direct HTTPS, robots respected, ≤2MB/page, 2 concurrent per host | $0 |
| Google Places (New) | Official API, Text Search Enterprise | $0.035/search |

No LLM or model inference is used anywhere in the pipeline. The summary layer is
a deterministic template over published claims, so no model can introduce an
unsupported claim and an unchanged snapshot always produces identical output.
