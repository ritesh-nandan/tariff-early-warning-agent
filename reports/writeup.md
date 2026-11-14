# Tariff Early-Warning Agent for Small US Importers

*Prototype write-up. Results below are from the run of 14 November 2026 in offline mode
(rule-based notice reader, no LLM calls), because no API key was configured on the build
machine. Rerunning `python run.py` with a key regenerates the dashboard, alert and backtest
with the LLM reader; this document's numbers should then be updated from `reports/backtest.md`.*

> Advisory only. Not legal advice and not customs-broker advice.

## 1. Problem

US tariffs now change month to month. Between January 2025 and November 2026 the Federal
Register carried Section 232 actions on steel, aluminum, copper, lumber, trucks,
semiconductors, pharmaceuticals, polysilicon and drones; a 10% Section 122 surcharge that
ran from 24 February to 24 July 2026; and Section 301 duties on 60 economies that took
effect the day the surcharge ended. Most of these were visible in advance: an investigation
is announced, comments are taken, and a statutory clock runs. Large importers pay
trade-compliance teams to watch this. A ten-person importer does not, and finds out when the
customs broker's invoice changes.

The question this prototype answers for such a firm: **which of my products are exposed,
how likely and how soon is the change, and what does it do to my landed cost?**

## 2. Approach

One command (`python run.py`) runs eight steps.

1. **Collect.** The Federal Register API supplies presidential documents, Commerce/BIS
   notices and USTR notices mentioning Sections 232, 301 and 122 since 20 January 2025
   (131 relevant notices). The USITC HTS API supplies base duty rates. Kalshi supplies open
   tariff markets as context. Each source is probed first; a labelled seed file of 21 real
   notices stands in if the Federal Register is down. Everything is stored in SQLite.
2. **Read.** Claude turns each notice into a *threat card*: authority, status, product
   categories, HTS codes, countries, rate range, key dates, a 0-1 likelihood of taking effect
   within 90 days, and a one-line rationale. Output is strict JSON validated with pydantic,
   retried once, cached by document hash and logged. The prompt forbids inferring dates,
   rates or codes the notice does not state.
3. **Match.** Products match threat cards by HTS prefix and country. Investigation notices
   rarely list HTS codes, so a small topic concordance proposes candidates; these and
   possible "derivative" products are the only cases sent to the LLM, which must give a
   written reason.
4. **Simulate.** Plain, unit-tested Python computes duty today (base plus in-force
   additional duties), a probability-weighted likely case, and a worst case at the top of
   the rate range, in dollars per unit and per year.
5. **Alert.** A short brief lists top risks, dates to watch and options to consider (order
   early, second supplier, foreign-trade zone or bonded warehouse, reprice). Every item must
   cite a source URL from the input; the code rejects briefs that cite anything else.
6. **Backtest and dashboard.** Described below.

The division of labour is deliberate: the LLM reads and judges; code does dates, status
over time, stacking rules and arithmetic.

## 3. Results

**Current picture (sample importer, 22 products).** 34 measures are active. Every product
carries a pending change, mainly because of an open Section 301 investigation covering many
economies. Estimated added duty cost is about $284k per year in the likely case and $3.55M
in the worst case. The nearest dated event is the polysilicon duty taking effect on
4 December 2026, which the alert flags with an "order early" option for the raw polysilicon SKU.

**Backtest.** Eight real events; the agent saw only documents published before each one.

| Event | Verdict | Lead time |
|---|---|---|
| Copper Section 232 tariff (Jul 2025) | hit | 21.7 weeks |
| Steel/aluminum raised from 25% to 50% (Jun 2025) | **miss** | none |
| Section 122 surcharge (Feb 2026) | **miss** | none |
| Pharmaceuticals Section 232 (Apr 2026) | hit | 50.1 weeks |
| Section 301 duties replacing Section 122 (Jul 2026) | hit | 18.4 weeks |
| Polysilicon Section 232 (Aug 2026) | hit | 55.1 weeks |
| Drones Section 232 (Aug 2026) | hit | 56.1 weeks |
| Wind turbines Section 232 (Oct 2026) | flagged, but no duty followed | 60.4 weeks |

- **Hit rate: 5 of 7 duty changes (71%)**, average lead time 40.3 weeks.
- **Two misses were genuine surprises.** The steel/aluminum doubling was signed one day
  before it took effect, and the Section 122 surcharge four days before. No Federal Register
  notice preceded either. A notice-based agent cannot catch these.
- **False alarms are frequent: 11 of 18.** When a pending measure's likelihood reached 50%,
  a duty followed within 90 days only 6 times (one alarm is still open). Four Section 232
  investigations (aircraft, critical minerals, robotics, wind turbines) ended with a
  proclamation ordering negotiations and no tariff; others simply ran longer than 90 days.
- **Early likelihoods are low by design.** A new investigation gets 5-10%; the value rises
  as the statutory clock runs, reaching 50-75% on the eve of four of the five hits.

The honest reading: the agent is good at telling an importer *that a product is in play*
many months ahead, weak at saying *when*, and it cannot tell a tariff outcome from a
negotiation outcome in advance.

## 4. Limitations

- **Offline results.** This run used the rule-based reader. It misreads some rates (it
  takes the lowest and highest percentages in the operative text), assumes "all countries"
  for multi-economy actions, and captures few HTS codes. The LLM reader is built, validated
  against the schema and cached, but has not been run end to end here.
- **In-sample likelihoods.** The ageing table was set from the same 2025-2026 Section 232
  cases that appear in the backtest. Eve-of-change likelihoods are therefore optimistic.
- **Small sample.** Eight events, one synthetic importer.
- **Scope is approximate.** The topic concordance is hand-written. Annex-level exemptions,
  country deals, quotas and company-specific rates are not modelled.
- **Duty math is simplified.** Ad valorem only; freight, fees and drawback excluded;
  Section 232 derivative duties applied to full value rather than metal content; one
  stacking rule for all cases; "net of MFN" Section 301 rates ignored.
- **Coverage.** Only the Federal Register. Court rulings, White House fact sheets, USTR
  press releases and CBP guidance often move first.
- **Possible LLM hindsight.** In LLM mode the model may know how past events ended.

## 5. Next steps

1. Run with the LLM reader and compare card accuracy against a hand-labelled set of 30 notices.
2. Add faster sources: White House actions, USTR press releases, CBP CSMS messages, court dockets.
3. Replace the concordance with scope taken from annexes and the HTS chapter 99 notes.
4. Predict *outcome type* (tariff, quota, negotiation) separately from *timing*; the wind,
   aircraft and robotics cases show these are different questions.
5. Fit the hazard table on 2018-2024 cases and test out of sample on 2025-2026.
6. Pilot with two or three real importers and measure whether alerts change ordering decisions.
7. Add metal-content shares, FTZ and drawback modelling to the cost simulator.
