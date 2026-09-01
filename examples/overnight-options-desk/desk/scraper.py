"""desk/scraper.py — scraper lane (GRE-3460).

Given <=5 symbols, scrapes earnings dates, dated headlines, and quote
snapshots (last + prev_close) using `desk.solari_client`'s cloud-browser
helper, and emits a schema-valid `scraped_data.json` (desk/contracts.py,
desk/schemas/scraped_data.schema.json).

Design
------
Each data type (earnings, headlines, quotes) has a primary source and two
fallbacks, tried in order (`EARNINGS_SOURCES` / `HEADLINE_SOURCES` /
`QUOTE_SOURCES` below). A source is either fetched through a fresh cloud
browser (`_browser_fetch`, session-recorded, session id folded into
`provenance.sessions`) or over plain HTTP (`_http_fetch`, used only where the
ticket explicitly allows it as a last resort). Parsing is a pure function of
the fetched text (+ html for one source) so it can be exercised hermetically
against saved fixtures in `fixtures/scraper/` with no network — see
`tests/test_scraper_parsers.py`.

Per-source failures (network error, unparseable page, or an injected
"unreachable" override — see `force_unreachable`) are caught, logged to
`warnings[]`, and the next fallback is tried; they never raise out of
`scrape()`. This is NG-5 (graceful degradation) and is exercised hermetically
in `tests/test_scraper_fallback.py`.

Build-time source verification (see the generator report for detail):
  - Yahoo Finance (earnings calendar, per-symbol news, quote page),
    StockAnalysis.com (earnings), Yahoo per-symbol headlines RSS, Google
    News RSS, Yahoo's chart JSON endpoint, and CBOE delayed quotes (JSON)
    all verified working against a vanilla browser / plain HTTP at build
    time.

GRE-3464 fallback repairs: two of the plain-HTTP fallbacks were
structurally broken (see the generator report for full live-verification
transcripts):
  - `stooq_csv` (the quotes chain's 2nd fallback) 404d on every shape of
    Stooq's `/q/l/` last-quote endpoint tried live (`f=sd2t2ohlcv&h&e=csv`
    and several variants) — the endpoint itself appears to have been
    retired/restructured site-wide, not a URL typo in this repo. Replaced
    with `yahoo_chart_quote`: Yahoo's chart JSON endpoint
    (`query1.finance.yahoo.com/v8/finance/chart/{SYM}?range=5d&interval=1d`,
    plain HTTP, no browser), reading `last`/`prev_close` off the last two
    entries of `indicators.quote[0].close` — verified live and cross-checked
    against `parse_yahoo_quote`'s own fixture values for AAPL (both agree:
    last 316.85, prev_close 319.70).
  - `marketwatch_rss` (the headlines chain's 2nd fallback) filtered
    MarketWatch's general top-stories RSS feed by symbol substring — a feed
    that is, by construction, almost never about any one given symbol, so
    the fallback warned on nearly every run instead of ever actually
    recovering data. A fallback that structurally cannot succeed is noise,
    not resilience, so it's removed outright (not demoted) and replaced
    with `yahoo_headlines_rss`: Yahoo's real per-symbol RSS feed
    (`feeds.finance.yahoo.com/rss/2.0/headline?s={SYM}&region=US&lang=en-US`),
    verified live returning real per-symbol articles over plain HTTP.
    `google_news_rss` (browser-fetched, proven live) stays as the deeper
    3rd fallback per NG-3.

GRE-3464 earnings-chain fix: Nasdaq's HTML earnings page
(`nasdaq.com/market-activity/stocks/{sym}/earnings`) was the original coded
PRIMARY earnings source, but failed with `net::ERR_HTTP2_PROTOCOL_ERROR`
from Solari's cloud browser for every symbol, on every run — a primary that
fails 100% of the time live is misconfiguration, not resilience. Three
things were tried, in order, before touching the chain (see the ticket
report for full transcripts):

  1. Nasdaq's JSON API as a plain-HTTP replacement (`_http_fetch`, no
     browser). `api.nasdaq.com/api/analyst/{SYM}/earnings-date` and
     `api.nasdaq.com/api/calendar/earnings?date=YYYY-MM-DD` both return
     real `200 application/json` with a browser-ish User-Agent (no auth,
     no stealth) — so the underlying HTTP/2 error is specific to Nasdaq's
     www-app edge rejecting Solari's cloud-browser egress, not a blanket
     Nasdaq block. BUT `analyst/{SYM}/earnings-date` returned "Our vendor,
     Zacks Investment Research, hasn't provided us with the upcoming
     earnings report date" for all 5 target symbols (AAPL, NVDA, MSFT,
     TSLA, AMZN) plus CRM/WMT/TGT — it reliably has *no* forward date for
     names that reported within roughly the last month (all five target
     symbols reported in late Jul/Aug 2026); it works fine for names
     further out in their cycle (PANW, ORCL, COST, JPM all returned a real
     date). And `calendar/earnings?date=` only returns rows for symbols
     reporting on that *specific* date, so finding one target symbol's
     next date means scanning up to ~180 daily calendar pages — not a
     viable "primary" shape. Not implemented: no JSON endpoint gives a
     reliable date for this ticket's actual test cohort.
  2. Stealth mode on the existing Nasdaq browser source, now that the
     account is nominally Starter tier. `solari.launch(stealth=True)`
     against the real gateway returned `402 {"error":"Stealth mode
     requires a paid plan","code":"FeatureRequiresPlan","plan":"free"}` —
     the gateway still sees this account as free tier, so the promo-code
     upgrade hadn't propagated at verification time. Per the ticket's own
     step 2 ("if it renders, add stealth as an option"), no plumbing was
     added since it never rendered — there is nothing live to wire an
     opt-in flag around yet. `solari_client.open_browser_page` still takes
     no `stealth` kwarg; re-run this probe once the plan shows non-free
     before adding one.
  3. Reorder: since neither replacement panned out, `yahoo_earnings_calendar`
     (proven reliable, live and in the existing fallback chain) is promoted
     to primary; `stockanalysis_earnings` stays second; `nasdaq_earnings` is
     demoted to last (kept per NG-3 — the chain still tries it, and its
     parser is unchanged so it starts working for free the day either (a)
     Nasdaq's edge stops HTTP/2-rejecting Solari's browser egress or (b)
     stealth propagates on this account).
"""

from __future__ import annotations

import argparse
import asyncio
import email.utils
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx

from desk.contracts import Earnings, Headline, Provenance, Quote, ScrapedData, validate_scraped
from desk.solari_client import open_browser_page

logger = logging.getLogger("desk.scraper")

FORCE_UNREACHABLE_ENV = "DESK_SCRAPER_FORCE_UNREACHABLE"
HEADLINE_TARGET = 3
HTTP_TIMEOUT_S = 15.0
HTTP_USER_AGENT = "Mozilla/5.0 (compatible; solari-desk-scraper/1.0; +https://github.com/anthropics/solari-cookbook)"
EARNINGS_WINDOW = (timedelta(days=-7), timedelta(days=180))


# ---------------------------------------------------------------------------
# low-level fetch primitives — kept separate from parsing and swappable so
# tests can stub them without touching the network or a real Solari session.
# ---------------------------------------------------------------------------


@dataclass
class FetchResult:
    text: str
    html: Optional[str] = None
    session_id: Optional[str] = None


async def _browser_fetch(url: str, *, recording: bool) -> FetchResult:
    page = await open_browser_page(url, recording=recording)
    return FetchResult(text=page.text, html=page.html, session_id=page.session_id)


async def _http_fetch(url: str) -> FetchResult:
    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT_S, headers={"User-Agent": HTTP_USER_AGENT}
    ) as client:
        resp = await client.get(url, follow_redirects=True)
        resp.raise_for_status()
        return FetchResult(text=resp.text)


def _infer_session(hour_24: Optional[int]) -> str:
    """4pm-ish -> after market close; before 9am -> before market open."""
    if hour_24 is None:
        return "unknown"
    if hour_24 >= 16:
        return "amc"
    if hour_24 < 9:
        return "bmo"
    return "unknown"


def _parse_rfc822(value: str) -> datetime:
    dt = email.utils.parsedate_to_datetime(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_month_day_year(value: str) -> datetime:
    """Accepts both abbreviated ("Jul 30, 2026") and full ("July 30, 2026")
    month names — sources are inconsistent about which they use."""
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognized date format: {value!r}")


def parse_nasdaq_earnings(fetch: FetchResult, symbol: str, now: datetime) -> list[dict]:
    """Best-effort parser for Nasdaq's earnings page. Unverified live — the
    page could not be reached at all from the vanilla cloud browser at build
    time or under GRE-3464's stealth retest (`net::ERR_HTTP2_PROTOCOL_ERROR`
    / stealth `402 FeatureRequiresPlan`, see module docstring). Demoted to
    last fallback in `EARNINGS_SOURCES` as of GRE-3464 — kept, not removed
    (NG-3), so it starts working for free if either blocker lifts."""
    m = re.search(r"Earnings Date[:\s]+([A-Za-z]+\.?\s+\d{1,2},\s+\d{4})", fetch.text)
    if not m:
        raise ValueError(f"no earnings date found for {symbol} on nasdaq page")
    dt = _parse_month_day_year(m.group(1).replace(".", ""))
    return [{"symbol": symbol, "date": dt.strftime("%Y-%m-%d"), "session": "unknown"}]


_YAHOO_CAL_ROW_RE = re.compile(
    r"\n(?P<symbol>[A-Z.]{1,6})\n\t[^\t\n]*\t"
    r"(?P<month>[A-Za-z]+) (?P<day>\d{1,2}), (?P<year>\d{4})"
    r"(?: at (?P<hour>\d{1,2}) (?P<ampm>AM|PM))?"
)


def parse_yahoo_earnings_calendar(fetch: FetchResult, symbol: str, now: datetime) -> list[dict]:
    rows: list[dict] = []
    seen_dates: set[str] = set()
    lo = (now + EARNINGS_WINDOW[0]).date()
    hi = (now + EARNINGS_WINDOW[1]).date()
    for m in _YAHOO_CAL_ROW_RE.finditer(fetch.text):
        if m.group("symbol") != symbol:
            continue
        try:
            dt = datetime.strptime(f"{m.group('month')} {m.group('day')} {m.group('year')}", "%B %d %Y")
        except ValueError:
            continue
        if not (lo <= dt.date() <= hi):
            continue
        hour24 = None
        if m.group("hour"):
            hour24 = int(m.group("hour")) % 12
            if m.group("ampm") == "PM":
                hour24 += 12
        date_str = dt.strftime("%Y-%m-%d")
        if date_str in seen_dates:
            continue
        seen_dates.add(date_str)
        rows.append({"symbol": symbol, "date": date_str, "session": _infer_session(hour24)})
    if not rows:
        raise ValueError(f"no upcoming/recent earnings rows found for {symbol} on yahoo calendar")
    return rows


def parse_stockanalysis_earnings(fetch: FetchResult, symbol: str, now: datetime) -> list[dict]:
    m = re.search(r"Earnings Date\t([A-Za-z]+ \d{1,2}, \d{4})", fetch.text)
    if not m:
        raise ValueError(f"no earnings date found for {symbol} on stockanalysis.com")
    dt = _parse_month_day_year(m.group(1))
    return [{"symbol": symbol, "date": dt.strftime("%Y-%m-%d"), "session": "unknown"}]


_YAHOO_NEWS_ITEM_RE = re.compile(
    r"^(?P<title>[^\n]+)\n(?:(?:LIVE|PREMIUM)\n)?(?P<source>[^\n•]+)\n•\n"
    r"(?P<num>\d+)(?P<unit>[hmd]) ago$",
    re.MULTILINE,
)
_YAHOO_NEWS_LINK_RE = re.compile(r'href="(https://[^"]+)" aria-label="([^"]+)"')

_UNIT_TO_DELTA = {
    "h": lambda n: timedelta(hours=n),
    "m": lambda n: timedelta(minutes=n),
    "d": lambda n: timedelta(days=n),
}


def parse_yahoo_news(fetch: FetchResult, symbol: str, now: datetime) -> list[dict]:
    link_map: dict[str, str] = {}
    if fetch.html:
        for url, title in _YAHOO_NEWS_LINK_RE.findall(fetch.html):
            link_map.setdefault(title.strip(), url)

    out: list[dict] = []
    seen_titles: set[str] = set()
    for m in _YAHOO_NEWS_ITEM_RE.finditer(fetch.text):
        title = m.group("title").strip()
        if title in seen_titles:
            continue
        seen_titles.add(title)
        published = now - _UNIT_TO_DELTA[m.group("unit")](int(m.group("num")))
        url = link_map.get(title, f"https://finance.yahoo.com/quote/{symbol}/news")
        out.append(
            {
                "symbol": symbol,
                "title": title,
                "source": m.group("source").strip(),
                "url": url,
                "published": _iso(published),
            }
        )
    if not out:
        raise ValueError(f"no headlines parsed from yahoo news for {symbol}")
    return out


def _parse_rss_items(text: str) -> list[dict]:
    items = []
    for body in re.findall(r"<item>(.*?)</item>", text, re.DOTALL):
        title_m = re.search(r"<title>(.*?)</title>", body, re.DOTALL)
        link_m = re.search(r"<link>(.*?)</link>", body, re.DOTALL)
        if not (title_m and link_m):
            continue
        pub_m = re.search(r"<pubDate>(.*?)</pubDate>", body, re.DOTALL)
        source_m = re.search(r'<source[^>]*>(.*?)</source>', body, re.DOTALL)
        items.append(
            {
                "title": title_m.group(1).strip(),
                "url": link_m.group(1).strip(),
                "pubdate": pub_m.group(1).strip() if pub_m else None,
                "source": source_m.group(1).strip() if source_m else None,
            }
        )
    return items


def parse_google_news_rss(fetch: FetchResult, symbol: str, now: datetime) -> list[dict]:
    out = []
    for item in _parse_rss_items(fetch.text):
        source = item["source"] or "Google News"
        title = item["title"]
        suffix = f" - {source}"
        if title.endswith(suffix):
            title = title[: -len(suffix)]
        try:
            published = _parse_rfc822(item["pubdate"]) if item["pubdate"] else now
        except (TypeError, ValueError):
            published = now
        out.append(
            {
                "symbol": symbol,
                "title": title,
                "source": source,
                "url": item["url"],
                "published": _iso(published),
            }
        )
    if not out:
        raise ValueError(f"no headlines parsed from google news rss for {symbol}")
    return out


def parse_yahoo_headlines_rss(fetch: FetchResult, symbol: str, now: datetime) -> list[dict]:
    """Yahoo's per-symbol headline RSS (GRE-3464, replaces marketwatch_rss —
    see module docstring). Unlike MarketWatch's general top-stories feed,
    this is scoped to the symbol server-side, so every item is relevant by
    construction — no title-substring filtering needed."""
    out = []
    for item in _parse_rss_items(fetch.text):
        try:
            published = _parse_rfc822(item["pubdate"]) if item["pubdate"] else now
        except (TypeError, ValueError):
            published = now
        out.append(
            {
                "symbol": symbol,
                "title": item["title"],
                "source": item["source"] or "Yahoo Finance",
                "url": item["url"],
                "published": _iso(published),
            }
        )
    if not out:
        raise ValueError(f"no headlines parsed from yahoo headlines rss for {symbol}")
    return out


def parse_yahoo_quote(fetch: FetchResult, symbol: str, now: datetime) -> dict:
    last_m = re.search(re.escape(f"({symbol})") + r"\n([\d,]+\.\d+)", fetch.text)
    prev_m = re.search(r"Previous Close\n([\d,]+\.\d+)", fetch.text)
    if not last_m or not prev_m:
        raise ValueError(f"could not find last/previous-close for {symbol} on yahoo quote page")
    return {
        "last": float(last_m.group(1).replace(",", "")),
        "prev_close": float(prev_m.group(1).replace(",", "")),
    }


def parse_yahoo_chart_quote(fetch: FetchResult, symbol: str, now: datetime) -> dict:
    """GRE-3464: replaces the structurally-broken `stooq_csv` fallback (its
    `/q/l/` last-quote endpoint 404s outright — see module docstring).
    Yahoo's chart JSON endpoint (plain HTTP, no browser) returns a daily
    `close` series; `last`/`prev_close` are the two most recent *usable*
    (non-null) entries — the newest bar can be null pre-market, so a plain
    `[-1]`/`[-2]` slice without filtering could silently pick up a stale or
    missing point."""
    try:
        payload = json.loads(fetch.text)
        result = payload["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"yahoo chart response missing close series for {symbol}") from exc
    usable = [c for c in closes if c is not None]
    if len(usable) < 2:
        raise ValueError(f"yahoo chart response has fewer than 2 usable closes for {symbol}")
    return {"last": float(usable[-1]), "prev_close": float(usable[-2])}


def parse_cboe_quote(fetch: FetchResult, symbol: str, now: datetime) -> dict:
    payload = json.loads(fetch.text)
    d = payload.get("data", {})
    last = d.get("current_price")
    prev = d.get("prev_day_close")
    if last is None or prev is None:
        raise ValueError(f"cboe delayed-quote response missing last/prev_close for {symbol}")
    return {"last": float(last), "prev_close": float(prev)}


ParseFn = Callable[[FetchResult, str, datetime], Any]


@dataclass(frozen=True)
class Source:
    id: str
    kind: str  # "browser" | "http"
    url: Callable[[str], str]
    parse: ParseFn


EARNINGS_SOURCES: list[Source] = [
    # GRE-3464: yahoo_earnings_calendar promoted to primary — it's the
    # source that actually works live. nasdaq_earnings demoted to last
    # (NG-3: still tried, never removed); see module docstring for the
    # JSON-API and stealth experiments that preceded this reorder.
    Source(
        "yahoo_earnings_calendar",
        "browser",
        lambda s: f"https://finance.yahoo.com/calendar/earnings?symbol={s}",
        parse_yahoo_earnings_calendar,
    ),
    Source(
        "stockanalysis_earnings",
        "browser",
        lambda s: f"https://stockanalysis.com/stocks/{s.lower()}/",
        parse_stockanalysis_earnings,
    ),
    Source(
        "nasdaq_earnings",
        "browser",
        lambda s: f"https://www.nasdaq.com/market-activity/stocks/{s.lower()}/earnings",
        parse_nasdaq_earnings,
    ),
]

HEADLINE_SOURCES: list[Source] = [
    Source(
        "yahoo_news",
        "browser",
        lambda s: f"https://finance.yahoo.com/quote/{s}/news",
        parse_yahoo_news,
    ),
    # GRE-3464: replaces marketwatch_rss (filtered a general top-stories
    # feed by symbol substring — almost always empty; see module
    # docstring). Yahoo's per-symbol RSS is scoped server-side and plain
    # HTTP (no browser needed), so it's also a genuinely browserless
    # fallback — not just a swap of one browser source for another.
    Source(
        "yahoo_headlines_rss",
        "http",
        lambda s: f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={s}&region=US&lang=en-US",
        parse_yahoo_headlines_rss,
    ),
    Source(
        "google_news_rss",
        "browser",
        lambda s: f"https://news.google.com/rss/search?q={s}+stock&hl=en-US&gl=US&ceid=US:en",
        parse_google_news_rss,
    ),
]

QUOTE_SOURCES: list[Source] = [
    Source(
        "yahoo_quote",
        "browser",
        lambda s: f"https://finance.yahoo.com/quote/{s}",
        parse_yahoo_quote,
    ),
    # GRE-3464: replaces stooq_csv (its /q/l/ last-quote endpoint 404s on
    # every URL shape tried live — see module docstring). Yahoo's chart
    # JSON endpoint is also plain HTTP (no browser needed).
    Source(
        "yahoo_chart_quote",
        "http",
        lambda s: f"https://query1.finance.yahoo.com/v8/finance/chart/{s}?range=5d&interval=1d",
        parse_yahoo_chart_quote,
    ),
    Source(
        "cboe_quotes",
        "http",
        lambda s: f"https://cdn.cboe.com/api/global/delayed_quotes/quotes/{s.upper()}.json",
        parse_cboe_quote,
    ),
]


async def _try_source(
    source: Source,
    symbol: str,
    now: datetime,
    *,
    recording: bool,
    unreachable: set[str],
    semaphore: asyncio.Semaphore,
    sessions: list[str],
    warnings: list[str],
) -> Optional[Any]:
    """Fetch + parse one source. Returns None (and logs a warning) on any
    failure — including a forced-unreachable override — so callers can move
    on to the next fallback without ever raising."""
    if source.id in unreachable:
        warnings.append(f"{source.id} forced unreachable for {symbol} (override)")
        return None
    url = source.url(symbol)
    try:
        if source.kind == "browser":
            async with semaphore:
                fetch = await _browser_fetch(url, recording=recording)
            if fetch.session_id:
                sessions.append(fetch.session_id)
        else:
            fetch = await _http_fetch(url)
        return source.parse(fetch, symbol, now)
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any source may fail
        warnings.append(f"{source.id} failed for {symbol}: {exc}")
        return None


async def _collect_first_success(
    sources: list[Source],
    symbol: str,
    now: datetime,
    **kwargs: Any,
) -> Optional[Any]:
    for source in sources:
        result = await _try_source(source, symbol, now, **kwargs)
        if result:
            return result
    return None


async def _collect_earnings(symbol: str, now: datetime, **kwargs: Any) -> list[dict]:
    rows = await _collect_first_success(EARNINGS_SOURCES, symbol, now, **kwargs)
    return rows or []


async def _collect_headlines(symbol: str, now: datetime, **kwargs: Any) -> list[dict]:
    """Merge headlines across sources (dedup by url) until reaching
    HEADLINE_TARGET, trying the next fallback only if still short."""
    merged: list[dict] = []
    seen_urls: set[str] = set()
    for source in HEADLINE_SOURCES:
        result = await _try_source(source, symbol, now, **kwargs)
        for headline in result or []:
            if headline["url"] in seen_urls:
                continue
            seen_urls.add(headline["url"])
            merged.append(headline)
        if len(merged) >= HEADLINE_TARGET:
            break
    return merged


async def _collect_quotes(symbol: str, now: datetime, **kwargs: Any) -> Optional[dict]:
    return await _collect_first_success(QUOTE_SOURCES, symbol, now, **kwargs)


def _env_unreachable() -> set[str]:
    raw = os.environ.get(FORCE_UNREACHABLE_ENV, "")
    return {x.strip() for x in raw.split(",") if x.strip()}


async def scrape(
    symbols: list[str],
    *,
    recording: bool = True,
    force_unreachable: Optional[set[str]] = None,
    concurrency: int = 3,
) -> ScrapedData:
    """Scrape earnings/headlines/quotes for `symbols` (<=5) and return a
    validated `ScrapedData`. Never raises for a single source's failure —
    NG-5 partial-output-plus-warnings is the contract."""
    now = datetime.now(timezone.utc)
    unreachable = force_unreachable if force_unreachable is not None else _env_unreachable()
    semaphore = asyncio.Semaphore(max(1, min(concurrency, 3)))  # NG-3: <=3 concurrent browsers

    universe = [s.strip().upper() for s in symbols if s.strip()]
    if not universe:
        raise ValueError("scrape() requires at least one symbol")
    if len(universe) > 5:
        raise ValueError(f"scrape() supports at most 5 symbols, got {len(universe)}")

    sessions: list[str] = []
    warnings: list[str] = []
    common = dict(
        recording=recording,
        unreachable=unreachable,
        semaphore=semaphore,
        sessions=sessions,
        warnings=warnings,
    )

    jobs: list[tuple[str, str, Awaitable[Any]]] = []
    for symbol in universe:
        jobs.append(("earnings", symbol, _collect_earnings(symbol, now, **common)))
        jobs.append(("headlines", symbol, _collect_headlines(symbol, now, **common)))
        jobs.append(("quotes", symbol, _collect_quotes(symbol, now, **common)))

    results = await asyncio.gather(*(job[2] for job in jobs))

    earnings_all: list[dict] = []
    headlines_all: list[dict] = []
    quotes: dict[str, dict] = {}
    for (kind, symbol, _), result in zip(jobs, results):
        if kind == "earnings":
            earnings_all.extend(result)
        elif kind == "headlines":
            headlines_all.extend(result)
        elif kind == "quotes" and result:
            quotes[symbol] = result

    for symbol in universe:
        if symbol not in quotes:
            warnings.append(f"no quote data available for {symbol} from any source")

    # GRE-3464: every browser session opened above shares this run's single
    # `recording` flag (folded into `common` and forwarded uniformly), so
    # the recorded-and-replayable subset of `sessions` is either all of it
    # or none of it — Solari's replay id is the session id itself
    # (solari.sessions.download_replay(session_id), see solari_client.py's
    # PageResult.replay_hint), so no separate id needs minting here.
    replays = list(sessions) if recording else []

    return ScrapedData(
        as_of=_iso(now),
        universe=universe,
        earnings=[Earnings(**r) for r in earnings_all],
        headlines=[Headline(**h) for h in headlines_all],
        quotes={sym: Quote(**q) for sym, q in quotes.items()},
        provenance=Provenance(sessions=sessions, replays=replays),
        warnings=warnings,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Scrape earnings dates, headlines, and quotes for an options-desk symbol universe."
    )
    p.add_argument("--symbols", required=True, help="Comma-separated symbols, max 5 (e.g. AAPL,NVDA)")
    p.add_argument("--out", required=True, help="Output path for scraped_data.json")
    p.add_argument(
        "--no-recording",
        action="store_true",
        help="Disable session recording (recording is on by default per GRE-3460)",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=3,
        help="Max concurrent browser sessions, capped at 3 (free-tier limit)",
    )
    return p


def main(argv: Optional[list[str]] = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _build_arg_parser().parse_args(argv)
    symbols = [s for s in (x.strip() for x in args.symbols.split(",")) if s]
    if not symbols:
        raise SystemExit("--symbols must contain at least one symbol")
    if len(symbols) > 5:
        raise SystemExit(f"--symbols supports at most 5 symbols, got {len(symbols)}")

    data = asyncio.run(scrape(symbols, recording=not args.no_recording, concurrency=args.concurrency))
    payload = data.to_dict()
    validate_scraped(payload)  # fail loudly before writing anything invalid

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n")

    print(
        f"wrote {out_path} — "
        f"{len(payload['earnings'])} earnings rows, "
        f"{len(payload['headlines'])} headlines, "
        f"{len(payload['quotes'])} quotes, "
        f"{len(payload['warnings'])} warnings, "
        f"{len(payload['provenance']['sessions'])} browser sessions"
    )


if __name__ == "__main__":
    main()
