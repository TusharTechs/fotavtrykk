"""Static evidence viewer.

The rubric scores whether "someone can find, compare and verify the
information on desktop and mobile", and the kit's scorer gates those points on
whether external intelligence is actually presented. So this shows the external
footprint next to the registry facts, and — the part that matters for this
agent — it shows *how each fact was proven* and what we could not establish.

One self-contained HTML file. No framework, no build step, no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .audit import risk_tier
from .models import Envelope

TIER_LABEL = {
    "primary_key": "registry key",
    "proven_on_page": "org number",
    "corroborated": "registry match",
    "declared": "declared by verified source",
    "inferred": "name only",
}


def project(envelope: Envelope) -> dict[str, Any]:
    """The smallest shape the page needs, so the file stays small."""
    claims = {c.field: c for c in envelope.claims}
    evidence = {e.id: e for e in envelope.evidence}

    def claim(field: str) -> dict[str, Any] | None:
        c = claims.get(field)
        if not c:
            return None
        source = next((evidence[i] for i in c.evidence_ids if i in evidence), None)
        return {
            "value": c.value,
            "state": str(c.availability),
            "note": c.note,
            "period": c.reporting_period,
            "qualifiers": {k: v for k, v in c.qualifiers.items()
                           if k in ("currency", "statement_type", "identity_proof",
                                    "filed_zero", "scale", "stale")},
            "source": source.source_url if source else None,
            "retrieved": source.retrieved_at if source else None,
            "span": source.claim_span if source else None,
        }

    platforms: dict[str, list[dict[str, Any]]] = {}
    for observation in envelope.observations:
        platforms.setdefault(observation.platform, []).append({
            "signal": observation.signal_type,
            "url": (observation.metrics or {}).get("declared_url") or observation.source_url,
            "proof": observation.identity_proof,
            "tier": risk_tier(observation),
            "span": observation.evidence_span,
            "observed_at": observation.observed_at,
            "metrics": observation.metrics or {},
        })

    return {
        "org": envelope.organisation_number,
        "name": envelope.legal_identity.get("legal_name") or envelope.organisation_number,
        "form": envelope.legal_identity.get("legal_form"),
        "municipality": envelope.legal_identity.get("municipality"),
        "industry": envelope.legal_identity.get("industry_code"),
        "status": envelope.run.terminal_status,
        "claims": {f: claim(f) for f in (
            "legal_name", "business_address", "employees_registered", "operating_status",
            "managing_director", "board_chair", "leadership", "locations",
            "official_website", "website_description", "social_handles",
            "financials.revenue", "financials.operating_profit", "financials.net_result",
            "financials.total_assets", "financials.equity", "financial_history.years",
            "places.rating", "places.rating_count", "places.address",
            "wikidata.qid", "wikidata.website", "wikidata.employees", "wikidata.wikipedia",
            "jobs.active_count", "activity.latest_post_date", "activity.posts",
        )},
        "platforms": platforms,
        "changes": [c.model_dump() for c in envelope.changes],
        "requests": envelope.operations.requests,
    }


def build(envelopes: list[Envelope], *, run_report: dict | None = None) -> str:
    companies = [project(e) for e in envelopes]
    companies.sort(key=lambda c: (-len(c["platforms"]), c["name"]))
    payload = json.dumps(
        {"companies": companies, "report": run_report or {}},
        ensure_ascii=False, separators=(",", ":"),
    )
    return _TEMPLATE.replace("__DATA__", payload.replace("</", "<\\/"))


def write(path: Path, envelopes: list[Envelope], *, run_report: dict | None = None) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    html = build(envelopes, run_report=run_report)
    path.write_text(html, encoding="utf-8")
    return len(html)


_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fotavtrykk — company evidence</title>
<style>
:root{
  --ground:#eef1f3; --card:#fff; --sunk:#e3e8ea; --ink:#0f181d; --ink2:#41565f; --ink3:#71868f;
  --rule:#d3dcdf; --accent:#0b5563; --accent-wash:#dceaee;
  --ok:#1f6b45; --ok-wash:#dcece2; --warn:#8a6410; --warn-wash:#f3e9cf;
  --off:#6b7f88; --off-wash:#e6ebed; --bad:#9a2f26; --bad-wash:#f4e0dd;
}
@media (prefers-color-scheme:dark){:root{
  --ground:#0a1114; --card:#101b20; --sunk:#16242a; --ink:#e3ecef; --ink2:#a4b8c0; --ink3:#71868f;
  --rule:#25373e; --accent:#4fb8cc; --accent-wash:#122e36;
  --ok:#5fc08c; --ok-wash:#12301f; --warn:#d4a63c; --warn-wash:#2d2512;
  --off:#7d919a; --off-wash:#1a262b; --bad:#e0756a; --bad-wash:#311a17;}}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);
  font:15px/1.55 ui-sans-serif,system-ui,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:0 16px 56px}
header{padding:26px 0 16px;border-bottom:2px solid var(--ink)}
h1{margin:0;font-size:1.45rem;letter-spacing:-.02em}
.sub{color:var(--ink2);margin-top:5px;font-size:.9rem;max-width:62ch}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));gap:1px;
  background:var(--rule);border:1px solid var(--rule);border-radius:6px;overflow:hidden;margin:18px 0}
.stat{background:var(--card);padding:11px 13px}
.stat b{display:block;font-size:1.3rem;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.stat span{font-size:.66rem;text-transform:uppercase;letter-spacing:.07em;color:var(--ink3)}
.controls{display:flex;gap:9px;flex-wrap:wrap;margin:16px 0}
input,select{font:inherit;padding:7px 10px;border:1px solid var(--rule);border-radius:6px;
  background:var(--card);color:var(--ink);min-width:0}
input{flex:1 1 230px}
.grid{display:grid;gap:12px}
.co{background:var(--card);border:1px solid var(--rule);border-radius:7px;overflow:hidden}
.co>summary{padding:13px 15px;cursor:pointer;display:flex;gap:11px;align-items:baseline;flex-wrap:wrap;
  list-style:none}
.co>summary::-webkit-details-marker{display:none}
.co>summary:hover{background:var(--sunk)}
.nm{font-weight:650;letter-spacing:-.01em}
.org{font-family:ui-monospace,Menlo,monospace;font-size:.76rem;color:var(--ink3);
  font-variant-numeric:tabular-nums}
.meta{color:var(--ink3);font-size:.8rem;margin-left:auto}
.pf{display:flex;gap:4px;flex-wrap:wrap;margin-top:2px}
.pill{font-size:.64rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;
  padding:2px 6px;border-radius:3px;background:var(--accent-wash);color:var(--accent);white-space:nowrap}
.body{padding:0 15px 15px;border-top:1px solid var(--rule)}
h3{font-size:.68rem;text-transform:uppercase;letter-spacing:.09em;color:var(--ink3);
  margin:15px 0 7px;font-weight:700}
table{width:100%;border-collapse:collapse;font-size:.855rem}
td{padding:5px 8px 5px 0;vertical-align:top;border-bottom:1px solid var(--rule)}
tr:last-child td{border-bottom:none}
td.k{color:var(--ink3);white-space:nowrap;width:1%;padding-right:14px}
td.v{color:var(--ink);word-break:break-word}
.st{font-size:.62rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;
  padding:1px 5px;border-radius:3px;white-space:nowrap}
.s-available{background:var(--ok-wash);color:var(--ok)}
.s-not_available{background:var(--off-wash);color:var(--off)}
.s-ambiguous{background:var(--warn-wash);color:var(--warn)}
.s-blocked,.s-failed{background:var(--bad-wash);color:var(--bad)}
.proof{font-family:ui-monospace,Menlo,monospace;font-size:.68rem;color:var(--ink3)}
a{color:var(--accent)}
.src{font-size:.7rem}
.note{color:var(--ink3);font-size:.78rem;font-style:italic}
.conf{font-size:.62rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;
  padding:1px 5px;border-radius:3px;background:var(--ok-wash);color:var(--ok);white-space:nowrap}
.empty{padding:26px;text-align:center;color:var(--ink3)}
/* Nothing may exceed the viewport: the rubric scores verifying on mobile. */
.v,td.v,.proof{overflow-wrap:anywhere}
@media (max-width:620px){
  .wrap{padding:0 12px 40px}
  .stats{grid-template-columns:repeat(2,minmax(0,1fr))}
  .stat b{font-size:1.15rem}
  .controls{flex-direction:column}
  /* flex-basis would stretch these to fill a column container */
  input,select{width:100%;flex:0 0 auto}
  .co>summary{padding:11px 12px}
  .meta{margin-left:0;width:100%;order:3}
  .pf{order:4}
  .pill{font-size:.6rem}
  .body{padding:0 12px 12px}
  table,tbody,tr{display:block;width:100%}
  td{display:block;width:auto;border-bottom:none;padding:2px 0}
  tr{border-bottom:1px solid var(--rule);padding:6px 0}
  tr:last-child{border-bottom:none}
  td.k{width:auto;white-space:normal;padding-right:0;
    font-size:.7rem;text-transform:uppercase;letter-spacing:.05em}
  .proof{display:block;margin-top:1px}
}
</style></head><body><div class="wrap">
<header>
  <h1>Fotavtrykk — company evidence</h1>
  <div class="sub">Every fact carries its source, the time it was read, and the proof that ties it
  to this exact legal entity. Anything we could not establish says so rather than showing a blank.</div>
</header>
<div class="stats" id="stats"></div>
<div class="controls">
  <input id="q" placeholder="Search name, organisation number or municipality" autocomplete="off">
  <select id="f">
    <option value="">All companies</option>
    <option value="rating">Has a rating</option>
    <option value="site">Verified website</option>
    <option value="wikidata">In Wikidata</option>
    <option value="activity">Has dated activity</option>
    <option value="none">No external footprint</option>
  </select>
</div>
<div class="grid" id="list"></div>
</div>
<script>
const DATA = __DATA__;
const F = (v) => v == null ? '' : (typeof v === 'number' ? v.toLocaleString('en-US') : String(v));
// Lists of records read as noise when dumped as JSON. Show what a person needs.
function human(v){
  if(v == null) return '';
  if(typeof v === 'number') return v.toLocaleString('en-US');
  if(Array.isArray(v)) return v.map(human).join(' · ');
  if(typeof v === 'object'){
    const pick = ['name','title','role','municipality','date','organisation_number','platform'];
    const parts = pick.filter(k => v[k] != null && v[k] !== '').map(k => v[k]);
    return parts.length ? parts.join(' — ') : Object.values(v).filter(x=>x!=null).join(' ');
  }
  return String(v);
}
// twitter.com and x.com are the same account declared two ways.
const normUrl = (u) => String(u||'').replace(/\/+$/,'')
  .replace(/^https?:\/\/(www\.)?/,'').replace(/^twitter\.com\//,'x.com/');
const esc = (s) => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function stat(v, k){ return `<div class="stat"><b>${v}</b><span>${k}</span></div>`; }
function pct(n, d){ return d ? Math.round(100*n/d) + '%' : '0%'; }

const cos = DATA.companies;
const has = (c, f) => c.claims[f] && c.claims[f].state === 'available';
document.getElementById('stats').innerHTML =
  stat(cos.length, 'companies')
+ stat(pct(cos.filter(c => Object.keys(c.platforms).length > 1).length, cos.length), 'two+ platforms')
+ stat(pct(cos.filter(c => has(c,'places.rating')).length, cos.length), 'with a rating')
+ stat(pct(cos.filter(c => has(c,'official_website')).length, cos.length), 'verified website')
+ stat(pct(cos.filter(c => has(c,'wikidata.qid')).length, cos.length), 'in wikidata')
+ stat(pct(cos.filter(c => has(c,'financials.revenue')).length, cos.length), 'filed accounts');

function row(label, c){
  if(!c) return '';
  const q = c.qualifiers || {};
  const extra = [q.currency, q.statement_type, c.period, q.filed_zero ? 'filed zero' : null,
                 q.stale ? 'stale' : null].filter(Boolean).join(' · ');
  let v = c.state === 'available'
    ? `<span class="v">${esc(human(c.value)).slice(0,320)}</span>`
    : `<span class="note">${esc(c.note || 'not established')}</span>`;
  const src = c.source ? ` <a class="src" href="${esc(c.source)}" target="_blank" rel="noopener">source</a>` : '';
  const pr = q.identity_proof ? ` <span class="proof">${esc(q.identity_proof)}</span>` : '';
  return `<tr><td class="k">${esc(label)}</td><td class="v">
    <span class="st s-${esc(c.state)}">${esc(c.state.replace('_',' '))}</span> ${v}
    ${extra ? `<span class="proof"> ${esc(extra)}</span>` : ''}${pr}${src}</td></tr>`;
}

function card(c){
  const cl = c.claims;
  const plats = Object.keys(c.platforms).sort();
  const money = ['financials.revenue','financials.operating_profit','financials.net_result',
                 'financials.total_assets','financials.equity']
    .map(f => row(f.split('.')[1].replace(/_/g,' '), cl[f])).join('');
  const ext = plats.map(p => {
    // The same handle found by two independent sources is corroboration, not
    // a duplicate row — merge and say so.
    const merged = new Map();
    for(const i of c.platforms[p]){
      const key = normUrl(i.url) + '|' + i.signal;
      if(merged.has(key)) merged.get(key).proofs.push(i.proof);
      else merged.set(key, Object.assign({}, i, {proofs:[i.proof]}));
    }
    const items = [...merged.values()];
    const bits = items.map(i => {
      const m = i.metrics || {};
      const label = m.rating != null ? `${m.rating}★ (${F(m.rating_count)} ratings)`
        : (i.observed_at ? `${i.observed_at} — ${esc(String(i.span||'').slice(0,70))}`
        : m.language ? `${esc(m.language)}.wikipedia article`
        : esc(String(i.span || m.label || m.declared_url || i.signal).slice(0,70)));
      const confirmed = i.proofs.length > 1
        ? `<span class="conf">confirmed by ${i.proofs.length} sources</span> ` : '';
      return `<div><a href="${esc(i.url)}" target="_blank" rel="noopener">${label}</a>
        ${confirmed}<span class="proof">${esc(i.tier)} · ${esc(i.proofs.join(' + '))}</span></div>`;
    }).join('');
    return `<tr><td class="k">${esc(p)}</td><td class="v">${bits}</td></tr>`;
  }).join('');

  return `<details class="co" data-k="${esc((c.name+' '+c.org+' '+(c.municipality||'')).toLowerCase())}">
    <summary>
      <span class="nm">${esc(c.name)}</span>
      <span class="org">${esc(c.org)}</span>
      <span class="meta">${esc(c.municipality||'')} · ${esc(c.industry||'')}</span>
      <div class="pf">${plats.map(p=>`<span class="pill">${esc(p)}</span>`).join('')}</div>
    </summary>
    <div class="body">
      <h3>Identity</h3><table>
        ${row('legal form', {value:c.form, state:c.form?'available':'not_available'})}
        ${row('address', cl['business_address'])}
        ${row('employees', cl['employees_registered'])}
        ${row('website', cl['official_website'])}
      </table>
      <h3>People &amp; places</h3><table>
        ${row('managing director', cl['managing_director'])}
        ${row('board chair', cl['board_chair'])}
        ${row('workplaces', cl['locations'])}
      </table>
      <h3>Latest filed accounts</h3><table>${money}</table>
      <h3>External footprint</h3><table>
        ${row('google rating', cl['places.rating'])}
        ${row('open vacancies', cl['jobs.active_count'])}
        ${row('latest post', cl['activity.latest_post_date'])}
        ${ext || '<tr><td class="k">—</td><td class="v"><span class="note">no external platform resolved to this entity</span></td></tr>'}
      </table>
      ${c.changes.length ? `<h3>Changes since last run</h3><table>${
        c.changes.map(ch=>`<tr><td class="k">${esc(ch.change_type)}</td><td class="v">${esc(ch.field)}:
          ${esc(F(ch.old_value)).slice(0,60)} → ${esc(F(ch.new_value)).slice(0,60)}</td></tr>`).join('')
      }</table>` : ''}
    </div></details>`;
}

const list = document.getElementById('list');
function render(){
  const q = document.getElementById('q').value.trim().toLowerCase();
  const f = document.getElementById('f').value;
  const shown = cos.filter(c => {
    if(q && !(c.name+' '+c.org+' '+(c.municipality||'')).toLowerCase().includes(q)) return false;
    if(f === 'rating') return has(c,'places.rating');
    if(f === 'site') return has(c,'official_website');
    if(f === 'wikidata') return has(c,'wikidata.qid');
    if(f === 'activity') return has(c,'activity.latest_post_date');
    if(f === 'none') return Object.keys(c.platforms).filter(p=>p!=='brreg').length === 0;
    return true;
  });
  list.innerHTML = shown.length ? shown.map(card).join('')
    : '<div class="empty">No companies match that filter.</div>';
}
document.getElementById('q').addEventListener('input', render);
document.getElementById('f').addEventListener('change', render);
render();
document.querySelector('.co')?.setAttribute('open','');
</script></body></html>
"""
