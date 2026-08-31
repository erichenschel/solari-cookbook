"""Hermetic parser tests — no network. Every parser is exercised against a
real page/RSS snapshot saved under fixtures/scraper/ during GRE-3460
build-time source verification (see desk/scraper.py module docstring for
which sources actually worked live vs. fell back)."""

from datetime import datetime, timezone

import pytest

from desk.scraper import (
    FetchResult,
    parse_cboe_quote,
    parse_google_news_rss,
    parse_marketwatch_rss,
    parse_stockanalysis_earnings,
    parse_stooq_csv,
    parse_yahoo_earnings_calendar,
    parse_yahoo_news,
    parse_yahoo_quote,
)

pytestmark = pytest.mark.filterwarnings("ignore")

NOW = datetime(2026, 8, 31, 22, 50, 0, tzinfo=timezone.utc)


def _load(fixtures_dir, name: str) -> str:
    return (fixtures_dir / "scraper" / name).read_text()


# --------------------------------------------------------------------------
# earnings
# --------------------------------------------------------------------------


def test_parse_yahoo_earnings_calendar(fixtures_dir):
    fetch = FetchResult(text=_load(fixtures_dir, "earnings_yahoo_calendar.txt"))
    rows = parse_yahoo_earnings_calendar(fetch, "AAPL", NOW)
    assert rows
    for row in rows:
        assert row["symbol"] == "AAPL"
        datetime.strptime(row["date"], "%Y-%m-%d")  # must be a valid date
        assert row["session"] in ("bmo", "amc", "unknown")
    # the "October 29, 2026 at 4 PM EDT" row must be picked up as amc
    assert any(r["date"] == "2026-10-29" and r["session"] == "amc" for r in rows)


def test_parse_yahoo_earnings_calendar_no_rows_for_unknown_symbol(fixtures_dir):
    fetch = FetchResult(text=_load(fixtures_dir, "earnings_yahoo_calendar.txt"))
    with pytest.raises(ValueError):
        parse_yahoo_earnings_calendar(fetch, "ZZZZ", NOW)


def test_parse_stockanalysis_earnings(fixtures_dir):
    fetch = FetchResult(text=_load(fixtures_dir, "stockanalysis_page.txt"))
    rows = parse_stockanalysis_earnings(fetch, "AAPL", NOW)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["date"] == "2026-07-30"
    assert rows[0]["session"] == "unknown"


# --------------------------------------------------------------------------
# headlines
# --------------------------------------------------------------------------


def test_parse_yahoo_news(fixtures_dir):
    fetch = FetchResult(
        text=_load(fixtures_dir, "headlines_yahoo_news.txt"),
        html=_load(fixtures_dir, "headlines_yahoo_news.html"),
    )
    headlines = parse_yahoo_news(fetch, "AAPL", NOW)
    assert len(headlines) >= 3
    for h in headlines:
        assert h["symbol"] == "AAPL"
        assert h["title"]
        assert h["source"]
        assert h["url"].startswith("https://")
        datetime.strptime(h["published"], "%Y-%m-%dT%H:%M:%SZ")
    # at least one headline resolved a real article URL from the HTML link map
    assert any("finance.yahoo.com/video" in h["url"] or "finance.yahoo.com/technology" in h["url"]
               for h in headlines)


def test_parse_yahoo_news_falls_back_to_symbol_page_url_without_html(fixtures_dir):
    fetch = FetchResult(text=_load(fixtures_dir, "headlines_yahoo_news.txt"), html=None)
    headlines = parse_yahoo_news(fetch, "AAPL", NOW)
    assert headlines
    assert all(h["url"] == "https://finance.yahoo.com/quote/AAPL/news" for h in headlines)


def test_parse_google_news_rss(fixtures_dir):
    fetch = FetchResult(text=_load(fixtures_dir, "headlines_google_news_rss.xml"))
    headlines = parse_google_news_rss(fetch, "AAPL", NOW)
    assert len(headlines) >= 3
    for h in headlines:
        assert h["symbol"] == "AAPL"
        assert h["title"]
        assert not h["title"].endswith(f" - {h['source']}")  # duplicate suffix stripped
        assert h["url"].startswith("https://")
        datetime.strptime(h["published"], "%Y-%m-%dT%H:%M:%SZ")


def test_parse_marketwatch_rss_filters_to_symbol(fixtures_dir):
    fetch = FetchResult(text=_load(fixtures_dir, "headlines_marketwatch_rss.xml"))
    # the fixture is general top-stories with no AAPL mentions -> no rows
    with pytest.raises(ValueError):
        parse_marketwatch_rss(fetch, "AAPL", NOW)
    # but a term that *does* appear in the fixture's top stories should match
    fetch2 = FetchResult(text=_load(fixtures_dir, "headlines_marketwatch_rss.xml"))
    headlines = parse_marketwatch_rss(fetch2, "Amazon", NOW)
    assert headlines
    assert all("amazon" in h["title"].lower() for h in headlines)
    assert all(h["source"] == "MarketWatch" for h in headlines)


# --------------------------------------------------------------------------
# quotes
# --------------------------------------------------------------------------


def test_parse_yahoo_quote(fixtures_dir):
    fetch = FetchResult(text=_load(fixtures_dir, "quotes_yahoo.txt"))
    quote = parse_yahoo_quote(fetch, "AAPL", NOW)
    assert quote == {"last": 316.85, "prev_close": 319.70}


def test_parse_cboe_quote(fixtures_dir):
    fetch = FetchResult(text=_load(fixtures_dir, "quotes_cboe.json"))
    quote = parse_cboe_quote(fetch, "AAPL", NOW)
    assert quote["last"] == pytest.approx(317.08)
    assert quote["prev_close"] == pytest.approx(319.7)


def test_parse_stooq_csv_documents_missing_prev_close():
    """Stooq's last-quote CSV field-set has no previous-close column —
    verified at build time (see module docstring); the parser always raises
    with a specific, documented reason so the fallback chain reaches CBOE."""
    fetch = FetchResult(text="Symbol,Date,Time,Open,High,Low,Close,Volume\nAAPL.US,2026-08-31,22:00:00,319.60,321.24,312.80,316.85,41195767\n")
    with pytest.raises(ValueError, match="previous-close"):
        parse_stooq_csv(fetch, "AAPL", NOW)


def test_parse_stooq_csv_raises_on_empty_response():
    fetch = FetchResult(text="not found")
    with pytest.raises(ValueError):
        parse_stooq_csv(fetch, "AAPL", NOW)
