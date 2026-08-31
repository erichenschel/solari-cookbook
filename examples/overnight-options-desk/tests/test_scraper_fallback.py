"""Hermetic orchestration tests — no network. `desk.scraper._browser_fetch`
and `_http_fetch` are monkeypatched to serve the same saved fixtures the
parser tests use, keyed by URL. This exercises the fallback chain (AC-3):
forcing a primary source unreachable (via `force_unreachable`, the same
injectable the `--symbols` CLI honors through DESK_SCRAPER_FORCE_UNREACHABLE)
must still produce schema-valid output, with the failure recorded in
`warnings[]` and the fallback source's data present."""

import json

import pytest

import desk.scraper as scraper
from desk.contracts import validate_scraped


def _fixture_router(fixtures_dir):
    """Map each source's URL shape to its saved fixture content, mimicking
    what a real fetch would return. Any URL not matched raises, so an
    accidental extra source call fails loudly instead of silently passing."""

    def load(name: str, *, as_html: bool = False) -> scraper.FetchResult:
        text = (fixtures_dir / "scraper" / name).read_text()
        if as_html:
            html = (fixtures_dir / "scraper" / "headlines_yahoo_news.html").read_text()
            return scraper.FetchResult(text=text, html=html)
        return scraper.FetchResult(text=text)

    async def fake_browser_fetch(url: str, *, recording: bool) -> scraper.FetchResult:
        if "nasdaq.com" in url:
            raise RuntimeError("net::ERR_HTTP2_PROTOCOL_ERROR (simulated, matches build-time finding)")
        if "finance.yahoo.com/calendar/earnings" in url:
            result = load("earnings_yahoo_calendar.txt")
            result.session_id = "fake-sess-yahoo-cal"
            return result
        if "stockanalysis.com" in url:
            result = load("stockanalysis_page.txt")
            result.session_id = "fake-sess-stockanalysis"
            return result
        if "finance.yahoo.com/quote/" in url and url.endswith("/news"):
            result = load("headlines_yahoo_news.txt", as_html=True)
            result.session_id = "fake-sess-yahoo-news"
            return result
        if "marketwatch.com/rss" in url:
            result = load("headlines_marketwatch_rss.xml")
            result.session_id = "fake-sess-marketwatch"
            return result
        if "news.google.com/rss" in url:
            result = load("headlines_google_news_rss.xml")
            result.session_id = "fake-sess-google-news"
            return result
        if "finance.yahoo.com/quote/" in url:
            result = load("quotes_yahoo.txt")
            result.session_id = "fake-sess-yahoo-quote"
            return result
        raise AssertionError(f"unexpected browser fetch url in test: {url}")

    async def fake_http_fetch(url: str) -> scraper.FetchResult:
        if "stooq.com" in url:
            raise RuntimeError("404 (simulated, matches build-time finding: endpoint moved)")
        if "cboe.com" in url:
            return load("quotes_cboe.json")
        raise AssertionError(f"unexpected http fetch url in test: {url}")

    return fake_browser_fetch, fake_http_fetch


@pytest.fixture
def patched_fetchers(monkeypatch, fixtures_dir):
    fake_browser_fetch, fake_http_fetch = _fixture_router(fixtures_dir)
    monkeypatch.setattr(scraper, "_browser_fetch", fake_browser_fetch)
    monkeypatch.setattr(scraper, "_http_fetch", fake_http_fetch)


async def test_scrape_happy_path_produces_schema_valid_output(patched_fetchers):
    data = await scraper.scrape(["AAPL"], recording=True)
    payload = data.to_dict()
    validate_scraped(payload)  # must not raise
    assert payload["universe"] == ["AAPL"]
    assert len(payload["earnings"]) >= 1
    assert len(payload["headlines"]) >= 3
    assert "AAPL" in payload["quotes"]
    assert payload["quotes"]["AAPL"] == {"last": 316.85, "prev_close": 319.70}
    assert payload["provenance"]["sessions"], "recording=True should populate session ids"


async def test_scrape_with_primary_earnings_forced_unreachable_falls_back(patched_fetchers):
    """AC-3: nasdaq_earnings forced unreachable -> output still schema-valid,
    earnings still populated via the yahoo fallback, and the forced failure
    is recorded in warnings[]."""
    data = await scraper.scrape(["AAPL"], force_unreachable={"nasdaq_earnings"})
    payload = data.to_dict()
    validate_scraped(payload)  # must not raise
    assert len(payload["earnings"]) >= 1  # fell back to yahoo calendar successfully
    assert any("nasdaq_earnings" in w and "AAPL" in w for w in payload["warnings"])


async def test_scrape_with_all_earnings_sources_unreachable_degrades_gracefully(patched_fetchers):
    """Every earnings source forced unreachable -> no earnings rows, but the
    file is still schema-valid (empty array is legal) and every failure is
    recorded — never a crash (NG-5)."""
    data = await scraper.scrape(
        ["AAPL"],
        force_unreachable={"nasdaq_earnings", "yahoo_earnings_calendar", "stockanalysis_earnings"},
    )
    payload = data.to_dict()
    validate_scraped(payload)  # must not raise even with earnings == []
    assert payload["earnings"] == []
    assert len(payload["warnings"]) >= 3
    # headlines and quotes were untouched and should still be fully populated
    assert len(payload["headlines"]) >= 3
    assert "AAPL" in payload["quotes"]


async def test_scrape_with_yahoo_quote_and_stooq_unreachable_falls_back_to_cboe(patched_fetchers):
    """Two-level fallback: yahoo_quote forced unreachable, stooq's real
    build-time failure (missing prev_close) still applies -> cboe_quotes
    (plain HTTP, last resort) supplies the quote."""
    data = await scraper.scrape(["AAPL"], force_unreachable={"yahoo_quote"})
    payload = data.to_dict()
    validate_scraped(payload)
    assert payload["quotes"]["AAPL"] == {"last": 317.08, "prev_close": 319.7}
    assert any("yahoo_quote" in w for w in payload["warnings"])
    assert any("stooq_csv" in w for w in payload["warnings"])


async def test_scrape_with_all_quote_sources_unreachable_omits_symbol_and_warns(patched_fetchers):
    data = await scraper.scrape(
        ["AAPL"], force_unreachable={"yahoo_quote", "stooq_csv", "cboe_quotes"}
    )
    payload = data.to_dict()
    validate_scraped(payload)  # must not raise
    assert "AAPL" not in payload["quotes"]
    assert any("no quote data available for AAPL" in w for w in payload["warnings"])


async def test_env_flag_drives_force_unreachable_default(monkeypatch, patched_fetchers):
    monkeypatch.setenv(scraper.FORCE_UNREACHABLE_ENV, "nasdaq_earnings, yahoo_quote")
    assert scraper._env_unreachable() == {"nasdaq_earnings", "yahoo_quote"}


async def test_scrape_never_raises_when_every_source_for_every_type_fails(patched_fetchers):
    """Full-blackout case: nothing at all is reachable -> still a
    schema-valid, mostly-empty file with a warning per attempted source,
    never an exception (NG-5)."""
    all_ids = {s.id for s in scraper.EARNINGS_SOURCES + scraper.HEADLINE_SOURCES + scraper.QUOTE_SOURCES}
    data = await scraper.scrape(["AAPL"], force_unreachable=all_ids)
    payload = data.to_dict()
    validate_scraped(payload)  # must not raise
    assert payload["earnings"] == []
    assert payload["headlines"] == []
    assert payload["quotes"] == {}
    assert len(payload["warnings"]) >= len(all_ids)
    assert payload["provenance"]["sessions"] == []


def test_scrape_rejects_more_than_five_symbols():
    with pytest.raises(ValueError):
        import asyncio

        asyncio.run(scraper.scrape(["A", "B", "C", "D", "E", "F"]))
