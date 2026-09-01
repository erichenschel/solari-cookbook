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
from pathlib import Path

import pytest

from desk.brief import _upcoming_earnings, main, render_brief
from desk.contracts import Earnings, ScrapedData, Signals

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


def test_zscore_bar_never_uses_direction_color_only_amber_or_grey(rendered):
    """GRE-3464: z-score is a magnitude reading, not a buy/sell call —
    green/red would read as directional advice."""
    signals_section = rendered.split('id="signals"')[1].split("</section>")[0]
    zbar_svgs = re.findall(r'<svg class="bar zbar".*?</svg>', signals_section, re.DOTALL)
    assert len(zbar_svgs) == 3  # AAPL, NVDA, TSLA are signal-covered; MSFT isn't
    for svg in zbar_svgs:
        assert "#3fb950" not in svg  # green
        assert "#f85149" not in svg  # red
    # fixture: NVDA |z|=1.92 >= 1.5 stretch threshold -> amber; TSLA |z|=1.15 -> muted grey
    nvda_row = signals_section.split('<strong>NVDA')[1].split("</tr>")[0]
    tsla_row = signals_section.split('<strong>TSLA')[1].split("</tr>")[0]
    assert "#d29922" in nvda_row
    assert "#6e7681" in tsla_row


def test_vol_cell_shows_1d_and_annualized_labels(rendered):
    signals_section = rendered.split('id="signals"')[1].split("</section>")[0]
    # fixture: AAPL garch_vol_forecast_1d=0.0118, garch_vol_forecast_ann=0.187
    assert "1d 1.18% &middot; ann 18.7%" in signals_section


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
