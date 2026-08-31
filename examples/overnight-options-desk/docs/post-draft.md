# Social post drafts — GRE-3464

**DRAFT ONLY. Not posted. Requires explicit human review + approval before
publishing anywhere.**

Context: `examples/overnight-options-desk` — a five-lane example built on
[Solari](https://getsolari.com) that scrapes an options-desk symbol
universe (cloud browser), fits GARCH/OU/momentum models in a sandbox,
renders a self-contained morning brief, and publishes it on a sandbox
preview URL + GitHub Pages. Real end-to-end demo run: status `full`,
$0.0309 spend, cumulative project spend ≈$0.0509. See the package
[README](../README.md) for the full writeup.

Repo link and Pages URL below are placeholders pending the PM's merge +
`gh api ... /pages` enable step (see the generator report for the exact
command) — fill in the live Pages URL once it resolves.

---

## X / Twitter (≤280 chars)

```
Built an overnight options desk on @getsolari: cloud browser scrapes earnings/news/quotes, a sandbox fits GARCH+OU+momentum, and renders a brief with a live preview URL. Research only, not advice. cc @harrychow_
https://github.com/erichenschel/solari-cookbook
https://erichenschel.github.io/solari-cookbook/
```

Length check: body text is 211 raw characters; X counts each URL as a
fixed 23 characters (t.co shortening) regardless of actual length, so two
URLs on their own lines add 1 (newline) + 23 + 1 (newline) + 23 = 48,
giving an effective length of **259/280** — 21 characters of headroom.

## LinkedIn (longer variant)

```
I built an overnight options research desk end-to-end on Solari (getsolari.com) —
a cloud browser + code sandbox API — as a cookbook example.

What it does, unattended, from a cold start:
1. Opens real cloud-browser sessions to scrape earnings dates, headlines, and
   quotes for a small symbol universe (Yahoo Finance, MarketWatch/Google News
   RSS, with graceful fallback chains when a primary source is blocked or down).
2. Spins up a throwaway sandbox VM, installs numpy/arch/statsmodels, and fits
   a GARCH(1,1) volatility forecast, an Ornstein-Uhlenbeck mean-reversion
   model, and 5-day momentum per symbol — textbook formulas, nothing
   proprietary.
3. Renders both into one self-contained brief.html (no JS, no external
   requests — readable from a bare file:// URL) and publishes it on a public
   sandbox preview URL, no deploy step.

The real demo run (AAPL, NVDA, MSFT, TSLA, AMZN) came back status "full" —
every stage succeeded live, including two real infrastructure findings along
the way (a stale sandbox VM clock breaking TLS cert validation, and Nasdaq's
earnings page blocking a vanilla cloud browser, both documented and worked
around in the code rather than papered over). Total spend for that run:
$0.0309. Cumulative spend across every live test this project has run,
including prior build phases: about five cents.

It's a research tool, not trading advice — every brief carries an explicit
disclaimer and the model outputs are research labels (mean-reversion-watch,
trend-watch, ...), never buy/sell signals.

Code, README, and the real published brief:
https://github.com/erichenschel/solari-cookbook
https://erichenschel.github.io/solari-cookbook/

cc Harry Chow / @getsolari
```
