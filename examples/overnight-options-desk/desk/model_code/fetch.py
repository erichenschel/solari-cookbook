"""fetch.py — network fetch of ~1y daily closes per symbol. Stooq CSV
primary, Yahoo chart endpoint fallback. Plain HTTP (stdlib `urllib`, no
extra dependency) — this file runs INSIDE the sandbox only; it is not
exercised by the hermetic test suite (no network in hermetic tests), which
instead calls `prices.parse_stooq_csv` directly against bundled fixtures.

Flat module (no `desk.*` imports) so it uploads verbatim into the sandbox
and is imported there as a top-level `fetch` module by `driver.py`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from prices import parse_stooq_csv, parse_yahoo_chart  # flat import, sandbox-side

STOOQ_URL = "https://stooq.com/q/d/l/?s={sym}.us&i=d"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1y&interval=1d"
REQUEST_TIMEOUT_S = 15
MIN_USABLE_CLOSES = 5  # below this, treat the source as having failed


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "solari-cookbook-desk/1.0"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
        return resp.read()


def fetch_from_stooq(symbol: str) -> list[float]:
    text = _get(STOOQ_URL.format(sym=symbol.lower())).decode("utf-8", errors="replace")
    if text.strip().upper().startswith("N/D") or not text.strip():
        raise ValueError("stooq returned no data (N/D or empty body)")
    return parse_stooq_csv(text)


def fetch_from_yahoo(symbol: str) -> list[float]:
    payload = json.loads(_get(YAHOO_URL.format(sym=symbol)).decode("utf-8"))
    return parse_yahoo_chart(payload)


def fetch_daily_closes(symbol: str) -> tuple[list[float], str, list[str]]:
    """Fetch ~1y of daily closes for `symbol`. Returns (closes, source,
    notes). Tries Stooq first; falls back to Yahoo on any failure or a
    too-short result; never raises — an empty list on total failure lets
    `signals.compute_symbol_signal` degrade to its insufficient-data path
    (NG-5) instead of crashing the whole run."""
    notes: list[str] = []
    try:
        closes = fetch_from_stooq(symbol)
        if len(closes) >= MIN_USABLE_CLOSES:
            return closes, "stooq", notes
        notes.append(f"stooq-thin: only {len(closes)} closes, trying yahoo fallback")
    except (urllib.error.URLError, ValueError, TimeoutError) as exc:
        notes.append(f"stooq-failed: {exc}; trying yahoo fallback")

    try:
        closes = fetch_from_yahoo(symbol)
        if len(closes) >= MIN_USABLE_CLOSES:
            return closes, "yahoo", notes
        notes.append(f"yahoo-thin: only {len(closes)} closes")
        return closes, "yahoo", notes
    except (urllib.error.URLError, ValueError, TimeoutError) as exc:
        notes.append(f"yahoo-failed: {exc}; no price history available")
        return [], "none", notes
