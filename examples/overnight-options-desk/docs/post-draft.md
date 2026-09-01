# Social post package — FINAL (fact-checked 2026-09-01)

**DRAFT ONLY. Posted manually by Eric. All numbers verified against run
logs and a fresh-clone test on 2026-09-01 — see "Fact basis" at bottom.**

## How to post (X)

1. **Quote-tweet** the challenge post (do NOT post standalone — the QT
   inherits its audience): https://x.com/harrychow_/status/2094437473912844480
2. Attach `docs/latest/brief-screenshot.jpg` (or a 20s screen recording
   of the live brief — scroll + expand a dropdown — if time allows;
   motion outperforms stills).
3. Post the main text, then add the four replies to your own post,
   staggered over ~10 minutes, in order.
4. After posting: share repo link + one-liner in the Solari Discord
   (https://discord.gg/2g8qQbTEbk), then post the LinkedIn variant
   linking the X thread. Pin the X post to your profile.

## Main post (quote-tweeting the challenge)

```
Your posting says $300K. My application cost about a dime. Every morning it rents a browser, a VM, and a web server for 90 seconds, reads the market, runs the models, and publishes my options brief before I wake up. @getsolari @harrychow_
https://github.com/erichenschel/solari-cookbook
https://erichenschel.github.io/solari-cookbook/examples/overnight-options-desk/docs/latest/index.html
```

## Reply 1 — the outage story

```
Best part: Solari's browser gateway went down mid-build. The desk shrugged, fell back to plain HTTP for what it could, and published the brief anyway. Then we diagnosed the outage — SDK version skew — upgraded, and the next run came back spotless. Stress-testing your infra wasn't in the posting, but here we are.
```

## Reply 2 — the receipts

```
The receipts, because vibes aren't verification: 154 tests passing from a fresh clone with no API key, every scrape session recorded and replayable, per-stage budget logs, 3 bugs found and documented in Solari's own stack. Total spend for the entire build and every demo: about a dime.
```

## Reply 3 — method + QuantMechanix

**Attach `docs/architecture.png` to this reply** — the four-stage diagram
with the Solari primitives and the contracts / resilience / receipts cards.

```
And yes, AI wrote most of it — as instructed. I wrote the contracts and acceptance criteria, four agent lanes built the stages in parallel, nothing merged unverified. Spec → live URL in one evening. Modeled on the research desk I run daily at QuantMechanix.
```

## Reply 4 — CTA closer

```
Fork it and run it yourself: clone, one env var, one command. The $3 free credit covers ~100 mornings of briefs. Research tool, not investment advice.
May the best dev win.
https://github.com/erichenschel/solari-cookbook
```

## LinkedIn (post after X, link the X thread at the end)

```
I built an overnight options research desk end-to-end on Solari (getsolari.com) — a cloud browser + code sandbox API — as my entry to Pinetree Research's build challenge.

What it does, unattended, from a cold start:
1. Opens real cloud-browser sessions to scrape earnings dates, headlines, and quotes for a small symbol universe — every session recorded and replayable, with graceful fallback chains when a source is blocked or down.
2. Spins up a throwaway sandbox VM and fits a GARCH(1,1) volatility forecast, an Ornstein-Uhlenbeck mean-reversion model, and 5-day momentum per symbol — textbook formulas, published in the page's own legend.
3. Renders one self-contained morning brief (no JS, no external requests) and publishes it on a public URL, no deploy step.

Mid-build, Solari's browser gateway had a real outage. The pipeline kept publishing via browserless HTTP fallbacks, we root-caused the failure as SDK version skew, and the next run came back with zero warnings. Three findings in the platform's own stack, documented in the README where they bite.

Built with AI, deliberately: I wrote the contracts and acceptance criteria; four AI agent lanes built the stages in parallel; nothing merged without verification. Spec to live URL in one evening. 154 tests pass from a fresh clone. Total spend: about a dime.

It's a research tool, not trading advice — every brief carries the disclaimer, and the model outputs are research labels, never buy/sell signals. Modeled on the research desk I run daily at QuantMechanix.

Code, README, and the live brief:
https://github.com/erichenschel/solari-cookbook
https://erichenschel.github.io/solari-cookbook/examples/overnight-options-desk/docs/latest/index.html

X thread: [link the X post here after publishing]
```

## Fact basis (all claims verified 2026-09-01)

- "about a dime": cumulative live spend ≈ $0.10 across every probe/run (incl. the 2026-09-01 09:48 run).
- "~90 seconds": stage wall-clock sums of real runs, 50–85s.
- "154 tests / fresh clone / no key": re-verified via literal clone to a
  temp dir + new venv on 2026-09-01 — `154 passed`.
- "3 bugs in Solari's stack": VM clock skew breaking TLS; numpy/kernel
  C-ABI trap; gateway version skew (428) — all in README.
- "recorded and replayable": 15 recorded sessions in the clean run;
  replay retrieval verified in the GRE-3459 spike.
- Outage run published quotes + headlines via HTTP fallbacks; earnings
  (browser-only sources) sat out — wording "for what it could" reflects
  this.
- "zero warnings": runs/2026-09-01 post-SDK-upgrade run, warnings: 0.
- "one evening": Linear project GRE-3459→3464, created and shipped
  2026-08-31 evening.
