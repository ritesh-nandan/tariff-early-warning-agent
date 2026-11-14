"""Step 8 - dashboard: one self-contained HTML file generated from the database.

All data is embedded as JSON; there are no external scripts, fonts or network calls.
The CSV upload is analysed in the browser by a JavaScript port of the deterministic
matcher and duty math (no LLM review, base duties only for HTS codes already in the
database). For the full pipeline on a new importer run:  python run.py --importer file.csv
"""
from __future__ import annotations

import json

from . import concordance, config, matcher

TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tariff Early-Warning Dashboard</title>
<style>
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;
--border:rgba(11,11,11,.10);--b1:#86b6ef;--b2:#2a78d6;--b3:#104281;--good:#0ca30c;--warning:#fab219;--serious:#ec835a;--critical:#d03b3b;--link:#1c5cab;--wash:rgba(11,11,11,.04)}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;
--border:rgba(255,255,255,.10);--b1:#184f95;--b2:#3987e5;--b3:#9ec5f4;--link:#86b6ef;--wash:rgba(255,255,255,.05)}}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1180px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:22px;margin:0 0 2px}h2{font-size:16px;margin:0 0 10px}h3{font-size:14px;margin:0 0 4px}
a{color:var(--link)}small,.muted{color:var(--ink2)}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px;margin-top:16px}
.banner{border-left:4px solid var(--warning);padding:8px 12px;background:var(--wash);border-radius:4px;margin-top:10px}
.banner.adv{border-left-color:var(--axis)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-top:16px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px}
.tile .v{font-size:26px;font-weight:650;line-height:1.1}.tile .l{color:var(--ink2);font-size:12.5px;margin-top:4px}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--grid);vertical-align:top}
th{font-size:12px;color:var(--ink2);font-weight:600;white-space:nowrap}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
tr.detail td{background:var(--wash);padding:10px 14px}
.chip{display:inline-block;padding:1px 8px;border-radius:999px;border:1px solid var(--border);font-size:12px;white-space:nowrap}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px;vertical-align:baseline}
.bars{min-width:150px}.bar{height:6px;border-radius:0 4px 4px 0;margin:2px 0;min-width:2px}
.legend{display:flex;gap:14px;flex-wrap:wrap;color:var(--ink2);font-size:12.5px;margin-bottom:8px}
.sw{display:inline-block;width:12px;height:8px;border-radius:2px;margin-right:5px}
.tl{display:grid;grid-template-columns:92px 1fr;gap:4px 12px;align-items:start}
.tl .d{font-variant-numeric:tabular-nums;font-weight:600}
.track{position:relative;height:34px;margin:6px 0 14px;border-bottom:2px solid var(--axis)}
.track .pt{position:absolute;bottom:-6px;width:10px;height:10px;border-radius:50%;background:var(--b2);border:2px solid var(--surface);transform:translateX(-5px)}
.track .tk{position:absolute;bottom:-20px;font-size:11px;color:var(--muted);transform:translateX(-50%)}
.threats{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:12px}
.tc{border:1px solid var(--border);border-radius:8px;padding:12px;background:var(--surface)}
.tc dl{display:grid;grid-template-columns:auto 1fr;gap:2px 10px;margin:8px 0 0;font-size:13px}.tc dt{color:var(--ink2)}.tc dd{margin:0}
button,input[type=file]{font:inherit}button{padding:6px 12px;border-radius:6px;border:1px solid var(--axis);background:var(--surface);color:var(--ink);cursor:pointer}
button:hover{background:var(--wash)}
ul{margin:6px 0 0;padding-left:20px}li{margin:5px 0}
.err{color:var(--critical);font-weight:600}
summary{cursor:pointer;color:var(--link)}
</style>
</head>
<body>
<main>
<h1>Tariff Early-Warning Dashboard</h1>
<div class="muted" id="sub"></div>
<div class="banner adv" id="advisory"></div>
<div id="mode"></div>
<div class="tiles" id="tiles"></div>

<section class="card"><h2>Upcoming decisions and dates</h2><div id="timeline"></div></section>

<section class="card">
<h2>Product risk, sorted by likely $ impact per year</h2>
<div class="muted" id="prodsrc"></div>
<div class="legend" style="margin-top:8px"><span><span class="sw" style="background:var(--b1)"></span>Duty today</span><span><span class="sw" style="background:var(--b2)"></span>Likely case (probability-weighted)</span><span><span class="sw" style="background:var(--b3)"></span>Worst case (top of range)</span></div>
<div class="scroll"><table id="prod"></table></div>
</section>

<section class="card">
<h2>Analyse a different importer (CSV upload)</h2>
<div class="muted">Columns: <code>sku, description, hts_code, country_of_origin, unit_cost_usd, annual_units</code>. The file is read in your browser and never uploaded anywhere. Browser results use the deterministic matcher only (no LLM review of ambiguous matches) and know base duties only for HTS codes already in the database; for the full analysis run <code>python run.py --importer yourfile.csv</code>.</div>
<p><input type="file" id="csv" accept=".csv,text/csv"> <button id="reset" hidden>Back to sample importer</button></p>
<div id="csvmsg"></div>
</section>

<section class="card"><h2>Weekly alert</h2><div id="alert"></div></section>

<section class="card">
<h2>Threat cards</h2>
<p><label><input type="checkbox" id="onlyMatched" checked> Only notices that match a product</label></p>
<div class="threats" id="threats"></div>
</section>

<section class="card"><h2>Backtest: would the agent have warned in time?</h2><div id="backtest"></div></section>
<section class="card"><h2>Prediction-market context (Kalshi)</h2><div class="muted">Shown for context only; not used in any calculation.</div><div class="scroll"><table id="kalshi"></table></div></section>
<section class="card"><h2>Data sources</h2><div id="sources"></div></section>
</main>
<script id="data" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = x => (x < 0 ? '-' : '') + '$' + Math.abs(x).toLocaleString('en-US', {maximumFractionDigits: 0});
const money2 = x => '$' + x.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
const pct = x => (Math.round(x * 10) / 10).toFixed(1) + '%';
const link = (u, t) => `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(t)}</a>`;
const STATUS = {'in force': ['var(--ink2)', '■'], 'action announced': ['var(--critical)', '▲'], 'comments open': ['var(--warning)', '●'],
  'hearing': ['var(--warning)', '●'], 'investigation': ['var(--serious)', '◆'], 'struck down': ['var(--good)', '✕'], 'expired': ['var(--good)', '✕']};
const chip = s => { const [c, g] = STATUS[s] || ['var(--muted)', '●']; return `<span class="chip"><span style="color:${c}">${g}</span> ${esc(s)}</span>`; };

function header() {
  $('sub').textContent = `As of ${D.as_of} · ${D.counts.documents} Federal Register notices · ${D.counts.active_threats} active measures · notice reader: ${D.reader_label}`;
  $('advisory').textContent = D.advisory;
  $('mode').innerHTML = D.offline ? `<div class="banner"><b>Offline mode.</b> No Anthropic API key was found, so notices were read by the rule-based fallback and ambiguous matches were not reviewed by the LLM. Rates, scope and country lists are less reliable than in LLM mode. Add <code>ANTHROPIC_API_KEY</code> to <code>.env</code> and rerun <code>python run.py</code>.</div>` : '';
}

function tiles(rows) {
  const exposed = rows.filter(r => r.costs.likely_delta_per_year > 0).length;
  const likely = rows.reduce((a, r) => a + r.costs.likely_delta_per_year, 0);
  const worst = rows.reduce((a, r) => a + r.costs.worst_delta_per_year, 0);
  const next = D.timeline[0];
  $('tiles').innerHTML = [
    [`${exposed} of ${rows.length}`, 'products with a pending duty change'],
    [money(likely), 'likely added duty cost per year'],
    [money(worst), 'worst-case added duty cost per year'],
    [next ? `${next.days_away} days` : 'none', next ? `to next date: ${next.label} (${next.date})` : 'no dated decisions in the next year'],
  ].map(([v, l]) => `<div class="tile"><div class="v">${esc(v)}</div><div class="l">${esc(l)}</div></div>`).join('');
}

function timeline() {
  const T = D.timeline;
  if (!T.length) { $('timeline').innerHTML = '<div class="muted">No dated decisions found in the next 12 months.</div>'; return; }
  const span = Math.max(90, ...T.map(e => e.days_away));
  const ticks = [0, 90, 180, 270, 360].filter(t => t <= span + 10);
  let h = `<div class="track" role="img" aria-label="Timeline of upcoming dates">` +
    ticks.map(t => `<span class="tk" style="left:${t / span * 96 + 2}%">${t ? '+' + t + 'd' : 'today'}</span>`).join('') +
    T.map(e => `<span class="pt" style="left:${e.days_away / span * 96 + 2}%" title="${esc(e.date + ': ' + e.label + ' - ' + e.title)}"></span>`).join('') + `</div><div class="tl">`;
  for (const e of T) h += `<div class="d">${esc(e.date)}<br><small>in ${e.days_away} days</small></div><div><b>${esc(e.label)}</b> ${chip(e.status)} <span class="chip">Sec. ${esc(e.authority)}</span><br>${link(e.url, e.title)}<br><small>${esc(e.basis)}</small></div>`;
  $('timeline').innerHTML = h + '</div>';
}

function products(rows, label) {
  $('prodsrc').textContent = label;
  const max = Math.max(1, ...rows.map(r => r.costs.worst_pct));
  let h = `<tr><th>Product</th><th>Duty rate: today / likely / worst</th><th class="n">Landed $/unit<br>today</th><th class="n">likely</th><th class="n">worst</th><th class="n">Added $/year<br>likely</th><th class="n">worst</th><th class="n">Exposure<br>score</th></tr>`;
  rows.forEach((r, i) => {
    const c = r.costs;
    const bar = (v, col, name) => `<div class="bar" style="width:${Math.max(1, v / max * 100)}%;background:${col}" title="${name}: ${pct(v)}"></div>`;
    h += `<tr><td><b>${esc(r.sku)}</b><br>${esc(r.description)}<br><small>HTS ${esc(r.hts_code)} · ${esc(r.country_of_origin)} · ${money2(r.unit_cost_usd)} × ${r.annual_units.toLocaleString()}</small></td>
      <td class="bars">${bar(c.today_pct, 'var(--b1)', 'Today')}${bar(c.likely_pct, 'var(--b2)', 'Likely')}${bar(c.worst_pct, 'var(--b3)', 'Worst')}<small>${pct(c.today_pct)} / ${pct(c.likely_pct)} / ${pct(c.worst_pct)}</small></td>
      <td class="n">${money2(c.today_per_unit)}</td><td class="n">${money2(c.likely_per_unit)}</td><td class="n">${money2(c.worst_per_unit)}</td>
      <td class="n"><b>${money(c.likely_delta_per_year)}</b></td><td class="n">${money(c.worst_delta_per_year)}</td><td class="n">${r.exposure_score}</td></tr>
      <tr class="detail"><td colspan="8"><details><summary>Why: ${esc(r.reason.slice(0, 150))}</summary>
      <p><small>Base (column 1 general) duty: ${r.base_duty.raw ? esc(r.base_duty.raw) : 'unknown'}${r.base_duty.note ? ' — ' + esc(r.base_duty.note) : ''}${r.base_duty.source_url ? ' · ' + link(r.base_duty.source_url, 'USITC HTS') : ''}. Annual landed cost today ${money(c.today_per_year)}, likely ${money(c.likely_per_year)}, worst ${money(c.worst_per_year)}.</small></p>
      ${r.threats.length ? `<table><tr><th>Notice</th><th>Status</th><th class="n">Chance in 90 days</th><th class="n">Rate range</th><th>Match and reason</th></tr>` + r.threats.map(t => `<tr><td>${link(t.url, t.title)}<br><small>Section ${esc(t.authority)}</small></td><td>${chip(t.status)}${t.counted_in_today ? '<br><small>counted in duty today</small>' : ''}</td><td class="n">${Math.round(t.likelihood_now * 100)}%<br><small>used: ${Math.round(t.probability_used * 100)}%</small></td><td class="n">${t.rate_low_pct}–${t.rate_high_pct}%<br><small>${esc(t.rate_basis)}</small></td><td>${esc(t.method)} (strength ${t.strength})<br><small>${esc(t.reason)}</small></td></tr>`).join('') + '</table>' : '<small>No matching notices.</small>'}
      </details></td></tr>`;
  });
  $('prod').innerHTML = h;
  tiles(rows);
}

function threats(rows) {
  const matched = new Set(rows.flatMap(r => r.threats.map(t => t.doc_id)));
  const only = $('onlyMatched').checked;
  const list = D.threats.filter(t => !only || matched.has(t.doc_id));
  $('threats').innerHTML = list.map(t => {
    const kd = Object.entries(t.key_dates).filter(([, v]) => v).map(([k, v]) => `${k.replace(/_/g, ' ')}: ${v}`).concat(t.other_dates.map(o => `${o.label}: ${o.date}`));
    const rate = t.rate_low_pct == null && t.rate_high_pct == null ? 'not stated' : `${t.rate_low_pct ?? t.rate_high_pct}–${t.rate_high_pct ?? t.rate_low_pct}%`;
    return `<div class="tc"><div>${chip(t.status)} <span class="chip">Section ${esc(t.authority)}</span> <span class="chip">${Math.round(t.likelihood_now * 100)}% in 90 days</span></div>
      <h3 style="margin-top:8px">${link(t.url, t.title)}</h3><small>Published ${esc(t.publication_date)} · read by ${t.extractor === 'llm' ? 'LLM' : 'rule-based fallback'}</small>
      <dl><dt>Products</dt><dd>${esc(t.categories.join('; ') || (t.applies_to_all_products ? 'all products' : 'not stated'))}</dd>
      <dt>HTS codes</dt><dd>${t.hts_codes.length ? esc(t.hts_codes.slice(0, 12).join(', ')) + (t.hts_codes.length > 12 ? ` … (+${t.hts_codes.length - 12})` : '') : 'none stated'}</dd>
      <dt>Countries</dt><dd>${esc(t.countries.join(', ') || 'not stated')}</dd><dt>Rate</dt><dd>${rate}</dd>
      <dt>Key dates</dt><dd>${esc(kd.join('; ') || 'none stated')}</dd><dt>Rationale</dt><dd>${esc(t.rationale)}</dd></dl></div>`;
  }).join('') || '<div class="muted">No notices match.</div>';
}

function alertBox() {
  const a = D.alert;
  const sec = (title, items) => `<h3 style="margin-top:12px">${title}</h3>` + (items.length ? '<ul>' + items.map(i => `<li><b>${esc(i.headline)}</b><br>${esc(i.detail)} ${i.source_urls.map(u => link(u, 'source')).join(' ')}</li>`).join('') + '</ul>' : '<div class="muted">Nothing to report.</div>');
  $('alert').innerHTML = `<p>${esc(a.summary)}</p>` + sec('Top risks', a.top_risks) + sec('Dates to watch', a.dates_to_watch) + sec('Suggested actions to consider', a.suggested_actions) +
    `<p><small>Written by ${D.alert_writer === 'llm' ? 'Claude' : 'the offline template'}. ${esc(D.advisory)}</small></p>`;
}

function backtest() {
  const b = D.backtest, s = b.summary, p = v => v == null ? '–' : Math.round(v * 100) + '%';
  let h = `<div class="tiles" style="margin-top:0">` + [
    [`${s.hits} of ${s.duty_events}`, 'real duty changes flagged in advance'],
    [s.avg_lead_weeks == null ? 'n/a' : s.avg_lead_weeks + ' weeks', 'average lead time on hits'],
    [`${s.false_alarms} of ${s.alarms}`, 'high-likelihood alarms not followed by a duty within 90 days'],
  ].map(([v, l]) => `<div class="tile"><div class="v">${esc(v)}</div><div class="l">${esc(l)}</div></div>`).join('') + `</div>
  <div class="scroll"><table style="margin-top:12px"><tr><th>Event</th><th>Change date</th><th>Verdict</th><th class="n">Products flagged</th><th>First warning</th><th class="n">Lead (weeks)</th><th class="n">Likelihood at first warning</th><th class="n">On the eve</th></tr>`;
  for (const r of b.events) h += `<tr><td>${link(r.action_url, r.name)}</td><td>${esc(r.change_date)}</td><td><b>${esc(r.verdict)}</b></td><td class="n">${r.skus_flagged.length}/${r.skus.length}</td><td>${r.first_warning ? link(r.first_warning.url, r.first_warning.date) : 'none: no advance notice'}</td><td class="n">${r.lead_weeks ?? '–'}</td><td class="n">${p(r.likelihood_first)}</td><td class="n">${p(r.likelihood_eve)}</td></tr>`;
  h += `</table></div><p><small>The agent saw only documents published before each change. Likelihood rules were tuned on these same 2025–2026 cases, so this is an in-sample illustration, not a forecast-accuracy claim. Full detail: reports/backtest.md.</small></p>`;
  if (b.alarms.length) h += `<details><summary>False-alarm scan (${b.alarms.length} alarms)</summary><div class="scroll"><table><tr><th>Alarm date</th><th>Measure</th><th class="n">Likelihood</th><th>Outcome</th><th>Note</th></tr>` +
    b.alarms.map(a => `<tr><td>${esc(a.alarm_date)}</td><td>${link(a.url, 'Section ' + a.authority + ': ' + a.topic.replace(/_/g, ' '))}</td><td class="n">${p(a.likelihood)}</td><td>${esc(a.verdict)}</td><td>${esc(a.note)}</td></tr>`).join('') + '</table></div></details>';
  $('backtest').innerHTML = h;
}

function misc() {
  $('kalshi').innerHTML = D.kalshi.length ? '<tr><th>Market</th><th class="n">Implied probability</th><th>Closes</th></tr>' + D.kalshi.map(k => `<tr><td>${link(k.url, k.title)}</td><td class="n">${k.yes_price == null ? '–' : Math.round(k.yes_price * 100) + '%'}</td><td>${esc((k.close_time || '').slice(0, 10))}</td></tr>`).join('') : '<tr><td class="muted">No open tariff markets were retrieved.</td></tr>';
  $('sources').innerHTML = '<ul>' + D.sources.map(s => `<li><b>${esc(s.source)}</b>: ${s.ok ? 'reachable' : 'UNREACHABLE'} (${esc(s.detail)}, checked ${esc(s.checked_at)})</li>`).join('') + '</ul>' + (D.used_seed ? '<div class="banner">Federal Register was unreachable; notices come from the labelled manual seed file data/seed_actions.json.</div>' : '');
}

/* ---- Browser port of tariff_agent/matcher.py and tariff_agent/duty.py (deterministic parts only) ---- */
const normCountry = n => { n = String(n || '').trim().toLowerCase().replace(/\s+/g, ' '); return D.country_aliases[n] || n; };
const fmtHts = d => [d.slice(0, 4), d.slice(4, 6), d.slice(6, 8), d.slice(8, 10)].filter(Boolean).join('.');
const overlap = (hts, codes) => codes.filter(c => c && hts.startsWith(c)).sort((a, b) => b.length - a.length)[0] || null;
function matchProduct(p, t) {
  const cs = t.countries.map(normCountry);
  if (cs.length && !cs.includes('all') && !cs.includes(normCountry(p.country_of_origin))) return null;
  const code = overlap(p.hts, t.hts_codes);
  if (code) return {method: 'hts', strength: code.length >= 6 ? 1.0 : code.length >= 4 ? 0.8 : 0.6, reason: `HTS ${fmtHts(code)} listed in the notice covers product code ${fmtHts(p.hts)}.`};
  if (t.applies_to_all_products && cs.length) return {method: 'country-wide', strength: 0.9, reason: 'Across-the-board measure; exemptions in the notice’s annexes not checked.'};
  for (const topic of t.topics) { const pre = overlap(p.hts, (D.concordance[topic] || {}).hts || []);
    if (pre) return t.hts_codes.length ? {method: 'derivative', strength: 0.4, reason: `Product is in the '${topic}' family, but the codes listed in the notice do not include it. (Unverified: no LLM review in the browser.)`}
      : {method: 'concordance', strength: 0.7, reason: `Notice names '${topic}' without HTS codes; product falls under ${fmtHts(pre)} in the category concordance. (Unverified: no LLM review in the browser.)`}; }
  for (const topic of t.topics) { const rx = (D.concordance[topic] || {}).product;
    if (rx && new RegExp(rx, 'i').test(p.description)) return {method: 'derivative', strength: 0.35, reason: `Description suggests a possible '${topic}' derivative, but the HTS code is outside the usual range. (Unverified: no LLM review in the browser.)`}; }
  return null;
}
function combine(r) { const o = {}; for (const k in r) if (r[k] > 0) o[k] = r[k]; if (o['232'] > 0) delete o['122']; return Object.values(o).reduce((a, b) => a + b, 0); }
function scenario(base, duties) {
  const today = {}, likely = {}, worst = {};
  for (const a of new Set(duties.map(d => d.authority))) {
    const mine = duties.filter(d => d.authority === a);
    const cur = Math.max(0, ...mine.filter(d => d.in_force).map(d => d.low));
    today[a] = cur;
    likely[a] = cur + Math.max(0, ...mine.filter(d => !d.in_force).map(d => d.p * Math.max(0, (d.low + d.high) / 2 - cur)));
    worst[a] = Math.max(0, ...mine.map(d => d.high));
  }
  const t = base + combine(today);
  return {today: t, likely: Math.max(t, base + combine(likely)), worst: Math.max(t, base + combine(worst))};
}
function analyse(p) {
  const known = Object.prototype.hasOwnProperty.call(D.base_rates, p.hts.slice(0, 8)) ? D.base_rates[p.hts.slice(0, 8)] : undefined;
  const base = known && known.pct != null ? known.pct : 0;
  const note = known === undefined ? 'base duty not in the database; treated as 0% (run the Python pipeline for a live HTS lookup)' : known.pct == null ? `base duty '${known.raw}' is not ad valorem; treated as 0%` : null;
  const duties = [], infos = [];
  for (const t of D.threats) {
    if (t.status === 'struck down' || t.status === 'expired') continue;
    const m = matchProduct(p, t); if (!m) continue;
    let low = t.rate_low_pct, high = t.rate_high_pct, basis = 'stated in notice';
    if (low == null && high == null) { if (t.status === 'in force') { low = high = 0; basis = 'rate not stated in notice (counted as 0)'; } else { [low, high] = D.assumed_rates[t.authority]; basis = 'assumed scenario range (notice states no rate)'; } }
    else { low = low ?? high; high = high ?? low; }
    const paid = t.status === 'in force' && m.strength >= 0.6;
    const pu = paid ? 1 : Math.min(1, Math.round(t.likelihood_now * m.strength * 1e4) / 1e4);
    duties.push({authority: t.authority, low, high, p: pu, in_force: paid});
    infos.push({doc_id: t.doc_id, title: t.title, url: t.url, authority: t.authority, status: t.status, method: m.method, strength: m.strength, reason: m.reason, likelihood_now: t.likelihood_now, probability_used: pu, rate_low_pct: low, rate_high_pct: high, rate_basis: basis, counted_in_today: paid});
  }
  const s = scenario(base, duties), u = p.unit_cost_usd, n = p.annual_units, per = r => u * (1 + r / 100);
  const c = {base_pct: base, today_pct: s.today, likely_pct: s.likely, worst_pct: s.worst, today_per_unit: per(s.today), likely_per_unit: per(s.likely), worst_per_unit: per(s.worst)};
  for (const k of ['today', 'likely', 'worst']) c[k + '_per_year'] = c[k + '_per_unit'] * n;
  c.likely_delta_per_year = c.likely_per_year - c.today_per_year; c.worst_delta_per_year = c.worst_per_year - c.today_per_year;
  const pend = infos.filter(i => !i.counted_in_today);
  infos.sort((a, b) => (a.counted_in_today - b.counted_in_today) || (b.probability_used - a.probability_used));
  const top = pend.slice().sort((a, b) => b.probability_used - a.probability_used)[0];
  return {sku: p.sku, description: p.description, hts_code: fmtHts(p.hts), country_of_origin: p.country_of_origin, unit_cost_usd: u, annual_units: n,
    base_duty: {pct: base, raw: known ? known.raw : null, source_url: null, note}, threats: infos, costs: c,
    exposure_score: Math.round(100 * Math.max(0, ...pend.map(i => i.probability_used))),
    reason: top ? `${top.title.slice(0, 90)} - ${top.reason}` : infos.length ? 'Only duties already in force match this product; no pending change found.' : 'No current tariff notice matches this product.'};
}
function parseCsv(text) {
  const rows = []; let row = [], cell = '', q = false;
  text = text.replace(/^﻿/, '');
  for (let i = 0; i < text.length; i++) { const ch = text[i];
    if (q) { if (ch === '"' && text[i + 1] === '"') { cell += '"'; i++; } else if (ch === '"') q = false; else cell += ch; }
    else if (ch === '"') q = true; else if (ch === ',') { row.push(cell); cell = ''; }
    else if (ch === '\n' || ch === '\r') { if (ch === '\r' && text[i + 1] === '\n') i++; row.push(cell); cell = ''; if (row.some(c => c.trim())) rows.push(row); row = []; }
    else cell += ch; }
  row.push(cell); if (row.some(c => c.trim())) rows.push(row);
  return rows;
}
function loadCsv(text) {
  const rows = parseCsv(text); if (rows.length < 2) throw new Error('CSV has no product rows');
  const head = rows[0].map(h => h.trim().toLowerCase()), need = D.required_columns, miss = need.filter(c => !head.includes(c));
  if (miss.length) throw new Error('CSV is missing column(s): ' + miss.join(', '));
  return rows.slice(1).map((r, i) => { const g = k => (r[head.indexOf(k)] || '').trim(), hts = g('hts_code').replace(/\D/g, '');
    const cost = parseFloat(g('unit_cost_usd').replace(/[$,]/g, '')), units = parseFloat(g('annual_units').replace(/,/g, ''));
    if (hts.length < 6) throw new Error(`line ${i + 2}: hts_code needs at least 6 digits`);
    if (!(cost >= 0) || !(units >= 0)) throw new Error(`line ${i + 2}: unit_cost_usd and annual_units must be non-negative numbers`);
    return {sku: g('sku') || 'row' + (i + 2), description: g('description'), hts, country_of_origin: g('country_of_origin'), unit_cost_usd: cost, annual_units: units}; });
}
let current = D.products;
function show(rows, label) { current = rows; products(rows, label); threats(rows); }
$('csv').addEventListener('change', async e => {
  const f = e.target.files[0]; if (!f) return;
  try { const rows = loadCsv(await f.text()).map(analyse).sort((a, b) => (b.costs.likely_delta_per_year - a.costs.likely_delta_per_year) || (b.costs.worst_delta_per_year - a.costs.worst_delta_per_year));
    show(rows, `Uploaded file: ${f.name} (${rows.length} products) — browser estimate, deterministic matcher only`);
    $('csvmsg').innerHTML = `<span class="muted">Analysed ${rows.length} products from ${esc(f.name)}. The alert and backtest sections below still describe the sample importer.</span>`; $('reset').hidden = false;
  } catch (err) { $('csvmsg').innerHTML = `<span class="err">⚠ ${esc(err.message)}</span>`; }
});
$('reset').addEventListener('click', () => { show(D.products, D.products_label); $('csv').value = ''; $('csvmsg').textContent = ''; $('reset').hidden = true; });
$('onlyMatched').addEventListener('change', () => threats(current));
header(); timeline(); show(D.products, D.products_label); alertBox(); backtest(); misc();
</script>
</body>
</html>
"""


def build(payload: dict) -> str:
    """Render the dashboard HTML from the pipeline payload."""
    data = dict(payload)
    data["concordance"] = {k: {"hts": v["hts"], "product": v["product"]} for k, v in concordance.TOPICS.items()}
    data["country_aliases"] = matcher.COUNTRY_ALIASES
    data["assumed_rates"] = {k: list(v) for k, v in config.ASSUMED_RATE_RANGE_PCT.items()}
    data["required_columns"] = matcher.REQUIRED_COLUMNS
    data["advisory"] = config.ADVISORY
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.replace("__DATA__", blob)


def write(payload: dict) -> str:
    config.ensure_dirs()
    out = config.DASHBOARD / "index.html"
    out.write_text(build(payload), encoding="utf-8")
    return str(out)
