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

## How to read the coverage numbers below

Coverage is scored as recall **against Builderr's checked collection**, not
against the universe: 70% company recall (of the companies that have a verified
fact of this type, how many did you cover) plus 30% fact recall (of the
individual facts, how many did you find).

The percentages in the next table are **population reach** — the share of *all*
companies where we found the fact. That is a different, harsher number, and it
is the one we can measure locally because the checked collection is not visible
to us. A family where few companies have the fact at all will show a tiny
population reach and could still score well on recall, or be reported as *not
measured* if the collection holds no verified positive either.

Read them as a floor on effort, not as an estimate of the score.

## Coverage that is structurally capped

| family | weight | representative coverage | why |
|---|---:|---:|---|
| `workforce_jobs` | 7 pts | **0.0%** | Norway has ~13,500 active adverts against 411,160 companies. Investigated properly: 8 of our 1,000 match an advertiser by name, but every one of those adverts had already expired, and NAV masks `employer` on an inactive advert — so there is no organisation number to verify against and the gate correctly refuses to publish. 0 is the true answer for this sample, not a miss. |
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

**Corroboration is not proof, and one form of it failed.** Spot-checking the
Google Places proofs found `places_address_matches_registry` matching on
postcode and town only — which in Norway covers a whole village or city
district, and structurally conflates a landlord with its tenants. Real results:
HØYRES STORTINGSGRUPPE matched the parliament *building* (4.4 from 616
ratings); HVAMSVINGEN 4 ANS, a property partnership named after its address,
matched whatever business occupies it; GELATO ASA matched a place in a
multi-tenant office tower. About 6 of 11 sampled were wrong — ~55% against a 95%
floor — so that proof was removed. Tightening it to the street would not help:
a landlord and its tenant share a street address by definition.

Places now publishes only on a **verified domain** or a **registry switchboard
match**, both of which belong to an entity rather than to a building.

`registry_site_corroborated` on company websites survives — the postcode+town
there is corroboration *on top of* a full legal-name match on a site the
registry itself declares, not a match on its own. It is still not human-audited.

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

**180 labels, 0 wrong-entity publications, `entity_precision: 1.0`** — but read
the provenance before reading the number.

| provenance | labels | what it covers |
|---|---:|---|
| `machine` | 100 | primary-key registry lookups, re-read from the stored payload |
| `assisted` | 80 | risky roots adjudicated from evidence, and their cascades |
| `human` | **0** | — |

Both risky root classes were checked one by one:

- **21 website `corroborated` roots** — every one resolved to the company's own
  domain (Norske Skog, Scana, TOMRA, Yara, Borregaard, Polaris Media, Navamedic
  …). 14 were confirmed by the registry switchboard appearing on the page, 7 by
  postcode and town. Unlike the withdrawn Places proof, that address check sits
  *on top of* a registry-declared website and a full legal-name match, so it is
  corroboration rather than the sole signal.
- **42 Wikidata roots** — all reconciled. The single label mismatch,
  `VERDIPAPIRSENTRALEN ASA` against Wikidata's `Euronext VPS`, resolved as a
  rebrand: Brreg's own record lists `hjemmeside: www.euronext.com` for that
  organisation number. P2333 matching survived a rename that name matching would
  have missed.

These 63 roots cascade to the `declared` tier, which is why 38 dependents were
settled by them.

**`qualification_passed` stays `false`, and should.** Assisted labels are
adjudication by something other than the gate that produced the observation —
real signal, not certification. Only a person's verdict counts toward the gate,
and 0 rows have one. `provisional_qualification: true` is what the kit's
evaluator would conclude from these labels; it is deliberately the weaker claim.

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
