"""Hermetic tests for desk/brief.py — no network, no Solari key needed.

Covers AC-1 (all five sections present, hostile-input escaping, whole file
end-to-end via the literal `python -m desk.brief` CLI) and AC-4 (disclaimer
+ provenance footer present)."""

import copy
import json
import re
import subprocess
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from desk.brief import (
    HEADLINE_CAP,
    _dedupe_and_group_headlines,
    _earnings_within,
    _interpret_signal,
    _newest_first_capped,
    _stretched_chip_text,
    _upcoming_earnings,
    _verdict_tally,
    main,
    render_brief,
)
from desk.contracts import Earnings, Headline, ScrapedData, Signals, SymbolSignal

pytestmark = pytest.mark.filterwarnings("ignore")

PACKAGE_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def scraped(fixtures_dir) -> ScrapedData:
    return ScrapedData.from_dict(json.loads((fixtures_dir / "scraped_data.json").read_text()))


@pytest.fixture
def signals(fixtures_dir) -> Signals:
    return Signals.from_dict(json.loads((fixtures_dir / "signals.json").read_text()))


@pytest.fixture
def rendered(scraped, signals) -> str:
    return render_brief(scraped, signals)


def test_renders_valid_html_shell(rendered):
    assert rendered.strip().startswith("<!doctype html>")
    assert "<html" in rendered and "</html>" in rendered
    assert rendered.count("<html") == 1


def test_section_1_header_present(rendered):
    assert 'id="header"' in rendered
    assert "2026-08-31 06:00 UTC" in rendered  # as_of, formatted
    for sym in ["AAPL", "MSFT", "NVDA", "TSLA"]:
        assert sym in rendered


def test_section_2_ranked_signal_table_present(rendered):
    assert 'id="signals"' in rendered
    for needle in [
        "OU z-score",
        "Half-life",
        "Mom 5d",
        "Verdict",
        "bullish",
        "neutral",
        "avoid",
    ]:
        assert needle in rendered
    # MSFT has no signals entry — should be flagged, not silently dropped.
    assert "No signal coverage" in rendered
    assert "MSFT" in rendered.split('id="signals"')[1].split("</section>")[0]


def test_section_2_ranked_by_abs_zscore(rendered):
    # NVDA |1.92| > TSLA |1.15| > AAPL |0.42| — NVDA should lead.
    signals_section = rendered.split('id="signals"')[1].split("</section>")[0]
    nvda_pos = signals_section.index("NVDA")
    tsla_pos = signals_section.index("TSLA")
    aapl_pos = signals_section.index("AAPL")
    assert nvda_pos < tsla_pos < aapl_pos


def test_section_3_earnings_callouts_present(rendered):
    assert 'id="earnings"' in rendered
    earnings_section = rendered.split('id="earnings"')[1].split("</section>")[0]
    assert "NVDA" in earnings_section
    assert "TSLA" in earnings_section
    assert "2026-09-02" in earnings_section
    assert "2026-09-04" in earnings_section
    assert "Before open" in earnings_section  # bmo
    assert "After close" in earnings_section  # amc


# GRE-3464: "Earnings in window" must be forward-looking-only, one row per
# symbol (the soonest upcoming date), out to EARNINGS_DISPLAY_WINDOW_DAYS.
# Eric caught a real rendered bug this covers: a past NVDA date
# (2026-08-26) shown alongside a future one (2026-11-17) on a run whose
# as_of was 2026-08-31.

_AS_OF = "2026-08-31T06:00:00Z"  # matches the `scraped` fixture's as_of


def test_upcoming_earnings_excludes_past_dates():
    earnings = [
        Earnings(symbol="NVDA", date="2026-08-26", session="amc"),  # 5 days before as_of
        Earnings(symbol="AAPL", date="2026-09-15", session="amc"),
    ]
    rows = _upcoming_earnings(earnings, _AS_OF)
    assert [e.symbol for e in rows] == ["AAPL"]


def test_upcoming_earnings_dedupes_symbol_to_soonest_upcoming():
    earnings = [
        Earnings(symbol="NVDA", date="2026-11-17", session="unknown"),
        Earnings(symbol="NVDA", date="2026-09-10", session="amc"),  # earlier -> wins
        Earnings(symbol="NVDA", date="2026-08-26", session="amc"),  # past -> excluded outright
    ]
    rows = _upcoming_earnings(earnings, _AS_OF)
    assert len(rows) == 1
    assert rows[0].symbol == "NVDA"
    assert rows[0].date == "2026-09-10"


def test_upcoming_earnings_includes_run_day_itself():
    """Boundary: a report dated the same calendar day as `as_of` is still
    "upcoming" to a reader of this morning's brief, not "past" -- the
    lower bound is inclusive."""
    earnings = [Earnings(symbol="TSLA", date="2026-08-31", session="bmo")]
    rows = _upcoming_earnings(earnings, _AS_OF)
    assert [e.symbol for e in rows] == ["TSLA"]


def test_upcoming_earnings_excludes_beyond_display_window():
    earnings = [Earnings(symbol="MSFT", date="2027-06-01", session="amc")]
    assert _upcoming_earnings(earnings, _AS_OF) == []


def test_upcoming_earnings_includes_display_window_upper_boundary():
    earnings = [Earnings(symbol="MSFT", date="2026-11-29", session="amc")]  # exactly +90d
    assert [e.symbol for e in _upcoming_earnings(earnings, _AS_OF)] == ["MSFT"]


def test_rendered_earnings_section_excludes_past_and_dedupes(scraped, signals):
    """End-to-end: render_brief() must reflect the same filtering, not just
    the helper in isolation."""
    hostile_scraped = replace(
        scraped,
        earnings=[
            Earnings(symbol="NVDA", date="2026-08-26", session="amc"),  # past
            Earnings(symbol="NVDA", date="2026-11-17", session="unknown"),  # keep
            Earnings(symbol="NVDA", date="2026-09-10", session="amc"),  # soonest -> keep, not 11-17
        ],
    )
    out = render_brief(hostile_scraped, signals)
    earnings_section = out.split('id="earnings"')[1].split("</section>")[0]
    assert "2026-08-26" not in earnings_section
    assert "2026-11-17" not in earnings_section
    assert earnings_section.count(">NVDA<") == 1
    assert "2026-09-10" in earnings_section


def test_section_4_headlines_with_source_links_present(rendered):
    assert 'id="headlines"' in rendered
    headlines_section = rendered.split('id="headlines"')[1].split("</section>")[0]
    assert "Reuters" in headlines_section
    assert "Bloomberg" in headlines_section
    assert "https://example.com/news/apple-supplier-ramp" in headlines_section
    assert "<a href=" in headlines_section
    # null-symbol headline should land in a market-wide group, not be dropped
    assert "Fed officials signal no change" in headlines_section
    assert "Market-wide" in headlines_section


def test_section_5_provenance_footer_present(rendered):
    assert 'id="provenance"' in rendered
    footer = rendered.split('id="provenance"')[1]
    assert "Provenance: 2 recorded browser sessions" in footer
    assert "full ids in the run" in footer and "scraped_data.json" in footer
    # GRE-3464: full raw session ids (they can embed internal hostnames)
    # must never appear in the brief — only the last-8-chars short form,
    # and only once (not duplicated as a second "replay ids" list).
    assert "sess_7f3a9c21" not in footer
    assert "sess_9b21e04d" not in footer
    assert footer.count("7f3a9c21") == 1
    assert footer.count("9b21e04d") == 1


def test_provenance_footer_omits_replay_mention_when_none_recorded(rendered):
    # the bundled fixture has no `provenance.replays` — the claim line must
    # not invent a replay mention it can't back up.
    footer = rendered.split('id="provenance"')[1]
    assert "download_replay" not in footer


def test_provenance_footer_reports_no_sessions_when_none_recorded(scraped, signals):
    empty_prov = replace(scraped, provenance=replace(scraped.provenance, sessions=[], replays=[]))
    out = render_brief(empty_prov, signals)
    footer = out.split('id="provenance"')[1]
    assert "No browser sessions were recorded" in footer


def test_hostile_headline_title_is_escaped(scraped, signals):
    hostile = copy.deepcopy(scraped)
    hostile.headlines = list(hostile.headlines)
    hostile.headlines[0].title = '<script>alert(1)</script> "onmouseover=alert(2)'

    out = render_brief(hostile, signals)

    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
    # the raw quote immediately followed by the injected attribute must not
    # survive unescaped, whatever entity form the escaper uses for `"`
    assert '"onmouseover=alert(2)' not in out


def test_hostile_headline_url_scheme_is_rejected(scraped, signals):
    hostile = copy.deepcopy(scraped)
    hostile.headlines = list(hostile.headlines)
    hostile.headlines[0].url = "javascript:alert(document.cookie)"

    out = render_brief(hostile, signals)

    assert "javascript:alert" not in out
    assert 'href="#"' in out


def test_hostile_source_and_symbol_text_is_escaped(scraped, signals):
    hostile = copy.deepcopy(scraped)
    hostile.headlines = list(hostile.headlines)
    hostile.headlines[0].source = '<img src=x onerror=alert(3)>'

    out = render_brief(hostile, signals)

    assert "<img src=x onerror=alert(3)>" not in out
    assert "&lt;img" in out


def test_disclaimer_present(rendered):
    assert "Research only" in rendered
    assert "not investment advice" in rendered


def test_no_script_tags_and_uses_plain_tables_and_svg(rendered):
    assert "<script" not in rendered
    assert " onclick=" not in rendered
    assert "<table" in rendered
    assert "<svg" in rendered


def test_no_external_resource_references(rendered):
    for needle in ["http://fonts", "https://fonts", "cdn.", "googleapis", "<link "]:
        assert needle not in rendered


def test_repeated_warnings_collapse_into_one_summary_row(scraped, signals):
    """GRE-3464: five near-identical per-symbol failures (same source, same
    error) must render as ONE compact amber row with a dedup'd headline —
    not five stacked raw stack traces — with one compact symbol+URL line per
    warning collapsed inside <details> (full traces stay in the run
    artifact, never the brief)."""
    symbols = ["AAPL", "NVDA", "MSFT", "TSLA", "AMZN"]
    repeated = replace(
        scraped,
        universe=symbols,
        warnings=[
            f"nasdaq_earnings failed for {sym}: Page.goto: net::ERR_HTTP2_PROTOCOL_ERROR "
            f"at https://www.nasdaq.com/market-activity/stocks/{sym.lower()}/earnings\n"
            'Call log:\n  - navigating to "...", waiting until "load"\n'
            for sym in symbols
        ],
    )
    out = render_brief(repeated, signals)
    signals_section = out.split('id="signals"')[1].split("</section>")[0]

    assert signals_section.count('class="warn-row"') == 1
    assert "blocked for all 5 symbols" in signals_section
    assert "net::ERR_HTTP2_PROTOCOL_ERROR" in signals_section
    assert "<details>" in signals_section
    assert "<summary>" in signals_section
    # one compact symbol+URL line per warning inside the disclosure; the
    # error signature appears once (headline), never re-dumped per symbol
    assert signals_section.count("net::ERR_HTTP2_PROTOCOL_ERROR") == 1
    for sym in symbols:
        assert f"{sym} — https://www.nasdaq.com/market-activity/stocks/{sym.lower()}/earnings" in signals_section
    assert "Call log" not in signals_section


def test_connect_failures_collapse_into_one_infra_group(scraped, signals):
    """GRE-3464: a Solari browser-gateway outage (connect-level / session-
    launch failure, before any page is even requested) must never render as
    a per-source "X blocked for Y" line — it's a platform failure, not a
    source failure — and must collapse into ONE group even though every
    real instance carries a unique per-session WebSocket URL that would
    otherwise defeat naive de-duplication (each of these 3 warnings has a
    distinct session id)."""
    warnings = [
        "yahoo_news failed for AAPL: BrowserType.connect: WebSocket error: "
        "wss://api.getsolari.com/ws/ip-10-0-11-41:aaaa1111-a10a-419c-9013-000000000001:sess:1.AAA",
        "yahoo_quote failed for MSFT: BrowserType.connect: WebSocket error: "
        "wss://api.getsolari.com/ws/ip-10-0-11-41:bbbb2222-a10a-419c-9013-000000000002:sess:2.BBB",
        "yahoo_earnings_calendar failed for NVDA: BrowserType.connect: WebSocket error: "
        "wss://api.getsolari.com/ws/ip-10-0-11-41:cccc3333-a10a-419c-9013-000000000003:sess:3.CCC",
    ]
    hostile = replace(scraped, warnings=warnings)
    out = render_brief(hostile, signals)
    signals_section = out.split('id="signals"')[1].split("</section>")[0]

    assert signals_section.count('class="warn-row"') == 1
    assert "Solari browser sessions unavailable for 3 fetches" in signals_section
    assert "gateway error" in signals_section
    assert "blocked for" not in signals_section  # never blames a data source
    assert "BrowserType.connect" not in signals_section  # raw text stays out of the brief
    assert "ip-10-0-11-41" not in signals_section  # internal-hostname hygiene
    # fixture already has quotes/earnings/headlines covering all 3 affected
    # symbols via other sources -> full recovery
    assert "recovered via HTTP fallbacks where available" in signals_section


def test_connect_failure_singular_fetch_grammar(scraped, signals):
    warnings = [
        "yahoo_news failed for AAPL: BrowserType.connect: WebSocket error: "
        "wss://api.getsolari.com/ws/ip-10-0-11-41:x:1.AAA",
    ]
    hostile = replace(scraped, warnings=warnings)
    out = render_brief(hostile, signals)
    signals_section = out.split('id="signals"')[1].split("</section>")[0]
    assert "Solari browser sessions unavailable for 1 fetch (" in signals_section
    assert "1 fetches" not in signals_section


def test_precondition_required_signature_detected_as_infra(scraped, signals):
    """A 428 version-skew failure that never reaches the
    `BrowserType.connect` wording (e.g. raised earlier, at session-create
    time) must still classify as infra via the 'Precondition Required'
    signature."""
    warnings = [
        "yahoo_quote failed for AAPL: SolariError: 428 Precondition Required "
        "(server version: v1.62, client version: v1.59)",
    ]
    hostile = replace(scraped, warnings=warnings)
    out = render_brief(hostile, signals)
    signals_section = out.split('id="signals"')[1].split("</section>")[0]
    assert "Solari browser sessions unavailable for 1 fetch" in signals_section


def test_connect_failure_outcome_partial_recovery(scraped, signals):
    """MSFT has no headline in the fixture (only AAPL/NVDA do) -> its
    connect-failed headlines fetch never recovered; AAPL's did -> partial."""
    warnings = [
        "yahoo_news failed for MSFT: BrowserType.connect: WebSocket error: "
        "wss://api.getsolari.com/ws/x:1.AAA",
        "yahoo_news failed for AAPL: BrowserType.connect: WebSocket error: "
        "wss://api.getsolari.com/ws/x:2.BBB",
    ]
    hostile = replace(scraped, warnings=warnings)
    out = render_brief(hostile, signals)
    signals_section = out.split('id="signals"')[1].split("</section>")[0]
    assert "partially recovered via HTTP fallbacks" in signals_section


def test_connect_failure_outcome_no_recovery(scraped, signals):
    """MSFT has neither headlines nor earnings in the fixture -> both
    connect-failed fetches for it stayed uncovered -> no recovery."""
    warnings = [
        "yahoo_news failed for MSFT: BrowserType.connect: WebSocket error: "
        "wss://api.getsolari.com/ws/x:1.AAA",
        "yahoo_earnings_calendar failed for MSFT: BrowserType.connect: WebSocket error: "
        "wss://api.getsolari.com/ws/x:2.BBB",
    ]
    hostile = replace(scraped, warnings=warnings)
    out = render_brief(hostile, signals)
    signals_section = out.split('id="signals"')[1].split("</section>")[0]
    assert "no data recovered via HTTP fallbacks" in signals_section


def test_mixed_infra_and_genuine_source_failures_render_separate_groups(scraped, signals):
    """A run that hits both a gateway outage AND a real per-source failure
    must render two distinct groups — the infra collapse must not swallow
    a genuine source failure, and vice versa."""
    warnings = [
        "yahoo_news failed for AAPL: BrowserType.connect: WebSocket error: "
        "wss://api.getsolari.com/ws/x:1.AAA",
        "yahoo_news failed for MSFT: BrowserType.connect: WebSocket error: "
        "wss://api.getsolari.com/ws/x:2.BBB",
        "nasdaq_earnings failed for TSLA: Page.goto: net::ERR_HTTP2_PROTOCOL_ERROR "
        "at https://www.nasdaq.com/market-activity/stocks/tsla/earnings\n"
        'Call log:\n  - navigating to "...", waiting until "load"\n',
    ]
    hostile = replace(scraped, warnings=warnings)
    out = render_brief(hostile, signals)
    signals_section = out.split('id="signals"')[1].split("</section>")[0]

    assert signals_section.count('class="warn-row"') == 2
    assert "Solari browser sessions unavailable for 2 fetches" in signals_section
    assert "nasdaq_earnings blocked for TSLA" in signals_section
    assert "net::ERR_HTTP2_PROTOCOL_ERROR" in signals_section


def test_genuine_source_failures_of_various_kinds_stay_ungrouped_with_infra(scraped, signals):
    """HTTP2 block, 404, timeout, and empty-feed failures are real source
    failures (not gateway/connect failures) — each keeps its own per-source
    group, never swept into the infra bucket."""
    warnings = [
        "nasdaq_earnings failed for AAPL: Page.goto: net::ERR_HTTP2_PROTOCOL_ERROR "
        "at https://www.nasdaq.com/market-activity/stocks/aapl/earnings\nCall log:\n  - x\n",
        "yahoo_chart_quote failed for MSFT: Client error '404 Not Found' for url "
        "'https://query1.finance.yahoo.com/v8/finance/chart/MSFT?range=5d&interval=1d'",
        "stockanalysis_earnings failed for NVDA: Page.goto: Timeout 30000ms exceeded.\n"
        'Call log:\n  - navigating to "https://stockanalysis.com/stocks/nvda/", waiting until "load"\n',
        "google_news_rss failed for TSLA: no headlines parsed from google news rss for TSLA",
    ]
    hostile = replace(scraped, warnings=warnings)
    out = render_brief(hostile, signals)
    signals_section = out.split('id="signals"')[1].split("</section>")[0]

    assert signals_section.count('class="warn-row"') == 4
    assert "Solari browser sessions unavailable" not in signals_section


def test_zscore_bar_never_uses_direction_color_only_amber_or_grey(rendered):
    """GRE-3464: z-score is a magnitude reading, not a buy/sell call —
    green/red would read as directional advice."""
    signals_section = rendered.split('id="signals"')[1].split("</section>")[0]
    zbar_svgs = re.findall(r'<svg class="bar zbar".*?</svg>', signals_section, re.DOTALL)
    assert len(zbar_svgs) == 3  # AAPL, NVDA, TSLA are signal-covered; MSFT isn't
    from desk.brief import _ZBAR_MUTED, _ZBAR_STRETCHED

    for svg in zbar_svgs:
        assert "#3fb950" not in svg  # green
        assert "#f85149" not in svg  # red
    # fixture: NVDA |z|=1.92 >= 1.5 stretch threshold -> stretched fill;
    # TSLA |z|=1.15 -> muted. Assert against the constants, not literals, so a
    # palette change restyles the bar without falsely failing this test — what
    # it guards is the *rule* (magnitude, never direction), not the hex.
    nvda_row = signals_section.split('<strong>NVDA')[1].split("</tr>")[0]
    tsla_row = signals_section.split('<strong>TSLA')[1].split("</tr>")[0]
    assert _ZBAR_STRETCHED in nvda_row
    assert _ZBAR_MUTED in tsla_row


def test_vol_cell_shows_1d_and_annualized_labels(rendered):
    signals_section = rendered.split('id="signals"')[1].split("</section>")[0]
    # fixture: AAPL garch_vol_forecast_1d=0.0118, garch_vol_forecast_ann=0.187
    # header carries the 1d/ann labels; cells show bare order-matched values
    assert "1.18% &middot; 18.7%" in signals_section
    assert "1d 1.18%" not in signals_section


def test_verdict_badge_is_not_forced_uppercase(rendered):
    """GRE-3464: only section headers and the masthead stay all-caps —
    table data cells (the verdict badge included) render as-is."""
    badge_css = rendered.split(".badge {")[1].split("}")[0]
    assert "text-transform" not in badge_css


def test_cli_module_entrypoint_produces_valid_html(tmp_path, fixtures_dir):
    out_file = tmp_path / "brief.html"
    exit_code = main(
        [
            "--scraped",
            str(fixtures_dir / "scraped_data.json"),
            "--signals",
            str(fixtures_dir / "signals.json"),
            "--out",
            str(out_file),
        ]
    )
    assert exit_code == 0
    assert out_file.exists()
    text = out_file.read_text(encoding="utf-8")
    assert text.strip().startswith("<!doctype html>")
    for section_id in ["header", "signals", "earnings", "headlines", "provenance"]:
        assert f'id="{section_id}"' in text


def test_cli_literal_ac1_command(tmp_path):
    """Runs the exact command from the ticket's AC-1, from the package root
    (so the relative `fixtures/...` paths resolve), and checks the process
    behaves like AC-1 describes."""
    out_file = tmp_path / "brief.html"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "desk.brief",
            "--scraped",
            "fixtures/scraped_data.json",
            "--signals",
            "fixtures/signals.json",
            "--out",
            str(out_file),
        ],
        cwd=PACKAGE_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert out_file.exists()
    assert "wrote" in result.stdout
    text = out_file.read_text(encoding="utf-8")
    assert text.strip().startswith("<!doctype html>")


def test_render_is_pure_no_solari_import(rendered):
    """NG-3 guard: desk.brief must not import the Solari client."""
    import desk.brief as brief_module

    assert "solari_client" not in brief_module.__dict__
    source = Path(brief_module.__file__).read_text()
    assert "solari_client" not in source
    assert "import solari_browser" not in source
    assert "import solari_sandbox" not in source


# ---------------------------------------------------------------------------
# GRE-3464 UX pass — 1. TL;DR strip
# ---------------------------------------------------------------------------
# The bundled fixture (fixtures/scraped_data.json + fixtures/signals.json):
# AAPL neutral z=+0.42, NVDA bullish z=+1.92, TSLA avoid z=-1.15, MSFT
# uncovered; earnings NVDA 2026-09-02 (2d out), TSLA 2026-09-04 (4d out) from
# as_of 2026-08-31; one warning ("TSLA quote is 18 minutes stale...").


def _tldr_html(rendered: str) -> str:
    return rendered.split('id="tldr"')[1].split("</header>")[0]


def test_tldr_strip_lives_directly_under_header(rendered):
    header_idx = rendered.index('id="header"')
    tldr_idx = rendered.index('id="tldr"')
    signals_idx = rendered.index('id="signals"')
    assert header_idx < tldr_idx < signals_idx


def test_tldr_verdict_tally_computed_from_context_not_hardcoded(rendered):
    # 1 avoid (TSLA) + 1 bullish (NVDA) + 1 neutral (AAPL); MSFT is uncovered
    # and must not be counted.
    tldr = _tldr_html(rendered)
    assert "1 avoid" in tldr
    assert "1 bullish" in tldr
    assert "1 neutral" in tldr
    assert "0 " not in tldr  # zero-count verdicts must be omitted, not shown as "0 bearish"


def test_tldr_most_stretched_name_chip(rendered):
    # NVDA |1.92| is the largest |OU z-score| in the fixture -> rank 1.
    tldr = _tldr_html(rendered)
    assert "NVDA +1.9σ above its 1-yr mean" in tldr


def test_tldr_most_stretched_name_chip_uses_below_for_negative_zscore(scraped, signals):
    # Make TSLA (z=-1.15) the most-stretched row by shrinking the others.
    hostile_signals = copy.deepcopy(signals)
    hostile_signals.per_symbol["AAPL"].ou_zscore = 0.01
    hostile_signals.per_symbol["NVDA"].ou_zscore = 0.02
    out = render_brief(scraped, hostile_signals)
    tldr = _tldr_html(out)
    assert "TSLA -1.1σ below its 1-yr mean" in tldr


def test_tldr_next_earnings_chip_computed_from_as_of(rendered):
    # NVDA reports 2026-09-02, 2 days after as_of 2026-08-31 -> soonest.
    tldr = _tldr_html(rendered)
    assert "next earnings: NVDA in 2d" in tldr


def test_tldr_next_earnings_chip_omitted_when_none_in_window(scraped, signals):
    hostile = replace(scraped, earnings=[])
    out = render_brief(hostile, signals)
    tldr = _tldr_html(out)
    assert "next earnings" not in tldr


def test_tldr_data_health_chip_shows_warning_count_and_links_signals(rendered):
    # fixture has exactly one (undedupable, no source-id match) warning.
    tldr = _tldr_html(rendered)
    assert "1 data warning" in tldr
    assert 'href="#signals"' in tldr
    assert "all sources ok" not in tldr


def test_tldr_data_health_chip_shows_all_ok_when_no_warnings(scraped, signals):
    clean = replace(scraped, warnings=[])
    out = render_brief(clean, signals)
    tldr = _tldr_html(out)
    assert "all sources ok" in tldr
    assert "data warning" not in tldr


def test_tldr_data_health_chip_pluralizes_multiple_warnings(scraped, signals):
    hostile = replace(
        scraped,
        warnings=[
            "yahoo_news failed for AAPL: net::ERR_TIMED_OUT",
            "stockanalysis_earnings failed for NVDA: Timeout 30000ms exceeded.",
        ],
    )
    out = render_brief(hostile, signals)
    tldr = _tldr_html(out)
    assert "2 data warnings" in tldr


def test_tldr_verdict_tally_empty_when_no_signal_coverage(scraped):
    empty_signals = Signals(as_of="2026-08-31T06:15:00Z", per_symbol={})
    out = render_brief(scraped, empty_signals)
    tldr = _tldr_html(out)
    assert "no signal coverage" in tldr


# ---------------------------------------------------------------------------
# GRE-3464 UX pass — 2. plain-English interpretation sentence
# ---------------------------------------------------------------------------


def _sig(**overrides) -> SymbolSignal:
    base = dict(
        garch_vol_forecast_1d=0.01,
        garch_vol_forecast_ann=0.15,
        ou_zscore=0.1,
        ou_half_life_d=5.0,
        momentum_5d=0.0,
        verdict="neutral",
        notes=[],
        label=None,
    )
    base.update(overrides)
    return SymbolSignal(**base)


def test_interpret_mean_reversion_watch_at_threshold_boundary():
    # Exactly at the rule table's >= boundaries (vol 35%, |z| 1.5).
    sig = _sig(garch_vol_forecast_ann=0.35, ou_zscore=1.5, verdict="avoid", label="mean-reversion-watch")
    assert _interpret_signal(sig, None, None) == "1.5σ above 1-yr mean, high vol — reversion risk"


def test_interpret_mean_reversion_watch_negative_zscore_says_below():
    sig = _sig(garch_vol_forecast_ann=0.40, ou_zscore=-2.0, verdict="avoid", label="mean-reversion-watch")
    assert _interpret_signal(sig, None, None) == "2.0σ below 1-yr mean, high vol — reversion risk"


def test_interpret_just_under_vol_threshold_does_not_flag_reversion():
    # vol 34.9% < 35% -> rule 3 must NOT fire even with a stretched z-score.
    sig = _sig(garch_vol_forecast_ann=0.349, ou_zscore=5.0, verdict="avoid")
    assert "reversion risk" not in _interpret_signal(sig, None, None)


def test_interpret_trend_watch_up_at_momentum_boundary():
    sig = _sig(garch_vol_forecast_ann=0.19, momentum_5d=0.02, verdict="bullish", label="trend-watch")
    assert _interpret_signal(sig, None, None) == "low vol, drifting up — steady trend"


def test_interpret_trend_watch_down_at_momentum_boundary():
    sig = _sig(garch_vol_forecast_ann=0.19, momentum_5d=-0.02, verdict="bearish", label="trend-watch")
    assert _interpret_signal(sig, None, None) == "low vol, drifting down — steady trend"


def test_interpret_vol_exactly_at_low_boundary_does_not_flag_trend():
    # vol == 20% is not "< 20%" -> rule 4/5 must NOT fire.
    sig = _sig(garch_vol_forecast_ann=0.20, momentum_5d=0.05, verdict="neutral")
    out = _interpret_signal(sig, None, None)
    assert "steady trend" not in out


def test_interpret_no_strong_signal_fallback():
    sig = _sig(garch_vol_forecast_ann=0.25, ou_zscore=0.3, momentum_5d=0.0, verdict="neutral", label="no-strong-signal")
    assert _interpret_signal(sig, None, None) == "no strong signal — inside normal ranges"


def test_interpret_insufficient_data_label_overrides_numbers():
    # Even with numbers that would otherwise read as stretched, the
    # insufficient-data label must win (rule 1 in the table).
    sig = _sig(garch_vol_forecast_ann=0.9, ou_zscore=4.0, verdict="avoid", label="insufficient-data")
    assert _interpret_signal(sig, None, None) == "limited price history — low-confidence read"


def test_interpret_earnings_within_window_takes_priority_over_reversion():
    # Earnings due soon AND vol/z both past the reversion threshold —
    # earnings (rule 2) must win, matching decide_verdict's rule order.
    sig = _sig(garch_vol_forecast_ann=0.40, ou_zscore=2.0, verdict="avoid", label="event-risk")
    row = Earnings(symbol="NVDA", date="2026-09-02", session="amc")
    out = _interpret_signal(sig, row, date(2026, 8, 31))
    assert out == "reports in 2d — expect a gap"


def test_interpret_earnings_today_boundary():
    sig = _sig(verdict="avoid", label="event-risk")
    row = Earnings(symbol="TSLA", date="2026-08-31", session="bmo")
    out = _interpret_signal(sig, row, date(2026, 8, 31))
    assert out == "reports today — expect a gap"


def test_interpret_avoid_without_label_or_matching_rule_has_generic_fallback():
    # Older-shape signal (no `label`) whose numbers don't match any
    # threshold rule — must not fabricate a specific reason.
    sig = _sig(garch_vol_forecast_ann=0.25, ou_zscore=0.5, momentum_5d=0.0, verdict="avoid", label=None)
    assert _interpret_signal(sig, None, None) == "flagged avoid — see notes for detail"


def test_interpret_sentence_is_at_most_ten_words():
    cases = [
        _sig(garch_vol_forecast_ann=0.40, ou_zscore=3.0, verdict="avoid", label="mean-reversion-watch"),
        _sig(garch_vol_forecast_ann=0.10, momentum_5d=0.05, verdict="bullish", label="trend-watch"),
        _sig(garch_vol_forecast_ann=0.10, momentum_5d=-0.05, verdict="bearish", label="trend-watch"),
        _sig(verdict="avoid", label="insufficient-data"),
        _sig(verdict="neutral", label="no-strong-signal"),
    ]
    for sig in cases:
        words = _interpret_signal(sig, None, None).split()
        assert len(words) <= 10, _interpret_signal(sig, None, None)


def test_interpret_sentence_never_uses_buy_sell_language():
    banned = {"buy", "sell", "short", "long", "purchase", "trade"}
    cases = [
        _sig(garch_vol_forecast_ann=0.40, ou_zscore=3.0, verdict="avoid", label="mean-reversion-watch"),
        _sig(garch_vol_forecast_ann=0.10, momentum_5d=0.05, verdict="bullish", label="trend-watch"),
        _sig(verdict="avoid", label="insufficient-data"),
        _sig(verdict="neutral"),
    ]
    for sig in cases:
        words = {w.strip("—.,").lower() for w in _interpret_signal(sig, None, None).split()}
        assert not (words & banned)


def test_rendered_verdict_cell_includes_interpretation_sentence(scraped, signals):
    # Strip NVDA's earnings row so the rule-order test below isolates the
    # mean-reversion-watch branch rather than the higher-priority
    # earnings-window branch (NVDA's fixture earnings date is 2 days out).
    hostile = replace(scraped, earnings=[e for e in scraped.earnings if e.symbol != "NVDA"])
    out = render_brief(hostile, signals)
    signals_section = out.split('id="signals"')[1].split("</section>")[0]
    assert 'class="verdict-interp"' in signals_section
    # NVDA: vol 41.4% (>=35%) and z=1.92 (>=1.5) -> mean-reversion-watch read
    nvda_row = signals_section.split('<strong>NVDA')[1].split("</tr>")[0]
    assert "1.9σ above 1-yr mean, high vol — reversion risk" in nvda_row


def test_rendered_verdict_cell_interpretation_respects_earnings_priority(rendered):
    # Unmodified fixture: NVDA has both a stretched z-score AND an earnings
    # date 2 days out — the rule table's earnings-window rule outranks
    # mean-reversion-watch, so the rendered sentence must reflect that.
    signals_section = rendered.split('id="signals"')[1].split("</section>")[0]
    nvda_row = signals_section.split('<strong>NVDA')[1].split("</tr>")[0]
    assert "reports in 2d — expect a gap" in nvda_row


# ---------------------------------------------------------------------------
# GRE-3464 UX pass — 3. column subtitles
# ---------------------------------------------------------------------------


def test_column_subtitles_present(rendered):
    signals_section = rendered.split('id="signals"')[1].split("</section>")[0]
    assert '<span class="col-sub">expected move</span>' in signals_section
    assert (
        '<span class="col-sub">0 = 1-yr mean &middot; dashed ticks at &plusmn;1.5</span>'
        in signals_section
    )
    assert '<span class="col-sub">days for stretch to halve</span>' in signals_section
    assert '<span class="col-sub">5-day price change</span>' in signals_section
    # subtitles live inside <thead>, which the mobile media query hides
    # entirely alongside the rest of the header row.
    assert "table.responsive thead { display: none; }" in rendered


# ---------------------------------------------------------------------------
# GRE-3464 UX pass — 4. "How to read this" disclosure
# ---------------------------------------------------------------------------


def test_how_to_read_disclosure_present_and_collapsed_after_table(rendered):
    signals_section = rendered.split('id="signals"')[1].split("</section>")[0]
    assert '<details class="how-to-read" id="how-to-read">' in signals_section
    assert "<summary>How to read this</summary>" in signals_section
    # comes after the ranked-signal table, not before
    assert signals_section.index("</table>") < signals_section.index('id="how-to-read"')
    # condensed rule-table thresholds, same numbers as the README/rule table
    assert "3 calendar days" in signals_section
    assert "1.5" in signals_section and "35%" in signals_section
    assert "20%" in signals_section and "2%" in signals_section
    # ends on the research-only boundary
    assert "Research only" in signals_section and "not investment advice" in signals_section
    # a <details> is collapsed by default absent an `open` attribute
    assert "<details class=\"how-to-read\" id=\"how-to-read\" open" not in signals_section


# ---------------------------------------------------------------------------
# GRE-3464 UX pass — 5. headlines triage (cap, newest-first, dedupe)
# ---------------------------------------------------------------------------


def _headline(symbol, title, published, source="Reuters", url="https://example.com/x"):
    return Headline(symbol=symbol, title=title, source=source, url=url, published=published)


def test_headline_cap_is_five_per_symbol():
    items = [_headline("AAPL", f"Story {i}", f"2026-08-{20+i:02d}T00:00:00Z") for i in range(8)]
    shown, overflow = _newest_first_capped(items)
    assert len(shown) == HEADLINE_CAP == 5
    assert len(overflow) == 3
    # overflow keeps newest-first ordering and carries the actual headlines
    assert [h.title for h in overflow] == ["Story 2", "Story 1", "Story 0"]


def test_headline_newest_first_ordering():
    items = [
        _headline("AAPL", "Oldest", "2026-08-20T00:00:00Z"),
        _headline("AAPL", "Newest", "2026-08-30T00:00:00Z"),
        _headline("AAPL", "Middle", "2026-08-25T00:00:00Z"),
    ]
    shown, overflow = _newest_first_capped(items)
    assert [h.title for h in shown] == ["Newest", "Middle", "Oldest"]
    assert overflow == []


def test_headline_unparseable_published_sorts_last_not_crashes():
    items = [
        _headline("AAPL", "Good date", "2026-08-30T00:00:00Z"),
        _headline("AAPL", "Bad date", "not-a-date"),
    ]
    shown, overflow = _newest_first_capped(items)
    assert [h.title for h in shown] == ["Good date", "Bad date"]


def test_headline_dedupe_keeps_shared_title_under_first_symbol_only():
    headlines = [
        _headline("AAPL", "Shared story", "2026-08-30T00:00:00Z"),
        _headline("MSFT", "Shared story", "2026-08-30T00:00:00Z"),
    ]
    by_symbol, promoted = _dedupe_and_group_headlines(headlines, ["AAPL", "MSFT"])
    assert [h.title for h in by_symbol["AAPL"]] == ["Shared story"]
    assert by_symbol["MSFT"] == []
    assert promoted == []


def test_headline_dedupe_promotes_to_market_wide_when_three_or_more_symbols_share_title():
    headlines = [
        _headline("AAPL", "Fed signals rate pause", "2026-08-30T00:00:00Z"),
        _headline("MSFT", "Fed signals rate pause", "2026-08-30T00:00:00Z"),
        _headline("NVDA", "Fed signals rate pause", "2026-08-30T00:00:00Z"),
        _headline("AAPL", "Apple-only story", "2026-08-29T00:00:00Z"),
    ]
    by_symbol, promoted = _dedupe_and_group_headlines(headlines, ["AAPL", "MSFT", "NVDA"])
    assert [h.title for h in promoted] == ["Fed signals rate pause"]
    assert len(promoted) == 1  # promoted exactly once, not once per symbol
    assert [h.title for h in by_symbol["AAPL"]] == ["Apple-only story"]
    assert by_symbol["MSFT"] == []
    assert by_symbol["NVDA"] == []


def test_headline_dedupe_two_symbols_not_promoted():
    # Below the 3-symbol promotion threshold — stays under the first symbol.
    headlines = [
        _headline("AAPL", "Two-symbol story", "2026-08-30T00:00:00Z"),
        _headline("MSFT", "Two-symbol story", "2026-08-30T00:00:00Z"),
    ]
    by_symbol, promoted = _dedupe_and_group_headlines(headlines, ["AAPL", "MSFT"])
    assert promoted == []
    assert [h.title for h in by_symbol["AAPL"]] == ["Two-symbol story"]


def test_rendered_headlines_show_more_note_when_capped(scraped, signals):
    many = [
        _headline("AAPL", f"AAPL story {i}", f"2026-08-{10+i:02d}T00:00:00Z")
        for i in range(7)
    ]
    hostile = replace(scraped, headlines=many)
    out = render_brief(hostile, signals)
    headlines_section = out.split('id="headlines"')[1].split("</section>")[0]
    groups = headlines_section.split('<div class="headline-group">')
    aapl_group = next(g for g in groups if "<h3>AAPL</h3>" in g)
    # 5 visible + 2 inside the collapsed dropdown = 7 <li> total
    assert aapl_group.count("<li>") == 7
    assert '<details class="headline-more">' in aapl_group
    assert "2 more headlines" in aapl_group
    # the overflow stories are rendered in the dropdown, not pointed at a file
    assert "scraped_data.json" not in aapl_group


def test_rendered_headlines_no_more_dropdown_when_under_cap(rendered):
    # bundled fixture has exactly one headline per covered symbol.
    headlines_section = rendered.split('id="headlines"')[1].split("</section>")[0]
    assert "headline-more" not in headlines_section


# ---------------------------------------------------------------------------
# GRE-3464 UX pass — 6. earnings cards: relative time
# ---------------------------------------------------------------------------


def test_earnings_within_matches_soonest_row_in_window():
    earnings = [
        Earnings(symbol="NVDA", date="2026-09-03", session="amc"),
        Earnings(symbol="NVDA", date="2026-09-01", session="bmo"),  # soonest -> wins
    ]
    row = _earnings_within("NVDA", earnings, date(2026, 8, 31))
    assert row.date == "2026-09-01"


def test_earnings_within_none_outside_window():
    earnings = [Earnings(symbol="NVDA", date="2026-09-10", session="amc")]  # 10d out, window is 3d
    assert _earnings_within("NVDA", earnings, date(2026, 8, 31)) is None


def test_earnings_within_none_when_as_of_unparseable():
    earnings = [Earnings(symbol="NVDA", date="2026-09-01", session="amc")]
    assert _earnings_within("NVDA", earnings, None) is None


def _earnings_cards(rendered_or_out: str) -> list[str]:
    earnings_section = rendered_or_out.split('id="earnings"')[1].split("</section>")[0]
    return earnings_section.split('<div class="callout">')[1:]


def test_rendered_earnings_cards_lead_with_relative_time(rendered):
    nvda_card = next(c for c in _earnings_cards(rendered) if '<div class="sym">NVDA</div>' in c)
    assert '<div class="rel-time">in 2d</div>' in nvda_card
    # absolute date + session survive as the secondary line
    assert "2026-09-02" in nvda_card
    assert "After close" in nvda_card  # NVDA earnings session is amc


def test_rendered_earnings_cards_relative_time_today_boundary(scraped, signals):
    hostile = replace(scraped, earnings=[Earnings(symbol="TSLA", date="2026-08-31", session="bmo")])
    out = render_brief(hostile, signals)
    earnings_section = out.split('id="earnings"')[1].split("</section>")[0]
    assert '<div class="rel-time">today</div>' in earnings_section


def test_rendered_earnings_cards_sorted_soonest_first(scraped, signals):
    hostile = replace(
        scraped,
        earnings=[
            Earnings(symbol="TSLA", date="2026-09-10", session="amc"),
            Earnings(symbol="NVDA", date="2026-09-02", session="amc"),
        ],
    )
    out = render_brief(hostile, signals)
    earnings_section = out.split('id="earnings"')[1].split("</section>")[0]
    nvda_pos = earnings_section.index('<div class="sym">NVDA</div>')
    tsla_pos = earnings_section.index('<div class="sym">TSLA</div>')
    assert nvda_pos < tsla_pos


# ---------------------------------------------------------------------------
# GRE-3464 UX pass — 7. one primary "run" timestamp
# ---------------------------------------------------------------------------


def test_header_shows_single_prominent_run_timestamp(rendered):
    header = rendered.split('id="header"')[1].split("</header>")[0]
    assert '<div class="run-time">Run <strong>2026-08-31 06:00 UTC</strong></div>' in header


def test_header_demotes_scraped_and_signals_as_of_to_small_muted_detail(rendered):
    header = rendered.split('id="header"')[1].split("</header>")[0]
    assert 'class="as-of-detail"' in header
    detail = header.split('class="as-of-detail">')[1].split("</div>")[0]
    # both values still present in the DOM, just demoted — same underlying
    # data the old single "As of ... signals as of ..." line carried.
    assert "2026-08-31 06:00 UTC" in detail  # scraped.as_of
    assert "2026-08-31 06:15 UTC" in detail  # signals.as_of


# --- z-score bar encoding -------------------------------------------------
# Regression guard for GRE-3464: _ZSCORE_SCALE was 3.0 while real readings ran
# +3.4 to +6.5, so every bar clipped to full width and four of five rendered
# pixel-identical. The column looked like an encoding and carried no signal.


def _zbar_geometry(z):
    """(x, width, fill) of the value rect in the rendered z-score bar."""
    from desk.brief import _zscore_bar_svg

    svg = _zscore_bar_svg(z)
    rect = re.search(r'<rect x="([\d.]+)" y="2" width="([\d.]+)"[^>]*fill="(#\w+)"', svg)
    return float(rect.group(1)), float(rect.group(2)), rect.group(3)


def test_zscore_bars_differentiate_across_observed_range():
    """The five symbols from the 2026-08-31 run must render five distinct widths."""
    observed = [6.48, 4.78, 4.72, 3.71, -3.45]
    widths = [round(_zbar_geometry(z)[1], 1) for z in observed]
    assert len(set(widths)) == len(observed), f"bars collapsed to {widths}"
    assert max(widths) < 50.0, "widths at the 50.0 cap mean the scale saturates again"


def test_negative_zscore_renders_left_of_the_zero_axis():
    """Direction is carried by position, not hue (the bar fill is a magnitude
    reading only). That makes the left/right placement load-bearing."""
    x_below, _, _ = _zbar_geometry(-3.45)
    x_above, _, _ = _zbar_geometry(4.72)
    assert x_below < 50.0 <= x_above


def test_zscore_bar_marks_the_stretch_threshold():
    from desk.brief import _ZBAR_MUTED, _zscore_bar_svg

    svg = _zscore_bar_svg(4.72)
    assert svg.count("stroke-dasharray") == 2, "expected a tick each side of zero"
    assert _zbar_geometry(0.8)[2] == _ZBAR_MUTED, "inside +/-1.5 must stay muted"


def test_half_life_note_is_relative_to_the_five_day_window():
    from desk.brief import _half_life_note

    assert "within" in _half_life_note(3.2)
    assert "beyond" in _half_life_note(14.3)
    assert "slow" in _half_life_note(46.8)
