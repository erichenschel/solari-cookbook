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
  - Nasdaq's earnings page (ticket's named primary) returned
    `net::ERR_HTTP2_PROTOCOL_ERROR` from the vanilla cloud browser on every
    attempt — kept as the coded primary per the ticket, but expect it to
    always fall through to the Yahoo/StockAnalysis fallbacks live.
  - Stooq's `/q/l/` CSV endpoint 404s outright (site restructure), and even
    if it resolved, that field-set has no previous-close column, so it can
    never alone satisfy the quotes contract. Kept as the coded fallback
    (raises a documented, specific error) so the chain still reaches CBOE.
  - Yahoo Finance (earnings calendar, per-symbol news, quote page),
    StockAnalysis.com (earnings), Google News RSS, MarketWatch RSS, and CBOE
    delayed quotes (JSON) all verified working against a vanilla browser /
    plain HTTP at build time.
"""

from __future__ import annotations

import argparse
import asyncio
import email.utils
import json
import logging
import os
import re
from dataclasses import dataclass, field
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


# ---------------------------------------------------------------------------
# shared parsing helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# earnings parsers
# ---------------------------------------------------------------------------


def parse_nasdaq_earnings(fetch: FetchResult, symbol: str, now: datetime) -> list[dict]:
    """Best-effort parser for Nasdaq's earnings page. Unverified live — the
    page could not be reached at all from the vanilla cloud browser at build
    time (`net::ERR_HTTP2_PROTOCOL_ERROR`, see module docstring)."""
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


# ---------------------------------------------------------------------------
# headline parsers
# ---------------------------------------------------------------------------

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


def parse_marketwatch_rss(fetch: FetchResult, symbol: str, now: datetime) -> list[dict]:
    """MarketWatch's public RSS is a general top-stories feed, not
    per-symbol — filter to items that actually mention the symbol."""
    out = []
    for item in _parse_rss_items(fetch.text):
        if symbol.lower() not in item["title"].lower():
            continue
        try:
            published = _parse_rfc822(item["pubdate"]) if item["pubdate"] else now
        except (TypeError, ValueError):
            published = now
        out.append(
            {
                "symbol": symbol,
                "title": item["title"],
                "source": "MarketWatch",
                "url": item["url"],
                "published": _iso(published),
            }
        )
    if not out:
        raise ValueError(f"no symbol-relevant headlines in marketwatch topstories for {symbol}")
    return out


# ---------------------------------------------------------------------------
# quote parsers
# ---------------------------------------------------------------------------


def parse_yahoo_quote(fetch: FetchResult, symbol: str, now: datetime) -> dict:
    last_m = re.search(re.escape(f"({symbol})") + r"\n([\d,]+\.\d+)", fetch.text)
    prev_m = re.search(r"Previous Close\n([\d,]+\.\d+)", fetch.text)
    if not last_m or not prev_m:
        raise ValueError(f"could not find last/previous-close for {symbol} on yahoo quote page")
    return {
        "last": float(last_m.group(1).replace(",", "")),
        "prev_close": float(prev_m.group(1).replace(",", "")),
    }


def parse_stooq_csv(fetch: FetchResult, symbol: str, now: datetime) -> dict:
    lines = [ln for ln in fetch.text.strip().splitlines() if ln.strip()]
    if len(lines) < 2:
        raise ValueError(f"stooq CSV for {symbol} has no data row (endpoint unreachable/moved)")
    header = [h.strip() for h in lines[0].split(",")]
    values = [v.strip() for v in lines[1].split(",")]
    row = dict(zip(header, values))
    if row.get("Close") in (None, "", "N/D"):
        raise ValueError(f"stooq has no quote data for {symbol}")
    # Stooq's last-quote field-set (`sd2t2ohlcv`) has no previous-close
    # column — even when the endpoint is reachable it cannot alone satisfy
    # the {last, prev_close} contract. Documented in the module docstring.
    raise ValueError(
        "stooq CSV field-set has no previous-close column; cannot satisfy the quotes contract alone"
    )


def parse_cboe_quote(fetch: FetchResult, symbol: str, now: datetime) -> dict:
    payload = json.loads(fetch.text)
    d = payload.get("data", {})
    last = d.get("current_price")
    prev = d.get("prev_day_close")
    if last is None or prev is None:
        raise ValueError(f"cboe delayed-quote response missing last/prev_close for {symbol}")
    return {"last": float(last), "prev_close": float(prev)}


# ---------------------------------------------------------------------------
# source registry
# ---------------------------------------------------------------------------

ParseFn = Callable[[FetchResult, str, datetime], Any]


@dataclass(frozen=True)
class Source:
    id: str
    kind: str  # "browser" | "http"
    url: Callable[[str], str]
    parse: ParseFn


EARNINGS_SOURCES: list[Source] = [
    Source(
        "nasdaq_earnings",
        "browser",
        lambda s: f"https://www.nasdaq.com/market-activity/stocks/{s.lower()}/earnings",
        parse_nasdaq_earnings,
    ),
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
]

HEADLINE_SOURCES: list[Source] = [
    Source(
        "yahoo_news",
        "browser",
        lambda s: f"https://finance.yahoo.com/quote/{s}/news",
        parse_yahoo_news,
    ),
    Source(
        "marketwatch_rss",
        "browser",
        lambda s: "https://www.marketwatch.com/rss/topstories",
        parse_marketwatch_rss,
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
    Source(
        "stooq_csv",
        "http",
        lambda s: f"https://stooq.com/q/l/?s={s.lower()}.us&f=sd2t2ohlcv&h&e=csv",
        parse_stooq_csv,
    ),
    Source(
        "cboe_quotes",
        "http",
        lambda s: f"https://cdn.cboe.com/api/global/delayed_quotes/quotes/{s.upper()}.json",
        parse_cboe_quote,
    ),
]


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


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

    return ScrapedData(
        as_of=_iso(now),
        universe=universe,
        earnings=[Earnings(**r) for r in earnings_all],
        headlines=[Headline(**h) for h in headlines_all],
        quotes={sym: Quote(**q) for sym, q in quotes.items()},
        provenance=Provenance(sessions=sessions),
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


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
