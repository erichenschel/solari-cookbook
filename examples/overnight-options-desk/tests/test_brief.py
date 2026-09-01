"""Hermetic tests for desk/brief.py — no network, no Solari key needed.

Covers AC-1 (all five sections present, hostile-input escaping, whole file
end-to-end via the literal `python -m desk.brief` CLI) and AC-4 (disclaimer
+ provenance footer present)."""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from desk.brief import main, render_brief
from desk.contracts import ScrapedData, Signals

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
    assert "sess_7f3a9c21" in footer
    assert "sess_9b21e04d" in footer


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
