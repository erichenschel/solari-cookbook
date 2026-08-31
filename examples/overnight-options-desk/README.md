# Overnight options desk

**Status:** foundation spike (GRE-3459) + models lane (GRE-3461). The
scraper and brief-assembly lanes are still spike-only; the quant models lane
is a runnable, schema-valid pipeline stage.

## What this will become

An overnight pipeline that:
1. Scrapes a universe of symbols for earnings dates, headlines, and quotes
   (cloud browser) into `scraped_data` (`desk/contracts.py`,
   `desk/schemas/scraped_data.schema.json`).
2. Runs quant models (GARCH vol forecast, OU mean-reversion, momentum) in a
   sandbox and emits `signals` (`desk/schemas/signals.schema.json`).
3. Assembles a morning brief from both, previewable via a sandbox port
   preview while it's being built.

This spike scaffolds the client wrapper and the two contracts so the four
lanes can build against a stable interface instead of each hand-rolling
Solari SDK calls.

## Layout

```
desk/
  solari_client.py   # open_browser_page / run_in_sandbox / serve_preview
  contracts.py         # dataclasses + load/validate for both contracts
  schemas/              # JSON Schema for scraped_data and signals
  models.py            # models-lane CLI: scraped_data.json -> signals.json
  model_code/            # uploaded into the sandbox AND imported locally
    prices.py             # pure CSV/JSON parsing -> list[float] closes
    signals.py            # GARCH / OU(AR1) / momentum / verdict rule table
    fetch.py               # network: Stooq primary, Yahoo chart fallback
    driver.py               # sandbox-side glue: fetch + signals -> signals.json body
    runner.py                # subprocess entry point (fresh interpreter, see "Live sandbox findings")
fixtures/
  scraped_data.json    # one realistic scraped_data fixture
  signals.json          # one realistic signals fixture
  prices/                 # seeded synthetic daily-close CSVs for hermetic model tests
tests/
  test_contracts.py      # hermetic: fixtures round-trip, invalid samples rejected
  test_models.py           # hermetic: model_code functions vs bundled price fixtures
  test_live_desk.py      # live: browser, sandbox, preview, recording probe
```

## Run

```bash
cd solari-cookbook
python3 -m venv .venv-desk
.venv-desk/bin/pip install -r examples/overnight-options-desk/requirements.txt

# hermetic — no network, no key needed
.venv-desk/bin/pytest examples/overnight-options-desk/tests -m "not live" -q

# live — needs SOLARI_API_KEY, hits the real free-tier API, sequential
set -a; source .env; set +a
.venv-desk/bin/pytest examples/overnight-options-desk/tests -m live -q

# models lane: scraped_data.json -> signals.json, one sandbox session (live)
set -a; source .env; set +a
python -m desk.models --scraped fixtures/scraped_data.json --out /tmp/signals.json
```

## Model rule table (GRE-3461)

`desk/model_code/` runs entirely inside one Solari sandbox session
(`run_in_sandbox`, one `pip install -q numpy arch statsmodels` up front) and
computes, per symbol in `scraped_data.universe`:

- **GARCH(1,1) vol forecast** (`arch`, zero-mean, normal innovations, fit on
  percent daily returns) — 1-day-ahead forecast (`garch_vol_forecast_1d`)
  and its `sqrt(252)`-annualized version (`garch_vol_forecast_ann`).
- **Ornstein-Uhlenbeck mean reversion via AR(1)** (`statsmodels` OLS on
  `log(P_t) = c + phi*log(P_t-1) + e_t`, the standard OU discretization) —
  `ou_zscore` (current log-price's distance from the fitted long-run mean,
  in residual-std units) and `ou_half_life_d` (`ln(0.5)/ln(phi)`).
- **5-day momentum** (`momentum_5d`) — plain `close[t]/close[t-5] - 1`.

All formulas are textbook/public (a GARCH(1,1) forecast, an AR(1)/OU fit, an
N-day percent change) — nothing here encodes proprietary signal logic.

### Verdict rule table

Applied top-to-bottom, first match wins (`desk/model_code/signals.py`,
`decide_verdict`):

| # | Condition | Verdict | Research label (in `notes[]`) |
|---|-----------|---------|-------------------------------|
| 1 | Fewer than 60 trading days of price history | `avoid` | `insufficient-data` |
| 2 | Earnings date within 3 calendar days of `as_of` | `avoid` | `event-risk` — vol forecast likely understates the actual move |
| 3 | Annualized vol forecast ≥ 35% **and** \|OU z-score\| ≥ 1.5 | `avoid` | `mean-reversion-watch` — stretched vs. fitted mean, elevated forecast vol |
| 4 | Annualized vol forecast < 20% **and** 5d momentum ≥ +2% | `bullish` | `trend-watch` (up) |
| 5 | Annualized vol forecast < 20% **and** 5d momentum ≤ -2% | `bearish` | `trend-watch` (down) |
| 6 | None of the above | `neutral` | `no-strong-signal` |

**Research verdicts only** — these labels describe what a human analyst
would flag for further reading (a stretched mean-reversion setup, a
low-vol trend, an upcoming earnings date), not a trade recommendation or
order/execution instruction (NG-1).

**CONTRACT GAP (flagged, not fixed by this ticket):**
`signals.schema.json`'s `verdict` field is a closed enum
(`bullish|bearish|neutral|avoid`) inherited from the GRE-3459 spike. It
predates this ticket's research-verdict vocabulary and doesn't literally
contain `insufficient-data`, `mean-reversion-watch`, `trend-watch`, or
`event-risk` — including AC-3's literal `verdict: "insufficient-data"`. Per
the ticket's instructions, `desk/contracts.py` and the schemas were left
untouched; instead each rule above maps onto the closest existing enum
value and the literal research label is always the corresponding
`notes[]` entry (see table). A follow-up should extend the enum (or split
`verdict` into a coarse enum + a `label` string) so `verdict` can carry the
literal AC-3 string. `bullish`/`bearish`/`avoid` as enum *names* also
predate NG-1 and read closer to trading-advice language than the research
labels above — worth revisiting in the same follow-up.

### Numerical edge cases (NG-5)

Every model function degrades to a documented fallback instead of raising:

- **GARCH fit failure or non-convergence** (too little data, degenerate/zero
  variance, optimizer non-convergence) → falls back to annualized
  sample-std vol, with a `garch-fit-failed`/`garch-skipped`/
  `garch-non-convergence` note.
- **OU/AR(1) fit failure** (too little data, or — for a genuinely constant
  price series — the OLS design matrix loses rank: `statsmodels.add_constant`
  detects the already-constant predictor and skips adding a duplicate
  constant column, so `model.params` has one entry instead of two) → falls
  back to `zscore=0, half_life_d=0`, with an `ou-fit-failed`/`ou-skipped`
  note.
- **Fitted AR(1) `phi` outside `(0, 1)`** (not clearly mean-reverting over
  the window) → clamped into `[0.01, 0.99]` for a finite half-life, with an
  `ou-ar1-phi-out-of-range` note.
- **Zero or one price points** (fetch failure) → all numeric fields `0.0`,
  verdict `avoid`, `insufficient-data` note — never a crash.

Every one of these is covered by a bundled `fixtures/prices/*.csv` fixture
and a hermetic test in `tests/test_models.py` (`CONST.csv` for the
zero-variance/collinearity case, `SHORT.csv` for the trading-days floor).

### Live sandbox findings (GRE-3461)

Two real infrastructure quirks surfaced during live AC-1 testing, both
worked around in `desk/model_code/` rather than papered over:

- **Sandbox VM clock can be stuck in the past.** Observed ~4 weeks behind
  real time; `date -s` inside the VM silently no-ops (kernel clock-set
  syscalls appear blocked in the container), so it can't be fixed in-guest.
  A stale clock makes legitimately-valid HTTPS certs look "not yet valid"
  (`CERTIFICATE_VERIFY_FAILED`) — this broke every fetch until diagnosed.
  `fetch.py`'s `_urlopen_tolerant` retries once, without cert verification,
  ONLY on that exact error signature (not TLS failures generally), and
  always leaves a `tls-clock-skew-workaround` note so the degraded-trust
  request is visible in the output rather than silent.
- **Installing `numpy` explicitly breaks `pandas`'s C extensions.** The
  `base` template's preinstalled `scipy` is pinned to `numpy<1.27`; asking
  pip for `numpy` as a top-level package (rather than letting it resolve
  transitively as `arch`/`statsmodels`'s own dependency) pulls the newest
  numpy (2.x) instead, silently breaking scipy/pandas' compiled ABI — surfacing
  as `ImportError: C extension: None not built` deep inside the GARCH fit,
  not as an install-time error. Worse: even with `numpy` correctly left off
  the install list, the sandbox's persistent Python kernel process (the one
  `run_code` executes in) already has numpy imported from kernel startup —
  a `pip install` afterward only changes what's on disk, not what's already
  bound in the kernel's `sys.modules`, so pandas' C extensions get checked
  against a stale in-process numpy regardless. `desk/model_code/runner.py`
  is the fix: `desk/models.py`'s bootstrap code installs `arch`+`statsmodels`
  (letting pip's resolver pick a mutually-compatible numpy/scipy/pandas —
  observed numpy 1.26.4 / scipy 1.10.1 / pandas 3.0.5), then runs
  `runner.py` as a **fresh subprocess** rather than importing `driver` in
  the kernel's own process — a brand-new interpreter has no stale numpy to
  conflict with anything just installed.

## Spike findings

- **Port preview: works on free tier.** `sandbox.preview_url(port)` returns
  a `*.preview.getsolari.com` URL that is reachable from the open internet
  immediately after the backgrounded `python3 -m http.server` binds — no
  extra allowlisting or plan flag needed. `test_port_preview_serves_static_file_and_is_publicly_reachable`
  confirms an HTTP 200 fetched from the local machine, outside the VM.
- **Recording: works on free tier, and fast.** `test_recording_availability_probe`
  creates a `recording=True` session, releases it, and polls
  `solari.sessions.download_replay()` for up to ~30s. It always passes — it
  reports the finding rather than asserting on it. Actual results from this
  spike's live runs (two separate runs, both against the real API):

  ```
  [finding] recording probe: replay_available=True (2320 bytes after 3.6s (attempt 1))
  [finding] recording probe: replay_available=True (2320 bytes after 10.0s (attempt 3))
  ```

  The replay was retrievable well inside the ~30s the root README's
  gotcha warns you to poll for — no 404s that didn't quickly resolve.

- **SDK deviations from the ticket packet, confirmed against the installed
  `solari-browser==0.1.2` / `solari-sandbox==0.2.0` source:**
  - Python's `Solari.close()` (browser client) is the real equivalent of the
    TS root-README gotcha "call `await solari.close()` or the process
    hangs." The Python cookbook examples never call it because they exit
    right after their `async def main()` returns, which masked this for the
    ticket packet — a long-lived process (like a test suite, or the future
    desk pipeline) should call it explicitly. `desk/solari_client.py` does,
    in every helper's `finally`.
  - The Python sandbox SDK's `commands.run(..., background=True)` is a
    first-class kwarg — `run_in_sandbox`/`serve_preview` don't need the
    `nohup ... &` shell trick the TS `sandbox-port-preview-ts` example uses
    to background a server.
  - `sandbox.preview_url(port)` (Python) / `sandbox.previewUrl(port)` (TS)
    returns `{"url": ..., "token"?: ...}` — matches the ticket's assumed
    shape.
  - The `base` sandbox template's kernel resolved `pip install -q numpy` in
    ~3s live (either it's already warm/cached on the image, or the install
    itself is just fast) — the whole kernel run (install + import + two
    prints) completed in 3.2s wall time, well inside the 2-minute per-test
    budget. `run_in_sandbox`/the test install it defensively regardless
    rather than assuming it's preinstalled.
  - `commands.run(..., background=True)` reliably backgrounds
    `python3 -m http.server`: the live preview test got HTTP 200 from the
    public URL on the very first fetch (no retry needed), i.e. the server
    was already bound by the time `preview_url()` returned.

## Gotchas this wrapper encodes

See the repo root [README.md](../../README.md#gotchas-the-examples-encode)
for the canonical list. `desk/solari_client.py` docstrings point back to the
specific gotcha each `finally` block is defending against.
