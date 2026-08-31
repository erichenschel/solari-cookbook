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
fixtures/               # one realistic fixture per contract
tests/
  test_contracts.py      # hermetic: fixtures round-trip, invalid samples rejected
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
