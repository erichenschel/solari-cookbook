"""fetch.py — network fetch of ~1y daily closes per symbol. Stooq CSV
primary, Yahoo chart endpoint fallback. Plain HTTP (stdlib `urllib`, no
extra dependency) — this file runs INSIDE the sandbox only; it is not
exercised by the hermetic test suite (no network in hermetic tests), which
instead calls `prices.parse_stooq_csv` directly against bundled fixtures.

Flat module (no `desk.*` imports) so it uploads verbatim into the sandbox
and is imported there as a top-level `fetch` module by `driver.py`.

SANDBOX FINDING (GRE-3461 live testing): the sandbox VM's system clock can
be stuck in the past relative to real time (observed ~4 weeks behind; `date
-s` inside the VM silently no-ops, so it can't be corrected in-guest). That
makes a legitimately valid HTTPS certificate look "not yet valid" from the
VM's own clock — every real symbol's fetch failed with
`CERTIFICATE_VERIFY_FAILED: certificate is not yet valid` until this was
diagnosed. `_urlopen_tolerant` below retries, once, without cert
verification — ONLY for that exact error signature, not TLS failures in
general — and always leaves a note behind so the degraded trust mode is
visible in the output, never silent.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request

from prices import parse_stooq_csv, parse_yahoo_chart  # flat import, sandbox-side

STOOQ_URL = "https://stooq.com/q/d/l/?s={sym}.us&i=d"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1y&interval=1d"
REQUEST_TIMEOUT_S = 15
MIN_USABLE_CLOSES = 5  # below this, treat the source as having failed


def _is_cert_not_yet_valid(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, ssl.SSLCertVerificationError) and "not yet valid" in str(reason)


def _urlopen_tolerant(url: str) -> tuple[bytes, list[str]]:
    """GET `url`. On a normal response, returns (body, []). On the specific
    sandbox-clock-skew cert error described above, retries once without
    verification and returns (body, [note]). Any other failure propagates."""
    req = urllib.request.Request(url, headers={"User-Agent": "solari-cookbook-desk/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            return resp.read(), []
    except urllib.error.URLError as exc:
        if not _is_cert_not_yet_valid(exc):
            raise
        unverified_ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S, context=unverified_ctx) as resp:
            body = resp.read()
        return body, [
            "tls-clock-skew-workaround: sandbox VM clock appears stale "
            "(cert reported 'not yet valid'); retried this request without "
            "certificate verification — see fetch.py docstring"
        ]


def fetch_from_stooq(symbol: str) -> tuple[list[float], list[str]]:
    body, notes = _urlopen_tolerant(STOOQ_URL.format(sym=symbol.lower()))
    text = body.decode("utf-8", errors="replace")
    if text.strip().upper().startswith("N/D") or not text.strip():
        raise ValueError("stooq returned no data (N/D or empty body)")
    return parse_stooq_csv(text), notes


def fetch_from_yahoo(symbol: str) -> tuple[list[float], list[str]]:
    body, notes = _urlopen_tolerant(YAHOO_URL.format(sym=symbol))
    payload = json.loads(body.decode("utf-8"))
    return parse_yahoo_chart(payload), notes


def fetch_daily_closes(symbol: str) -> tuple[list[float], str, list[str]]:
    """Fetch ~1y of daily closes for `symbol`. Returns (closes, source,
    notes). Tries Stooq first; falls back to Yahoo on any failure or a
    too-short result; never raises — an empty list on total failure lets
    `signals.compute_symbol_signal` degrade to its insufficient-data path
    (NG-5) instead of crashing the whole run."""
    notes: list[str] = []
    try:
        closes, tls_notes = fetch_from_stooq(symbol)
        notes.extend(tls_notes)
        if len(closes) >= MIN_USABLE_CLOSES:
            return closes, "stooq", notes
        notes.append(f"stooq-thin: only {len(closes)} closes, trying yahoo fallback")
    except (urllib.error.URLError, ValueError, TimeoutError) as exc:
        notes.append(f"stooq-failed: {exc}; trying yahoo fallback")

    try:
        closes, tls_notes = fetch_from_yahoo(symbol)
        notes.extend(tls_notes)
        if len(closes) >= MIN_USABLE_CLOSES:
            return closes, "yahoo", notes
        notes.append(f"yahoo-thin: only {len(closes)} closes")
        return closes, "yahoo", notes
    except (urllib.error.URLError, ValueError, TimeoutError) as exc:
        notes.append(f"yahoo-failed: {exc}; no price history available")
        return [], "none", notes
