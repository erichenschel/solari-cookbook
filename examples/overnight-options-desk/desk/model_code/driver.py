"""driver.py — sandbox-side glue: fetch + signals -> a full signals.json
body. Runs INSIDE the sandbox kernel only (uploaded flat alongside
`prices.py`, `signals.py`, `fetch.py` and imported there as a top-level
module by the bootstrap code `desk/models.py` writes into the kernel).

Not imported by the hermetic test suite (it needs live network via
`fetch.py`) — only `prices` and `signals` are exercised hermetically.
"""

from __future__ import annotations

from datetime import datetime, timezone

import fetch  # flat import, sandbox-side
import signals  # flat import, sandbox-side


def build_signals(scraped_data: dict) -> dict:
    """Fetch prices for every symbol in `scraped_data["universe"]` and
    compute a full `signals` contract body. Per-symbol failures degrade to
    an insufficient-data verdict + notes rather than aborting the run
    (NG-5) — one bad fetch must not sink the other four symbols."""
    universe = scraped_data.get("universe", [])
    earnings = scraped_data.get("earnings", [])
    per_symbol: dict[str, dict] = {}

    for symbol in universe:
        try:
            closes, source, fetch_notes = fetch.fetch_daily_closes(symbol)
            signal = signals.compute_symbol_signal(
                symbol, closes, earnings, scraped_data.get("as_of", _now_iso())
            )
            signal["notes"] = fetch_notes + [f"price-source: {source}"] + signal["notes"]
        except Exception as exc:  # noqa: BLE001 - last-resort guard, NG-5
            signal = signals.compute_symbol_signal(symbol, [], earnings, scraped_data.get("as_of", _now_iso()))
            signal["notes"] = [f"driver-error: {exc}"] + signal["notes"]
        per_symbol[symbol] = signal

    return {"as_of": _now_iso(), "per_symbol": per_symbol}


def _now_iso() -> str:
    # GRE-3464: this reads the SANDBOX VM's clock, which live testing found
    # can be stuck weeks in the past (see fetch.py's TLS clock-skew note) —
    # so the top-level `as_of` this produces is a best-effort placeholder
    # only. desk/models.py (host-side, trustworthy clock) overwrites it
    # before signals.json is written; don't rely on this value elsewhere.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
