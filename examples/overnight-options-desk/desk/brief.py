"""desk/brief.py — render `scraped_data` + `signals` into one self-contained
`brief.html`.

Pure and hermetic (NG-3): bytes in (two validated contract objects) -> one
HTML string out. No Solari calls live here — `desk/serve.py` is the only
module in this lane that touches the API. No external requests from the
rendered page either (NG-1): no CDN fonts, no analytics, no JS at all —
structure is plain HTML tables/sections plus inline SVG bars (NG-2, no
charting framework), so the page is fully readable with JS disabled and from
a bare `file://` URL.
"""

from __future__ import annotations

import argparse
import re
from collections import Counter, OrderedDict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from jinja2 import Environment

from desk.contracts import Earnings, Headline, ScrapedData, Signals, SymbolSignal, load_scraped, load_signals

DISCLAIMER = "Research only — not investment advice."
_ALLOWED_URL_SCHEMES = {"http", "https"}

# GRE-3464: "Earnings in window" is presentation, not the raw record —
# scraped_data.json's earnings[] intentionally keeps a short look-back
# (desk/scraper.py's EARNINGS_WINDOW is -7d..+180d, so a source can
# legitimately report "reported 5 days ago") and can carry more than one
# row per symbol (e.g. yahoo's calendar page listing both a recent past
# report and a further-out estimate). The brief should only ever show what
# an analyst reading it *this morning* would call "upcoming" -- one row per
# symbol, never a date that's already happened. Filtering lives here
# (not in scraper.py) so scraped_data.json stays the unfiltered raw record.
EARNINGS_DISPLAY_WINDOW_DAYS = 90

# Visual scale caps for the inline SVG bars — clipped, not truncated data: a
# value beyond the cap still renders (full bar + exact number in the cell),
# it just stops growing the bar past 100% width. `_ZSCORE_SCALE` additionally
# gets a small overflow marker past the cap (see `_zscore_bar_svg`).
# Was 3.0, which every real reading exceeded: on 2026-08-31 the five ranked
# symbols scored +6.48/+4.78/+4.72/+3.71/-3.45, so all five bars clipped to full
# width and four were pixel-identical — the column looked like an encoding and
# carried no information. 8.0 keeps a fixed run-over-run scale (same argument as
# _VOL_CAP_ANN) while leaving real spread visible.
_ZSCORE_SCALE = 8.0
_ZSCORE_STRETCH = 1.5  # mirrors model_code/signals.py's Z_STRETCH — display-only threshold, kept local so brief.py stays decoupled from the sandbox-side model code

# GRE-3464 restyle: bar fill is a MAGNITUDE reading only (inside vs. beyond
# the stretch/elevated threshold) — never a direction/above-below reading.
# Direction is still fully legible from bar position (left/right of the zero
# axis) and from the aria-label text; encoding it a second time in hue would
# read as buy/sell (green/red) or invent a second color axis nobody asked
# for. One accent color, reused for both bars' "worth a second look" state.
_BAR_TRACK = "#F1F0EE"
_ZBAR_MUTED = "#C9C7C3"  # neutral fill — inside the stretch/elevated threshold
_ZBAR_STRETCHED = "#956400"  # pale-yellow text tone — stretched (z) / elevated (vol)
_VOL_CAP_ANN = 0.60  # fixed annualized-vol scale so bars are comparable run over run, not just within one brief

# GRE-3464: the same rule-table thresholds `decide_verdict` uses, kept local
# (not imported from model_code/signals.py) for the same NG-3 purity reason
# as `_ZSCORE_STRETCH` above — this module only ever reads the *rendered*
# verdict/label, never the model code. Used to generate the plain-English
# per-symbol interpretation sentence and the TL;DR strip below.
_VOL_HIGH_ANN = 0.35  # mirrors model_code/signals.py's VOL_HIGH_ANN
_VOL_LOW_ANN = 0.20  # mirrors model_code/signals.py's VOL_LOW_ANN
_MOMENTUM_POS = 0.02  # mirrors model_code/signals.py's MOMENTUM_POS
_MOMENTUM_NEG = -0.02  # mirrors model_code/signals.py's MOMENTUM_NEG
_EARNINGS_SOON_WINDOW_DAYS = 3  # mirrors model_code/signals.py's EARNINGS_WINDOW_DAYS
_VERDICT_TALLY_ORDER = ("avoid", "bearish", "bullish", "neutral")

HEADLINE_CAP = 5  # GRE-3464: max headlines shown per symbol/Market-wide group


def _fmt_dt(iso: str) -> str:
    """'2026-08-31T06:00:00Z' -> '2026-08-31 06:00 UTC'. Falls back to the
    raw string if it doesn't parse — never raise on display formatting."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, AttributeError):
        return iso


def _fmt_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return iso


def _parse_dt(iso: str) -> datetime:
    """Best-effort parse for sort ordering only — never raises. Unparseable
    timestamps sort last (oldest) rather than crashing the triage."""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _safe_url(url: str) -> str:
    """Only ever emit an http(s) href — anything else (javascript:, data:,
    a malformed string) collapses to '#' rather than being trusted as-is."""
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        return "#"
    return url if scheme in _ALLOWED_URL_SCHEMES else "#"


def _session_label(session: str) -> str:
    return {"bmo": "Before open", "amc": "After close", "unknown": "Unknown"}.get(
        session, session
    )


def _verdict_class(verdict: str) -> str:
    return {
        "bullish": "v-bullish",
        "bearish": "v-bearish",
        "avoid": "v-avoid",
        "neutral": "v-neutral",
    }.get(verdict, "v-neutral")


def _pct(x: float, digits: int = 2) -> str:
    return f"{x * 100:+.{digits}f}%"


def _zscore_bar_svg(z: float) -> str:
    """Horizontal bar centered at 0, clipped to +/- _ZSCORE_SCALE with a
    small overflow marker (arrow) past the cap. Direction (above/below the
    fitted mean) reads from bar position and the aria-label text only; fill
    color is a MAGNITUDE reading — the accent color when |z| is "stretched"
    (>= _ZSCORE_STRETCH), neutral grey otherwise — never green/red, and
    never direction-coded: this is a magnitude reading, not a buy/sell
    signal (GRE-3464)."""
    width, height, center = 100, 14, 50
    overflow = abs(z) > _ZSCORE_SCALE
    clipped = max(-_ZSCORE_SCALE, min(_ZSCORE_SCALE, z))
    half = clipped / _ZSCORE_SCALE * center
    color = _ZBAR_STRETCHED if abs(z) >= _ZSCORE_STRETCH else _ZBAR_MUTED
    x = center if half >= 0 else center + half
    w = abs(half)
    marker = ""
    if overflow:
        arrow, marker_x, anchor = (
            ("&#9656;", width - 3, "end") if z >= 0 else ("&#9666;", 3, "start")
        )
        marker = (
            f'<text x="{marker_x}" y="{height - 3}" font-size="9" '
            f'fill="{color}" text-anchor="{anchor}">{arrow}</text>'
        )
    # +/- _ZSCORE_STRETCH ticks: make "stretched" a position the eye can read off
    # the axis, not a rule you have to already know from the colour change.
    tick_off = _ZSCORE_STRETCH / _ZSCORE_SCALE * center
    ticks = "".join(
        f'<line x1="{center + s * tick_off:.1f}" y1="1" '
        f'x2="{center + s * tick_off:.1f}" y2="{height - 1}" '
        f'stroke="#B9B7B2" stroke-width="1" stroke-dasharray="2 2"/>'
        for s in (-1, 1)
    )
    return (
        f'<svg class="bar zbar" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="z-score {z:+.2f}, '
        f'{"above" if z >= 0 else "below"} the fitted mean'
        f'{", stretched" if abs(z) >= _ZSCORE_STRETCH else ""}'
        f'{" (beyond +/-" + str(_ZSCORE_SCALE) + " scale)" if overflow else ""}">'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="{_BAR_TRACK}" rx="2"/>'
        f"{ticks}"
        f'<line x1="{center}" y1="0" x2="{center}" y2="{height}" stroke="#C9C7C3" stroke-width="1"/>'
        f'<rect x="{x:.1f}" y="2" width="{w:.1f}" height="{height - 4}" fill="{color}" rx="1"/>'
        f"{marker}"
        f"</svg>"
    )


def _vol_bar_svg(vol_ann: float) -> str:
    """Annualized-vol bar against the fixed `_VOL_CAP_ANN` scale (clipped
    past the cap, same convention as the z-score bar). Same magnitude-only
    accent as the z-score bar: the fill turns from neutral to the accent
    color once the forecast reaches `_VOL_HIGH_ANN` ("elevated"), rather
    than growing more alarming the whole way up the scale."""
    width, height = 100, 14
    frac = max(0.0, min(1.0, vol_ann / _VOL_CAP_ANN))
    w = frac * width
    color = _ZBAR_STRETCHED if vol_ann >= _VOL_HIGH_ANN else _ZBAR_MUTED
    return (
        f'<svg class="bar volbar" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="annualized vol forecast {vol_ann:.1%}">'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="{_BAR_TRACK}" rx="2"/>'
        f'<rect x="0" y="2" width="{w:.1f}" height="{height - 4}" fill="{color}" rx="1"/>'
        f"</svg>"
    )


def _half_life_note(days: float) -> str:
    """Qualitative read on an OU half-life, relative to the 5-day momentum
    window the rest of the brief uses. A bare '46.8' tells a reader nothing
    about whether the reversion is reachable inside their horizon; 'slow'
    does. Boundaries are the horizon itself and 4x it."""
    if days <= 5:
        return "within 5d window"
    if days <= 20:
        return "beyond 5d window"
    return "slow — weeks out"


def _momentum_arrow(m: float) -> str:
    if m > 0:
        return f'<span class="mom mom-up">&#9650; {_pct(m)}</span>'
    if m < 0:
        return f'<span class="mom mom-down">&#9660; {_pct(m)}</span>'
    return f'<span class="mom mom-flat">&#8212; {_pct(m)}</span>'


# Dedupe the raw per-symbol failure log into one summary line per distinct
# failure pattern (GRE-3464) — a 5-symbol run hitting the same dead source
# used to render as five near-identical amber paragraphs (a Playwright stack
# trace each) stacked after the signal table.

_SOURCE_FAILURE_RE = re.compile(
    r"^(?P<source>[a-z0-9_]+) (?:failed for|forced unreachable for) "
    r"(?P<symbol>[A-Z][A-Z0-9.]*)(?:: (?P<detail>.*))?$",
    re.DOTALL,
)
_GENERIC_SYMBOL_RE = re.compile(r"\bfor ([A-Z][A-Z0-9.]*)\b")
_NET_ERR_RE = re.compile(r"net::ERR_[A-Z0-9_]+")
_URL_RE = re.compile(r"https?://[^\s\"']+")

# GRE-3464: the Solari browser gateway itself can go down (a connect-level
# / session-launch failure) before any page is even requested — see the
# `open_browser_page` helper (BROWSER_LAUNCH_RETRIES) in the module that
# wraps the Solari browser SDK, and this module's own docstring for the
# live incident this was built from. That is a
# failure of the *platform*, not of the data source being scraped, so it
# must never render as e.g. "yahoo_news blocked for MSFT" (blames the
# source) and must never flood the page with one row per fetch (every
# instance carries a unique session id in its WebSocket URL, so naive
# signature-based dedup treats each one as distinct). Matched on the raw
# warning text emitted by desk/scraper.py's `_try_source`:
#   - "BrowserType.connect: ..." — patchright/Playwright's own connect-
#     failure exception, raised by solari_browser's `Solari.launch()` after
#     `BROWSER_LAUNCH_RETRIES` attempts are exhausted.
#   - "wss://api.getsolari.com/ws/..." / ".../getsolari.com/ws/" — the
#     gateway's WebSocket endpoint, present in that same exception's text.
#   - "Precondition Required" — the live incident's actual HTTP status text
#     (428 version-skew between the gateway and the published SDK).
_CONNECT_FAILURE_RE = re.compile(
    r"BrowserType\.connect|getsolari\.com/ws/|Precondition Required",
    re.IGNORECASE,
)


def _is_connect_failure(warning: str) -> bool:
    return bool(_CONNECT_FAILURE_RE.search(warning))


def _source_kind(source: str) -> str:
    """Which `scraped_data` field a source's failure would have populated —
    used to infer whether the affected (kind, symbol) pair ended up covered
    anyway, regardless of which literal source id produced the warning."""
    if "earnings" in source:
        return "earnings"
    if "quote" in source:
        return "quotes"
    return "headlines"


def _kind_covered(scraped: ScrapedData, kind: str, symbol: str) -> bool:
    if kind == "earnings":
        return any(e.symbol == symbol for e in scraped.earnings)
    if kind == "quotes":
        return symbol in scraped.quotes
    return any(h.symbol == symbol for h in scraped.headlines)


def _error_signature(detail: str) -> str:
    """A short, stable label for an error message, used to group otherwise-
    identical failures across symbols. Playwright network errors repeat the
    same `net::` code with a different per-symbol URL/call-log each time —
    prefer that code; fall back to the first line, truncated."""
    m = _NET_ERR_RE.search(detail)
    if m:
        return m.group(0)
    return detail.splitlines()[0].strip()[:80]


def _parse_warning(w: str) -> tuple[str, Optional[str], str]:
    """(source_id, symbol, signature) for grouping near-duplicate warnings.
    `source_id` is '' when the warning doesn't match the scraper's
    "SOURCE failed/forced unreachable for SYMBOL[: detail]" shape (e.g. the
    scrape-level "no quote data available for X" warning); `signature` then
    has the symbol blanked out to `{symbol}` so per-symbol repeats of that
    shape still dedupe into one group."""
    m = _SOURCE_FAILURE_RE.match(w)
    if m:
        detail = m.group("detail") or "forced unreachable (override)"
        return m.group("source"), m.group("symbol"), _error_signature(detail)
    sym_m = _GENERIC_SYMBOL_RE.search(w)
    symbol = sym_m.group(1) if sym_m else None
    signature = w.replace(symbol, "{symbol}", 1) if symbol else w
    return "", symbol, signature


def _who(symbols: list[str], universe_size: int) -> str:
    if not symbols:
        return "unknown symbol(s)"
    if len(symbols) == 1:
        return symbols[0]
    if len(symbols) == universe_size and universe_size > 1:
        return f"all {universe_size} symbols"
    return f"{len(symbols)} symbols ({', '.join(symbols)})"


def _fallback_outcome(scraped: ScrapedData, source: str, symbols: list[str]) -> str:
    """Evidence-based read on whether the affected symbols ended up covered
    anyway, inferred from `scraped_data` itself rather than importing
    `desk.scraper`'s source-chain constants (keeps this lane's pure-module
    boundary intact — see the module docstring's NG-3 note)."""
    if not symbols:
        return "outcome unknown"
    kind = _source_kind(source)
    hits = sum(1 for s in symbols if _kind_covered(scraped, kind, s))
    if hits == len(symbols):
        return "recovered via fallback"
    if hits > 0:
        return "partially recovered via fallback"
    return "no data recovered from any source"


def _connect_failure_outcome(scraped: ScrapedData, entries: list[tuple[str, Optional[str]]]) -> str:
    """Same evidence-based read as `_fallback_outcome`, generalized across a
    connect-failure group that can span multiple sources/kinds/symbols at
    once (GRE-3464) — dedup to distinct (kind, symbol) pairs first so a
    symbol that failed on two different sources isn't double-counted."""
    pairs = {(_source_kind(source), symbol) for source, symbol in entries if symbol}
    if not pairs:
        return "outcome unknown"
    hits = sum(1 for kind, symbol in pairs if _kind_covered(scraped, kind, symbol))
    if hits == len(pairs):
        return "recovered via HTTP fallbacks where available"
    if hits > 0:
        return "partially recovered via HTTP fallbacks"
    return "no data recovered via HTTP fallbacks"


def _detail_line(symbol: Optional[str], w: str) -> str:
    """One compact line per warning for the expanded view: the symbol and the
    URL that failed. The error signature already lives in the group headline,
    and the full raw text (Playwright call logs included) stays in the run's
    scraped_data.json — repeating either here is noise."""
    m = _URL_RE.search(w)
    if symbol and m:
        return f"{symbol} — {m.group(0).rstrip('\",.')}"
    first = w.split("Call log", 1)[0].splitlines()[0].strip()
    return first[:120] if len(first) <= 120 else first[:117] + "..."


def _infra_detail_line(source: str, symbol: Optional[str]) -> str:
    """Compact per-entry line for the collapsed connect-failure group — the
    source id and symbol, no raw WebSocket URL (it embeds an internal
    hostname, same hygiene rule as `_provenance_claim`'s session ids)."""
    who = symbol or "unknown symbol"
    return f"{who} — {source} (browser session failed to launch)"


def _summarize_warnings(warnings: list[str], universe_size: int, scraped: ScrapedData) -> list[dict]:
    groups: "OrderedDict[tuple[str, str], list[tuple[Optional[str], str]]]" = OrderedDict()
    infra: list[tuple[str, Optional[str], str]] = []  # (source, symbol, raw)
    for w in warnings:
        source, symbol, sig = _parse_warning(w)
        if source and _is_connect_failure(w):
            infra.append((source, symbol, w))
            continue
        groups.setdefault((source, sig), []).append((symbol, w))

    summaries = []
    if infra:
        n = len(infra)
        outcome = _connect_failure_outcome(scraped, [(source, symbol) for source, symbol, _ in infra])
        headline = (
            f"Solari browser sessions unavailable for {n} fetch{'es' if n != 1 else ''} "
            f"(gateway error) — {outcome}"
        )
        summaries.append(
            {
                "headline": headline,
                "raw": [w for _, _, w in infra],
                "details": [_infra_detail_line(source, symbol) for source, symbol, _ in infra],
                "count": n,
            }
        )

    for (source, sig), entries in groups.items():
        symbols = [s for s, _ in entries if s]
        raw = [w for _, w in entries]
        if source:
            outcome = _fallback_outcome(scraped, source, symbols)
            headline = f"{source} blocked for {_who(symbols, universe_size)} ({sig}) — {outcome}"
        elif "{symbol}" in sig:
            headline = sig.replace("{symbol}", _who(symbols, universe_size), 1)
        else:
            headline = sig
        details = [_detail_line(s, w) for s, w in entries]
        summaries.append({"headline": headline, "raw": raw, "details": details, "count": len(raw)})
    return summaries


def _provenance_claim(sessions: list[str], replays: list[str]) -> str:
    """One-line rollup for the footer — never the raw session ids: they
    embed internal hostnames (e.g. `ip-10-0-10-195:...`), so the full list
    belongs only in the scraped_data.json artifact this run produced, not
    printed twice into a public brief (GRE-3464)."""
    if not sessions:
        return "No browser sessions were recorded for this run."
    n = len(sessions)
    claim = f"Provenance: {n} recorded browser session{'s' if n != 1 else ''} back the scraped data"
    if replays:
        r = len(replays)
        eligible = "all of them" if r == n else f"{r} of them"
        claim += f" ({eligible} replayable via solari.sessions.download_replay)"
    claim += " — full ids in the run's scraped_data.json."
    return claim


def _short_id(session_id: str) -> str:
    return session_id[-8:] if len(session_id) > 8 else session_id


def _as_of_date(as_of_iso: str) -> Optional[date]:
    """Date-level (UTC) parse of `scraped.as_of` for window comparisons.
    `None` on anything unparseable — callers treat that as "can't filter,
    show nothing" rather than guessing (never crash rendering a brief)."""
    try:
        return datetime.fromisoformat(as_of_iso.replace("Z", "+00:00")).astimezone(timezone.utc).date()
    except (ValueError, AttributeError):
        return None


def _upcoming_earnings(earnings: list[Earnings], as_of_iso: str) -> list[Earnings]:
    """Filter `scraped.earnings` down to what belongs in the brief's
    "Earnings in window" section (GRE-3464): forward-looking only (the run
    day itself counts as forward-looking — a report due *today* is still
    "upcoming" to a reader of this morning's brief), out to
    `EARNINGS_DISPLAY_WINDOW_DAYS`, one row per symbol (the soonest
    upcoming date wins if a source returned more than one).

    `scraped_data.json` is left untouched — this is presentation
    filtering, not re-scraping; the raw multi-row / recent-past-report
    record stays the source of truth on disk."""
    as_of = _as_of_date(as_of_iso)
    if as_of is None:
        return []
    hi = as_of + timedelta(days=EARNINGS_DISPLAY_WINDOW_DAYS)

    best: "OrderedDict[str, Earnings]" = OrderedDict()
    for e in sorted(earnings, key=lambda e: e.date):
        try:
            e_date = date.fromisoformat(e.date)
        except (ValueError, TypeError):
            continue  # unparseable date -> can't judge "upcoming", drop it
        if not (as_of <= e_date <= hi):
            continue
        current = best.get(e.symbol)
        if current is None or e_date < date.fromisoformat(current.date):
            best[e.symbol] = e
    return list(best.values())


def _headline_ctx(h: Headline) -> dict:
    return {
        "title": h.title,
        "source": h.source,
        "url": _safe_url(h.url),
        "published": _fmt_dt(h.published),
    }


def _dedupe_and_group_headlines(
    headlines: list[Headline], universe: list[str]
) -> tuple["OrderedDict[str, list[Headline]]", list[Headline]]:
    """Headlines triage (GRE-3464): group `scraped.headlines` by symbol,
    dropping an exact-title duplicate that already appears under an earlier
    symbol (kept under the first symbol it was seen with) and promoting a
    title shared by 3+ distinct symbols into the Market-wide group instead
    of repeating it under each one. Presentation-only, same convention as
    `_upcoming_earnings` — `scraped_data.json` stays untouched."""
    universe_set = set(universe)
    title_symbols: "OrderedDict[str, list[str]]" = OrderedDict()
    for h in headlines:
        if h.symbol is None or h.symbol not in universe_set:
            continue
        syms = title_symbols.setdefault(h.title, [])
        if h.symbol not in syms:
            syms.append(h.symbol)

    promoted_titles = {t for t, syms in title_symbols.items() if len(syms) >= 3}

    by_symbol: "OrderedDict[str, list[Headline]]" = OrderedDict((s, []) for s in universe)
    promoted: list[Headline] = []
    promoted_seen: set = set()
    for h in headlines:
        if h.symbol is None or h.symbol not in universe_set:
            continue
        if h.title in promoted_titles:
            if h.title not in promoted_seen:
                promoted.append(h)
                promoted_seen.add(h.title)
            continue
        first_symbol = title_symbols[h.title][0]
        if h.symbol != first_symbol:
            continue  # duplicate under a later symbol — dropped
        by_symbol[h.symbol].append(h)
    return by_symbol, promoted


def _newest_first_capped(items: list[Headline], cap: int = HEADLINE_CAP) -> tuple[list[Headline], list[Headline]]:
    """Sort newest-published-first and cap at `cap`; return (shown, overflow).
    Overflow renders inside a collapsed <details> dropdown rather than being
    pointed at the run artifact — the reader expands in place."""
    ordered = sorted(items, key=lambda h: _parse_dt(h.published), reverse=True)
    return ordered[:cap], ordered[cap:]


def _earnings_within(
    symbol: str, earnings: list[Earnings], as_of: Optional[date], window_days: int = _EARNINGS_SOON_WINDOW_DAYS
) -> Optional[Earnings]:
    """Soonest `earnings` row for `symbol` within `window_days` calendar
    days on/after `as_of` — the same "near-term" test `has_earnings_soon`
    applies in model_code/signals.py, re-derived here from `scraped.earnings`
    (not imported — NG-3 purity) so the interpretation sentence can flag
    event-risk without trusting `notes[]` prose."""
    if as_of is None:
        return None
    best: Optional[Earnings] = None
    for e in earnings:
        if e.symbol != symbol:
            continue
        try:
            e_date = date.fromisoformat(e.date)
        except (ValueError, TypeError):
            continue
        delta = (e_date - as_of).days
        if 0 <= delta <= window_days:
            if best is None or e_date < date.fromisoformat(best.date):
                best = e
    return best


def _interpret_signal(sig: SymbolSignal, earnings_row: Optional[Earnings], as_of_date: Optional[date]) -> str:
    """Terse (<=10 words), research-toned read of *why* the verdict landed
    where it did — same threshold order as `decide_verdict` (README's
    "Verdict rule table"): insufficient data, then earnings window, then
    stretch+vol, then low-vol momentum, else no strong signal. Never
    buy/sell language (GRE-3464) — a factual restatement of the numbers,
    not a recommendation."""
    label = getattr(sig, "label", None)
    z, vol_ann, mom = sig.ou_zscore, sig.garch_vol_forecast_ann, sig.momentum_5d

    if label == "insufficient-data":
        return "limited price history — low-confidence read"
    if earnings_row is not None and as_of_date is not None:
        days = (date.fromisoformat(earnings_row.date) - as_of_date).days
        when = "today" if days <= 0 else f"in {days}d"
        return f"reports {when} — expect a gap"
    if vol_ann >= _VOL_HIGH_ANN and abs(z) >= _ZSCORE_STRETCH:
        direction = "above" if z >= 0 else "below"
        return f"{abs(z):.1f}σ {direction} 1-yr mean, high vol — reversion risk"
    if vol_ann < _VOL_LOW_ANN and mom >= _MOMENTUM_POS:
        return "low vol, drifting up — steady trend"
    if vol_ann < _VOL_LOW_ANN and mom <= _MOMENTUM_NEG:
        return "low vol, drifting down — steady trend"
    if label is None and sig.verdict == "avoid":
        return "flagged avoid — see notes for detail"
    return "no strong signal — inside normal ranges"


def _verdict_tally(signal_rows: list[dict]) -> str:
    """TL;DR tally chip text, e.g. '4 avoid · 1 neutral' — fixed
    priority order, zero-count verdicts omitted."""
    counts = Counter(r["verdict"] for r in signal_rows)
    parts = [f"{counts[v]} {v}" for v in _VERDICT_TALLY_ORDER if counts.get(v)]
    return " · ".join(parts) if parts else "no signal coverage"


def _stretched_chip_text(signal_rows: list[dict]) -> Optional[str]:
    """TL;DR most-stretched-name chip, e.g. 'MSFT +6.5σ above its 1-yr
    mean' — `signal_rows` is already ranked by |OU z-score| desc, so the
    top row is the answer."""
    if not signal_rows:
        return None
    top = signal_rows[0]
    z = top["zscore"]
    direction = "above" if z >= 0 else "below"
    return f'{top["symbol"]} {z:+.1f}σ {direction} its 1-yr mean'


def _build_context(scraped: ScrapedData, signals: Signals) -> dict:
    covered = signals.per_symbol
    as_of_date = _as_of_date(scraped.as_of)

    ranked = sorted(covered.items(), key=lambda kv: abs(kv[1].ou_zscore), reverse=True)
    signal_rows = []
    for rank, (symbol, sig) in enumerate(ranked, start=1):
        quote = scraped.quotes.get(symbol)
        chg_pct = None
        if quote is not None and quote.prev_close:
            chg_pct = (quote.last - quote.prev_close) / quote.prev_close
        earnings_row = _earnings_within(symbol, scraped.earnings, as_of_date)
        signal_rows.append(
            {
                "rank": rank,
                "symbol": symbol,
                "last": quote.last if quote else None,
                "chg_pct": chg_pct,
                "garch_1d": sig.garch_vol_forecast_1d,
                "garch_ann": sig.garch_vol_forecast_ann,
                "vol_svg": _vol_bar_svg(sig.garch_vol_forecast_ann),
                "zscore": sig.ou_zscore,
                "zscore_svg": _zscore_bar_svg(sig.ou_zscore),
                "zscore_stretched": abs(sig.ou_zscore) >= _ZSCORE_STRETCH,
                "half_life": sig.ou_half_life_d,
                "half_life_note": _half_life_note(sig.ou_half_life_d),
                "momentum": sig.momentum_5d,
                "momentum_html": _momentum_arrow(sig.momentum_5d),
                "verdict": sig.verdict,
                "verdict_class": _verdict_class(sig.verdict),
                # GRE-3464: optional finer-grained research label — None for
                # any signal produced before this field existed.
                "label": getattr(sig, "label", None),
                "notes": sig.notes,
                # GRE-3464: plain-English "why" sentence, same thresholds
                # decide_verdict uses — see _interpret_signal.
                "interpretation": _interpret_signal(sig, earnings_row, as_of_date),
            }
        )
    uncovered = [s for s in scraped.universe if s not in covered]

    earnings_sorted = _upcoming_earnings(scraped.earnings, scraped.as_of)
    earnings_rows = []
    for e in earnings_sorted:
        sig = covered.get(e.symbol)
        e_date = date.fromisoformat(e.date)
        days = (e_date - as_of_date).days if as_of_date is not None else None
        relative = None if days is None else ("today" if days <= 0 else f"in {days}d")
        earnings_rows.append(
            {
                "symbol": e.symbol,
                "date": _fmt_date(e.date),
                "session": _session_label(e.session),
                "relative": relative,
                "verdict": sig.verdict if sig else None,
                "verdict_class": _verdict_class(sig.verdict) if sig else None,
                "zscore": sig.ou_zscore if sig else None,
            }
        )

    # GRE-3464: headlines triage — dedupe identical titles shared across
    # symbols, then newest-first + cap per group. See
    # _dedupe_and_group_headlines / _newest_first_capped.
    by_symbol, promoted = _dedupe_and_group_headlines(scraped.headlines, scraped.universe)
    headline_groups = []
    for symbol in scraped.universe:
        shown, overflow = _newest_first_capped(by_symbol[symbol])
        headline_groups.append(
            {
                "symbol": symbol,
                # NB: deliberately not called "items" — dict.items is a real
                # attribute, and Jinja's dot-lookup tries attribute access
                # before item access, so `g.items` would silently resolve to
                # the bound method instead of this list.
                "headlines": [_headline_ctx(h) for h in shown],
                "more_headlines": [_headline_ctx(h) for h in overflow],
                "more_count": len(overflow),
            }
        )
    macro_raw = [h for h in scraped.headlines if h.symbol is None] + promoted
    macro_shown, macro_overflow = _newest_first_capped(macro_raw)
    macro_headlines = [_headline_ctx(h) for h in macro_shown]
    macro_more_headlines = [_headline_ctx(h) for h in macro_overflow]
    macro_more_count = len(macro_overflow)

    sessions = list(scraped.provenance.sessions)
    replays = list(getattr(scraped.provenance, "replays", None) or [])

    warning_groups = _summarize_warnings(scraped.warnings, len(scraped.universe), scraped)

    # GRE-3464: TL;DR strip — always computed from this same context, never
    # hardcoded. Chips omitted when the underlying data doesn't support them
    # (no signal coverage, no earnings in window).
    tldr_chips = [{"text": _verdict_tally(signal_rows), "cls": "tldr-tally"}]
    stretched_text = _stretched_chip_text(signal_rows)
    if stretched_text:
        tldr_chips.append({"text": stretched_text, "cls": "tldr-stretch"})
    if earnings_rows:
        top_e = earnings_rows[0]
        tldr_chips.append(
            {"text": f"next earnings: {top_e['symbol']} {top_e['relative']}", "cls": "tldr-earnings"}
        )
    n_warn = len(warning_groups)
    if n_warn:
        tldr_chips.append(
            {
                "text": f"{n_warn} data warning{'s' if n_warn != 1 else ''}",
                "cls": "tldr-warn",
                "href": "#signals",
            }
        )
    else:
        tldr_chips.append({"text": "all sources ok", "cls": "tldr-ok"})

    return {
        "as_of": _fmt_dt(scraped.as_of),
        "signals_as_of": _fmt_dt(signals.as_of),
        "universe": scraped.universe,
        "signal_rows": signal_rows,
        "uncovered": uncovered,
        "earnings_rows": earnings_rows,
        "headline_groups": headline_groups,
        "macro_headlines": macro_headlines,
        "macro_more_headlines": macro_more_headlines,
        "macro_more_count": macro_more_count,
        "provenance_claim": _provenance_claim(sessions, replays),
        "session_ids_short": [_short_id(s) for s in sessions],
        "warning_groups": warning_groups,
        "tldr_chips": tldr_chips,
        "disclaimer": DISCLAIMER,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Overnight Options Desk Brief — {{ as_of }}</title>
<style>
  :root {
    color-scheme: light;
    --canvas: #FBFBFA;
    --surface: #FFFFFF;
    --border: #EAEAEA;
    --text: #111111;
    --muted: #787774;
    --neutral-bg: #F7F6F3;
    --red-bg: #FDEBEC; --red-text: #9F2F2D;
    --green-bg: #EDF3EC; --green-text: #346538;
    --yellow-bg: #FBF3DB; --yellow-text: #956400;
    --blue-bg: #E1F3FE; --blue-text: #1F6C9F;
    --font-sans: -apple-system, "SF Pro Display", "Helvetica Neue", "Segoe UI", sans-serif;
    --font-serif: "Iowan Old Style", "Palatino", Georgia, serif;
    --font-mono: "SF Mono", "Geist Mono", Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--canvas);
    color: var(--text);
    font-family: var(--font-sans);
    font-size: 15px;
    line-height: 1.6;
    padding: 0 0 4rem;
  }
  a { color: var(--text); text-decoration-color: var(--border); text-underline-offset: 2px; }
  a:hover { text-decoration-color: currentColor; }
  .wrap { max-width: 72rem; margin: 0 auto; padding: 0 1.5rem; }
  .mono { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }

  /* CSS-only page-load motion: opacity + a short rise, staggered per
     section. No JS, no scroll listeners — purely declarative, and fully
     disabled for anyone who has asked for less motion. */
  @media (prefers-reduced-motion: no-preference) {
    header.brief-header, section, footer.provenance {
      animation: brief-rise 520ms ease-out both;
    }
    header.brief-header { animation-delay: 0ms; }
    section:nth-of-type(1) { animation-delay: 60ms; }
    section:nth-of-type(2) { animation-delay: 120ms; }
    section:nth-of-type(3) { animation-delay: 180ms; }
    footer.provenance { animation-delay: 240ms; }
    @keyframes brief-rise {
      from { opacity: 0; transform: translateY(8px); }
      to { opacity: 1; transform: translateY(0); }
    }
  }

  header.brief-header {
    border-bottom: 1px solid var(--border);
    padding: 2.5rem 0 1.5rem;
    margin-bottom: 3rem;
  }
  header.brief-header .wrap { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: baseline; gap: .5rem 2rem; }
  h1 {
    font-family: var(--font-serif);
    font-size: 1.6rem;
    letter-spacing: -.02em;
    text-transform: uppercase;
    margin: 0;
    color: var(--text);
    font-weight: 500;
  }
  .as-of { color: var(--muted); font-size: .85rem; text-align: right; }
  .run-time { color: var(--text); font-size: .9rem; }
  .run-time strong { font-family: var(--font-mono); font-variant-numeric: tabular-nums; color: var(--text); font-weight: 600; }
  .as-of-detail { font-family: var(--font-mono); color: var(--muted); font-size: .72rem; margin-top: .2rem; }
  .universe { color: var(--muted); font-size: .85rem; }
  .universe strong { font-family: var(--font-mono); color: var(--text); font-weight: 600; }
  .tldr-wrap { padding-top: 1.25rem; margin-top: 1rem; border-top: 1px solid var(--border); }
  .tldr { display: flex; flex-wrap: wrap; gap: .5rem; }
  .chip {
    display: inline-block;
    padding: .3rem .8rem;
    border-radius: 9999px;
    font-size: .78rem;
    border: 1px solid var(--border);
    color: var(--text);
    background: var(--surface);
    text-decoration: none;
  }
  a.chip:hover { text-decoration: underline; background: var(--neutral-bg); }
  .tldr-ok { color: var(--green-text); background: var(--green-bg); border-color: var(--green-bg); }
  .tldr-warn { color: var(--yellow-text); background: var(--yellow-bg); border-color: var(--yellow-bg); }
  section { margin: 0 0 4rem; }
  section > h2 {
    font-family: var(--font-sans);
    font-size: .78rem;
    letter-spacing: .1em;
    text-transform: uppercase;
    font-weight: 600;
    color: var(--muted);
    border-bottom: 1px solid var(--border);
    padding-bottom: .6rem;
    margin: 0 0 1.5rem;
  }
  table { width: 100%; border-collapse: collapse; background: var(--surface); }
  .table-card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
  th, td { text-align: left; padding: .9rem 1rem; border-bottom: 1px solid var(--border); vertical-align: middle; }
  tbody tr:last-child td { border-bottom: none; }
  th { font-family: var(--font-sans); font-size: .7rem; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); font-weight: 600; }
  .col-sub { display: block; font-size: .68rem; text-transform: none; letter-spacing: 0; font-weight: 400; color: var(--muted); margin-top: .2rem; }
  .num { text-align: right; }
  .cell-metric { display: flex; align-items: center; gap: .6rem; }
  .bar { display: block; width: 90px; height: 14px; flex: 0 0 auto; }
  .metric-text { white-space: nowrap; }
  .mom { display: inline-flex; align-items: center; gap: .3rem; font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
  .mom-up { color: var(--green-text); }
  .mom-down { color: var(--red-text); }
  .mom-flat { color: var(--muted); }
  /* The bar carries magnitude; the number stays in text ink so the accent
     colour means one thing (stretched/elevated) instead of doing double
     duty. Weight, not hue, marks a stretched reading in the numeral. */
  .zscore-num { color: var(--text); }
  .zscore-num.stretched { font-weight: 700; }
  .hl-note { display: block; font-size: .68rem; color: var(--muted); margin-top: .15rem; }
  .badge {
    display: inline-block;
    padding: .2rem .7rem;
    border-radius: 9999px;
    font-size: .72rem;
    font-family: var(--font-sans);
    letter-spacing: .01em;
    font-weight: 600;
    border: 1px solid transparent;
  }
  .v-bullish { color: var(--green-text); background: var(--green-bg); }
  .v-bearish { color: var(--yellow-text); background: var(--yellow-bg); }
  .v-avoid   { color: var(--red-text); background: var(--red-bg); }
  .v-neutral { color: var(--muted); background: var(--neutral-bg); }
  .verdict-interp { display: block; color: var(--muted); font-size: .78rem; margin-top: .4rem; line-height: 1.5; max-width: 26ch; }

  /* Sharp +/- disclosure toggle, CSS only — no JS. */
  details > summary { cursor: pointer; list-style: none; }
  details > summary::-webkit-details-marker { display: none; }
  details > summary::before {
    content: "+";
    display: inline-block;
    width: 1em;
    font-family: var(--font-mono);
    font-weight: 700;
    color: var(--muted);
  }
  details[open] > summary::before { content: "\2212"; }

  .how-to-read { margin-top: 1.5rem; padding: 1rem 0 0; border-top: 1px solid var(--border); }
  .how-to-read summary { color: var(--muted); font-size: .82rem; font-weight: 500; }
  .how-to-read p { color: var(--text); font-size: .88rem; line-height: 1.7; margin: .9rem 0 0; }
  .htr { display: flex; flex-direction: column; gap: 1rem; margin-top: .9rem; font-size: .88rem; line-height: 1.65; }
  .htr-models { display: flex; flex-direction: column; }
  .htr-row { display: grid; grid-template-columns: 9rem 1fr; gap: .75rem; padding: .5rem 0; border-bottom: 1px solid var(--border); }
  .htr-row:last-child { border-bottom: 0; }
  .htr-row b { font-weight: 600; }
  .htr-rules b { font-weight: 600; }
  .htr-rules ol { margin: .5rem 0 0; padding-left: 1.3rem; }
  .htr-rules li { margin-bottom: .3rem; }
  .htr-note { margin: 0; }
  .htr-eq { display: block; margin-top: .2rem; font-size: .76rem; color: var(--muted); }
  @media (max-width: 640px) { .htr-row { grid-template-columns: 1fr; gap: .15rem; } }
  .how-to-read .boundary { color: var(--yellow-text); }
  .uncovered-note { color: var(--muted); font-size: .82rem; margin-top: 1rem; }
  .callouts { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 1rem; }
  .callout { border: 1px solid var(--border); background: var(--surface); border-radius: 8px; padding: 1.1rem 1.2rem; }
  .callout .sym { font-family: var(--font-sans); font-weight: 700; color: var(--text); font-size: .95rem; }
  .callout .rel-time { font-family: var(--font-sans); font-weight: 600; color: var(--text); font-size: 1.15rem; margin-top: .5rem; }
  .callout .date { font-family: var(--font-mono); color: var(--muted); font-size: .74rem; margin-top: .35rem; }
  .callout .badge { margin-top: .7rem; }
  .headline-group { margin-bottom: 1.75rem; }
  .headline-group h3 { font-family: var(--font-sans); font-size: .85rem; font-weight: 700; color: var(--text); margin: 0 0 .5rem; }
  .headline-group ul { list-style: none; margin: 0; padding: 0; }
  .headline-group li { padding: .7rem 0; border-bottom: 1px solid var(--border); }
  .headline-group li:last-child { border-bottom: none; }
  .headline-group li a { display: block; color: var(--text); font-size: .92rem; }
  .headline-meta { font-family: var(--font-mono); color: var(--muted); font-size: .74rem; margin-top: .3rem; }
  details.headline-more { padding: .5rem 0 0; border: 0; }
  details.headline-more > summary { color: var(--muted); font-size: .78rem; cursor: pointer; }
  details.headline-more > ul { margin: .35rem 0 0; }
  .empty { color: var(--muted); font-size: .85rem; }
  .warnings { margin-top: 1rem; display: flex; flex-direction: column; gap: .5rem; }
  .warn-row {
    border: 1px solid var(--border);
    background: var(--yellow-bg);
    border-radius: 8px;
    padding: .7rem .9rem;
    font-size: .82rem;
    color: var(--yellow-text);
  }
  .warn-row .warn-headline { display: flex; gap: .55rem; align-items: flex-start; }
  .warn-row .warn-icon { flex: 0 0 auto; margin-top: .15rem; }
  .warn-row .warn-icon svg { display: block; width: 13px; height: 13px; }
  .warn-row details { margin-top: .45rem; }
  .warn-row summary { color: var(--muted); font-size: .74rem; }
  .warn-row .warn-raw {
    margin: .45rem 0 0;
    padding-left: 1.4rem;
    color: var(--muted);
    font-family: var(--font-mono);
    font-size: .72rem;
    word-break: break-word;
  }
  .warn-row .warn-raw li { margin-bottom: .2rem; }
  footer.provenance {
    border-top: 1px solid var(--border);
    margin-top: 3rem;
    padding-top: 1.25rem;
    color: var(--muted);
    font-family: var(--font-mono);
    font-size: .76rem;
  }
  footer.provenance .disclaimer {
    font-family: var(--font-sans);
    color: var(--yellow-text);
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .04em;
    font-size: .74rem;
    margin-bottom: .6rem;
  }
  footer.provenance .prov-claim { margin-bottom: .4rem; }
  footer.provenance details.prov-detail { margin-bottom: .6rem; }
  footer.provenance details.prov-detail summary { color: var(--muted); }
  footer.provenance .prov-ids { margin-top: .4rem; }
  kbd {
    display: inline-block;
    font-family: var(--font-mono);
    font-size: .72rem;
    color: var(--text);
    background: var(--neutral-bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: .1rem .4rem;
  }

  @media (max-width: 640px) {
    table.responsive thead { display: none; }
    table.responsive, table.responsive tbody, table.responsive tr, table.responsive td {
      display: block; width: 100%;
    }
    table.responsive tr { border-bottom: 1px solid var(--border); margin-bottom: 0; padding: .6rem 1rem; }
    table.responsive tr:last-child { border-bottom: none; }
    table.responsive td {
      display: flex; justify-content: space-between; align-items: flex-start;
      border-bottom: none; padding: .45rem 0; text-align: right; gap: .75rem;
    }
    table.responsive td::before {
      content: attr(data-label);
      flex: 0 0 auto;
      color: var(--muted); text-transform: uppercase; font-size: .68rem;
      letter-spacing: .04em; margin-right: .5rem; margin-top: .15rem; text-align: left;
    }
    /* The vol/z-score cells pair a fixed-width bar with a text label — at
       390px there isn't room for both on one line without either wrapping
       or being clipped by the card edge. Wrap and shrink the bar rather
       than let it overflow. */
    table.responsive .cell-metric { flex-wrap: wrap; justify-content: flex-end; row-gap: .3rem; }
    table.responsive .metric-text { white-space: normal; text-align: right; }
    table.responsive .bar { width: 64px; }
    table.responsive .verdict-interp { max-width: 20ch; }
  }
</style>
</head>
<body>
<header class="brief-header" id="header">
  <div class="wrap">
    <h1>Overnight Options Desk &mdash; Morning Brief</h1>
    <div class="as-of">
      <div class="run-time">Run <strong>{{ as_of }}</strong></div>
      <div class="as-of-detail">scraped {{ as_of }} &middot; signals {{ signals_as_of }}</div>
    </div>
    <div class="universe">Universe: {% for s in universe %}<strong>{{ s }}</strong>{% if not loop.last %}, {% endif %}{% endfor %}</div>
  </div>
  <div class="wrap tldr-wrap" id="tldr">
    <div class="tldr">
      {% for c in tldr_chips %}
      {% if c.href %}<a class="chip {{ c.cls }}" href="{{ c.href }}">{{ c.text }}</a>{% else %}<span class="chip {{ c.cls }}">{{ c.text }}</span>{% endif %}
      {% endfor %}
    </div>
  </div>
</header>

<div class="wrap">

  <section id="signals">
    <h2>Ranked signals (by |OU z-score|)</h2>
    <div style="overflow-x:auto" class="table-card">
    <table class="responsive">
      <thead>
        <tr>
          <th>#</th><th>Sym</th><th class="num">Last</th><th class="num">Chg</th>
          <th>Vol (1d / ann)<span class="col-sub">expected move</span></th>
          <th>OU z-score<span class="col-sub">0 = 1-yr mean &middot; dashed ticks at &plusmn;1.5</span></th>
          <th class="num">Half-life (d)<span class="col-sub">days for stretch to halve</span></th>
          <th>Mom 5d<span class="col-sub">5-day price change</span></th>
          <th>Verdict</th>
        </tr>
      </thead>
      <tbody>
        {% for r in signal_rows %}
        <tr>
          <td data-label="#" class="mono">{{ r.rank }}</td>
          <td data-label="Symbol"><strong>{{ r.symbol }}</strong></td>
          <td data-label="Last" class="num mono">{% if r.last is not none %}{{ "%.2f"|format(r.last) }}{% else %}&mdash;{% endif %}</td>
          <td data-label="Chg" class="num mono">{% if r.chg_pct is not none %}<span class="{{ 'mom-up' if r.chg_pct >= 0 else 'mom-down' }}">{{ "%+.2f%%"|format(r.chg_pct * 100) }}</span>{% else %}&mdash;{% endif %}</td>
          <td data-label="Vol 1d/ann"><span class="cell-metric">{{ r.vol_svg|safe }}<span class="metric-text mono">{{ "%.2f%%"|format(r.garch_1d * 100) }} &middot; {{ "%.1f%%"|format(r.garch_ann * 100) }}</span></span></td>
          <td data-label="OU z-score"><span class="cell-metric">{{ r.zscore_svg|safe }}<span class="zscore-num mono{{ ' stretched' if r.zscore_stretched else '' }}">{{ "%+.2f"|format(r.zscore) }}</span></span></td>
          <td data-label="Half-life" class="num mono">{{ "%.1f"|format(r.half_life) }}<span class="hl-note">{{ r.half_life_note }}</span></td>
          <td data-label="Momentum 5d">{{ r.momentum_html|safe }}</td>
          <td data-label="Verdict"><span class="badge {{ r.verdict_class }}"{% if r.label %} title="{{ r.label }}"{% endif %}>{{ r.verdict }}</span><span class="verdict-interp">{{ r.interpretation }}</span></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    </div>
    <details class="how-to-read" id="how-to-read">
      <summary>How to read this</summary>
      <div class="htr">
        <div class="htr-models">
          <div class="htr-row"><b>Vol (1d / ann)</b><span>GARCH(1,1) forecast of tomorrow's volatility &mdash; how much movement to expect, daily and annualized.
            <span class="htr-eq mono">&sigma;&sup2;(t) = &omega; + &alpha;&middot;r&sup2;(t&minus;1) + &beta;&middot;&sigma;&sup2;(t&minus;1) &nbsp;&middot;&nbsp; ann = 1d &times; &radic;252</span></span></div>
          <div class="htr-row"><b>OU z-score</b><span>Ornstein&ndash;Uhlenbeck fit &mdash; how many standard deviations the price sits from its own fitted 1-year mean. 0 means "at its normal level".
            <span class="htr-eq mono">ln P(t) = c + &phi;&middot;ln P(t&minus;1) + &epsilon; &nbsp;&middot;&nbsp; z = (ln P &minus; &mu;) / &sigma;&epsilon;, &nbsp;&mu; = c / (1&minus;&phi;)</span></span></div>
          <div class="htr-row"><b>Half-life</b><span>from the same fit &mdash; how many days a stretch like this has historically taken to decay halfway back.
            <span class="htr-eq mono">t&frac12; = ln 0.5 / ln &phi;</span></span></div>
          <div class="htr-row"><b>Mom 5d</b><span>plain 5-day percent change &mdash; which way it's leaning right now.
            <span class="htr-eq mono">P(t) / P(t&minus;5) &minus; 1</span></span></div>
        </div>
        <div class="htr-rules">
          <b>The Verdict applies these rules, first match wins:</b>
          <ol>
            <li>fewer than 60 days of history &rarr; <code>avoid</code> (insufficient data)</li>
            <li>earnings within 3 calendar days &rarr; <code>avoid</code> (event-risk: the vol forecast likely understates an earnings move)</li>
            <li>z-score &ge;1.5&sigma; and annualized vol &ge;35% &rarr; <code>avoid</code> (stretched and volatile &mdash; mean-reversion watch)</li>
            <li>annualized vol under 20% and 5-day momentum beyond &plusmn;2% &rarr; <code>bullish</code> / <code>bearish</code> (quiet trend)</li>
            <li>anything else &rarr; <code>neutral</code></li>
          </ol>
        </div>
        <p class="htr-note">Every verdict traces back to these numbers &mdash; nothing here is hidden or proprietary.
        <span class="boundary">These are research labels, not trade
        instructions. {{ disclaimer }}</span></p>
      </div>
    </details>
    {% if uncovered %}
    <div class="uncovered-note">No signal coverage: {% for s in uncovered %}{{ s }}{% if not loop.last %}, {% endif %}{% endfor %}</div>
    {% endif %}
    {% if warning_groups %}
    <div class="warnings">
      {% for g in warning_groups %}
      <div class="warn-row">
        <div class="warn-headline"><span class="warn-icon" aria-hidden="true"><svg viewBox="0 0 16 16"><path d="M8 1.5 L15 14.5 L1 14.5 Z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><line x1="8" y1="6.2" x2="8" y2="10" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/><circle cx="8" cy="12.3" r=".9" fill="currentColor"/></svg></span><span>{{ g.headline }}</span></div>
        {% if g.count > 1 or g.details[0] != g.headline %}
        <details>
          <summary>affected page{{ 's' if g.count != 1 else '' }} ({{ g.count }}) — full traces in the run's scraped_data.json</summary>
          <ul class="warn-raw">
            {% for line in g.details %}<li>{{ line }}</li>{% endfor %}
          </ul>
        </details>
        {% endif %}
      </div>
      {% endfor %}
    </div>
    {% endif %}
  </section>

  <section id="earnings">
    <h2>Earnings in window</h2>
    {% if earnings_rows %}
    <div class="callouts">
      {% for e in earnings_rows %}
      <div class="callout">
        <div class="sym">{{ e.symbol }}</div>
        {% if e.relative %}<div class="rel-time">{{ e.relative }}</div>{% endif %}
        <div class="date">{{ e.date }} &middot; {{ e.session }}</div>
        {% if e.verdict %}
        <div class="badge {{ e.verdict_class }}">{{ e.verdict }}</div>
        {% endif %}
      </div>
      {% endfor %}
    </div>
    {% else %}
    <div class="empty">No earnings in the current window.</div>
    {% endif %}
  </section>

  <section id="headlines">
    <h2>Headlines</h2>
    {% for g in headline_groups %}
    <div class="headline-group">
      <h3>{{ g.symbol }}</h3>
      {% if g.headlines %}
      <ul>
        {% for h in g.headlines %}
        <li>
          <a href="{{ h.url }}" rel="noopener noreferrer nofollow" target="_blank">{{ h.title }}</a>
          <div class="headline-meta">{{ h.source }} &middot; {{ h.published }}</div>
        </li>
        {% endfor %}
      </ul>
      {% if g.more_count %}
      <details class="headline-more">
        <summary>{{ g.more_count }} more headline{{ 's' if g.more_count != 1 else '' }}</summary>
        <ul>
          {% for h in g.more_headlines %}
          <li>
            <a href="{{ h.url }}" rel="noopener noreferrer nofollow" target="_blank">{{ h.title }}</a>
            <div class="headline-meta">{{ h.source }} &middot; {{ h.published }}</div>
          </li>
          {% endfor %}
        </ul>
      </details>
      {% endif %}
      {% else %}
      <div class="empty">No headlines captured.</div>
      {% endif %}
    </div>
    {% endfor %}
    {% if macro_headlines %}
    <div class="headline-group">
      <h3>Market-wide</h3>
      <ul>
        {% for h in macro_headlines %}
        <li>
          <a href="{{ h.url }}" rel="noopener noreferrer nofollow" target="_blank">{{ h.title }}</a>
          <div class="headline-meta">{{ h.source }} &middot; {{ h.published }}</div>
        </li>
        {% endfor %}
      </ul>
      {% if macro_more_count %}
      <details class="headline-more">
        <summary>{{ macro_more_count }} more headline{{ 's' if macro_more_count != 1 else '' }}</summary>
        <ul>
          {% for h in macro_more_headlines %}
          <li>
            <a href="{{ h.url }}" rel="noopener noreferrer nofollow" target="_blank">{{ h.title }}</a>
            <div class="headline-meta">{{ h.source }} &middot; {{ h.published }}</div>
          </li>
          {% endfor %}
        </ul>
      </details>
      {% endif %}
    </div>
    {% endif %}
  </section>

</div>

<footer class="provenance" id="provenance">
  <div class="wrap">
    <div class="disclaimer">{{ disclaimer }}</div>
    <div class="prov-claim">{{ provenance_claim }}</div>
    {% if session_ids_short %}
    <details class="prov-detail">
      <summary>session ids, last 8 chars ({{ session_ids_short|length }})</summary>
      <div class="prov-ids">{% for s in session_ids_short %}<kbd>&hellip;{{ s }}</kbd>{% if not loop.last %} {% endif %}{% endfor %}</div>
    </details>
    {% endif %}
    <div>Rendered {{ generated_at }} &middot; desk/brief.py (hermetic, no external requests)</div>
  </div>
</footer>

</body>
</html>
"""


def render_brief(scraped: ScrapedData, signals: Signals) -> str:
    """Pure render: two validated contract objects -> one self-contained
    HTML document string. No I/O, no Solari calls."""
    env = Environment(autoescape=True)
    template = env.from_string(_TEMPLATE)
    return template.render(**_build_context(scraped, signals))


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render scraped_data.json + signals.json into one self-contained brief.html."
    )
    parser.add_argument("--scraped", required=True, help="Path to a scraped_data JSON file.")
    parser.add_argument("--signals", required=True, help="Path to a signals JSON file.")
    parser.add_argument("--out", required=True, help="Path to write the rendered brief.html to.")
    args = parser.parse_args(argv)

    scraped = load_scraped(args.scraped)
    signals = load_signals(args.signals)
    html_out = render_brief(scraped, signals)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_out, encoding="utf-8")
    print(f"wrote {out_path} ({len(html_out)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
