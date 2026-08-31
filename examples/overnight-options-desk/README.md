# Overnight options desk (spike)

**Status: spike (GRE-3459).** This is the foundation four parallel lanes
(scraper, quant models, brief assembly, and whatever else the desk project
needs) will build on top of. It is not itself a runnable desk — it's a thin
Solari client wrapper, two data contracts, and smoke tests proving the free
tier can do what the desk needs.

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
  contracts.py        # dataclasses + load/validate for both contracts
  schemas/             # JSON Schema for scraped_data and signals
  run_overnight.py     # orchestrator: scrape -> models -> render -> serve (GRE-3463)
  stubs.py              # fixture-derived stand-ins for the sibling lanes, used by
                        #   --dry-run / --stubs / DESK_STUBS=1
fixtures/               # one realistic fixture per contract
runs/                   # per-day run artifacts (gitignored; .gitkeep only)
tests/
  test_contracts.py      # hermetic: fixtures round-trip, invalid samples rejected
  test_orchestrator.py    # hermetic: sequencing, --resume, partial-failure degrade
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
```

## Orchestrator (`desk/run_overnight.py`, GRE-3463)

`run_overnight.py` is the single entry point that chains the four stages
end to end, with stage checkpointing, structured logging, budget
accounting, and a fully offline `--dry-run` mode. Each stage opens and
kills its own Solari sessions — never held across stages — and each stage's
artifact is persisted to `runs/<YYYY-MM-DD>/` (`scraped_data.json`,
`signals.json`, `brief.html`, `run.log`, `budget.json`, `state.json`).

```bash
cd examples/overnight-options-desk

# hermetic — zero API calls, all four stages run against stubs
python -m desk.run_overnight --dry-run --symbols AAPL,NVDA,MSFT,TSLA

# live chain, once the sibling scraper/models/brief/serve CLIs exist
python -m desk.run_overnight --symbols AAPL,NVDA,MSFT,TSLA

# resume a partial run, skipping stages whose valid artifacts already exist
python -m desk.run_overnight --resume runs/2026-08-31
```

A stage that fails is retried once, then the run continues **degraded**:
downstream stages render whatever inputs exist (the brief always renders,
even with a missing `signals.json`), and the run is marked `partial` in
`state.json` / the printed summary instead of aborting. The final step
copies `brief.html` to `docs/latest/index.html` and prints a summary —
per-stage status/timing, total estimated spend, and the preview URL when
`serve` succeeded.

`--stubs` (or `DESK_STUBS=1`) uses the fixture-derived stand-ins in
`desk/stubs.py` instead of shelling out to the real sibling CLIs;
`--dry-run` implies `--stubs` and is the mode the hermetic tests exercise.
The exact sibling CLI contracts this orchestrator assumes (subject to
reconciliation once each lane ships):

- `python -m desk.scraper --symbols A,B --out path` — writes `scraped_data.json`
- `python -m desk.models --scraped path --out path` — writes `signals.json`
- `python -m desk.brief --scraped path --signals path --out path` — writes `brief.html`
- `python -m desk.serve --file path` — prints a preview URL as the last stdout line

Each is invoked as its own subprocess (a fresh process per stage — the
NG-1 "no session reuse across stages" guarantee falls out of that for
free). Spend is read from `[spend] resource: Ns ~= $X` lines emitted by
`desk/solari_client.py`'s helpers (summed across every resource a stage
opened), or from a trailing `{"spend_usd": ...}` line if a stage
self-reports instead.

### Scheduling

This ticket is orchestration only — no daemon or scheduler is implemented.
Point either of these at `run_overnight.py` from outside the process:

**cron** (add via `crontab -e`, runs at 5:00 AM local time every weekday):

```cron
0 5 * * 1-5 cd /path/to/solari-cookbook/examples/overnight-options-desk && \
  /path/to/solari-cookbook/.venv-desk/bin/python -m desk.run_overnight \
  --symbols AAPL,NVDA,MSFT,TSLA >> runs/cron.out 2>&1
```

**launchd** (macOS — save as
`~/Library/LaunchAgents/com.solari.overnight-desk.plist`, then
`launchctl load ~/Library/LaunchAgents/com.solari.overnight-desk.plist`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.solari.overnight-desk</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/solari-cookbook/.venv-desk/bin/python</string>
    <string>-m</string><string>desk.run_overnight</string>
    <string>--symbols</string><string>AAPL,NVDA,MSFT,TSLA</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/path/to/solari-cookbook/examples/overnight-options-desk</string>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>5</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key><string>runs/launchd.out</string>
  <key>StandardErrorPath</key><string>runs/launchd.err</string>
</dict>
</plist>
```

Both examples assume `SOLARI_API_KEY` is available to the scheduler's
environment (cron and launchd don't source your shell profile — export it
in the crontab/plist directly, or have `run_overnight.py`'s subprocess
stages load it via `.env` as `desk/solari_client.py` already does for
in-process calls).

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
