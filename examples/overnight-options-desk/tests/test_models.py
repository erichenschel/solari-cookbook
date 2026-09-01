"""Hermetic tests for the quant models lane (GRE-3461).

Model functions (`desk.model_code.prices`, `desk.model_code.signals`) are
imported LOCALLY — same files that get uploaded flat into the sandbox — and
run against bundled price-history fixtures (`fixtures/prices/*.csv`), seeded
synthetic data so the numeric outputs are reproducible. No network, no
Solari key required.
"""

from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone

import pytest

import desk.models as models
from desk.contracts import validate_signals
from desk.model_code import signals
from desk.model_code.prices import closes_from_csv_file, parse_stooq_csv
from desk.models import (
    RESULT_END_MARKER,
    RESULT_START_MARKER,
    MODEL_CODE_FILES,
    _bootstrap_code,
    _extract_signals_json,
    _load_model_code_files,
    run_models,
)
from desk.solari_client import ExecResult

AS_OF = "2026-03-01T06:00:00Z"


def test_parse_stooq_csv_basic():
    text = "Date,Open,High,Low,Close,Volume\n2026-01-02,10,11,9,10.5,1000\n2026-01-03,10.5,11,10,10.8,1200\n"
    closes = parse_stooq_csv(text)
    assert closes == [10.5, 10.8]


def test_parse_stooq_csv_skips_unparseable_rows():
    text = "Date,Open,High,Low,Close,Volume\n2026-01-02,10,11,9,N/D,1000\n2026-01-03,10.5,11,10,10.8,1200\n"
    closes = parse_stooq_csv(text)
    assert closes == [10.8]


@pytest.mark.parametrize(
    "name,expected_len",
    [("UPTREND", 300), ("DOWNTREND", 300), ("MEANREV", 300), ("NEUTRAL", 300), ("SHORT", 40), ("CONST", 80)],
)
def test_price_fixtures_parse_to_expected_length(fixtures_dir, name, expected_len):
    closes = closes_from_csv_file(str(fixtures_dir / "prices" / f"{name}.csv"))
    assert len(closes) == expected_len
    assert all(c > 0 for c in closes)


def _signal_for(fixtures_dir, name, earnings=None):
    closes = closes_from_csv_file(str(fixtures_dir / "prices" / f"{name}.csv"))
    return signals.compute_symbol_signal(name, closes, earnings or [], AS_OF)


def test_uptrend_fixture_yields_bullish_trend_watch(fixtures_dir):
    sig = _signal_for(fixtures_dir, "UPTREND")
    assert sig["verdict"] == "bullish"
    assert sig["garch_vol_forecast_ann"] < signals.VOL_LOW_ANN
    assert sig["momentum_5d"] == pytest.approx(0.035, abs=0.01)
    assert any("trend-watch" in n for n in sig["notes"])


def test_downtrend_fixture_yields_bearish_trend_watch(fixtures_dir):
    sig = _signal_for(fixtures_dir, "DOWNTREND")
    assert sig["verdict"] == "bearish"
    assert sig["garch_vol_forecast_ann"] < signals.VOL_LOW_ANN
    assert sig["momentum_5d"] == pytest.approx(-0.035, abs=0.01)
    assert any("trend-watch" in n for n in sig["notes"])


def test_meanrev_fixture_yields_avoid_mean_reversion_watch(fixtures_dir):
    sig = _signal_for(fixtures_dir, "MEANREV")
    assert sig["verdict"] == "avoid"
    assert sig["garch_vol_forecast_ann"] >= signals.VOL_HIGH_ANN
    assert abs(sig["ou_zscore"]) >= signals.Z_STRETCH
    assert any("mean-reversion-watch" in n for n in sig["notes"])


def test_neutral_fixture_yields_neutral(fixtures_dir):
    sig = _signal_for(fixtures_dir, "NEUTRAL")
    assert sig["verdict"] == "neutral"
    assert any("no-strong-signal" in n for n in sig["notes"])


def test_short_history_fixture_yields_insufficient_data_note(fixtures_dir):
    """AC-3: a symbol with <60 trading days gets an insufficient-data note
    and the run still succeeds. `verdict` itself is schema-constrained to
    `bullish|bearish|neutral|avoid` (see the CONTRACT GAP note in
    desk/model_code/signals.py) so the literal research label
    "insufficient-data" lives in notes[], not the verdict field."""
    sig = _signal_for(fixtures_dir, "SHORT")
    assert sig["verdict"] == "avoid"
    assert any(n.startswith("insufficient-data") for n in sig["notes"])
    # still schema-valid and structurally complete — the run did NOT crash
    validate_signals({"as_of": AS_OF, "per_symbol": {"SHORT": sig}})


def test_earnings_within_window_forces_avoid_event_risk(fixtures_dir):
    earnings = [{"symbol": "NEUTRAL", "date": "2026-03-03", "session": "bmo"}]
    sig = _signal_for(fixtures_dir, "NEUTRAL", earnings=earnings)
    assert sig["verdict"] == "avoid"
    assert any("event-risk" in n for n in sig["notes"])


def test_earnings_outside_window_does_not_force_avoid(fixtures_dir):
    earnings = [{"symbol": "NEUTRAL", "date": "2026-04-01", "session": "bmo"}]
    sig = _signal_for(fixtures_dir, "NEUTRAL", earnings=earnings)
    assert sig["verdict"] == "neutral"
    assert not any("event-risk" in n for n in sig["notes"])


def test_past_earnings_date_does_not_force_event_risk(fixtures_dir):
    """GRE-3464 consistency check (brief.py's earnings-window filter was
    the actual bug): `has_earnings_soon`'s `0 <= delta <= window_days`
    guard already excludes a date before `as_of` (AS_OF is 2026-03-01) —
    this locks that behavior in with a test rather than leaving it
    implicit. A report 2 days *before* as_of must not trigger event-risk,
    even though it's within 3 calendar days of it."""
    earnings = [{"symbol": "NEUTRAL", "date": "2026-02-27", "session": "amc"}]
    sig = _signal_for(fixtures_dir, "NEUTRAL", earnings=earnings)
    assert sig["verdict"] == "neutral"
    assert not any("event-risk" in n for n in sig["notes"])


def test_constant_price_series_does_not_crash(fixtures_dir):
    """A flat 80-day price series is a genuine pathological input: zero
    variance trips GARCH into non-convergence and the AR(1) OLS design
    matrix into perfect collinearity (statsmodels' add_constant column-skip
    drops a column, `model.params` has one entry instead of two). Both
    fallbacks must absorb this without raising."""
    sig = _signal_for(fixtures_dir, "CONST")
    assert sig["verdict"] in signals.VERDICT_VALUES
    assert sig["notes"], "expected fallback notes explaining the degraded fit"
    validate_signals({"as_of": AS_OF, "per_symbol": {"CONST": sig}})


@pytest.mark.parametrize("closes", [[], [123.4]])
def test_zero_or_one_price_points_does_not_crash(closes):
    sig = signals.compute_symbol_signal("X", closes, [], AS_OF)
    assert sig["verdict"] == "avoid"
    assert any(n.startswith("insufficient-data") for n in sig["notes"])
    validate_signals({"as_of": AS_OF, "per_symbol": {"X": sig}})


def test_all_fixture_verdicts_together_are_schema_valid(fixtures_dir):
    per_symbol = {
        name: _signal_for(fixtures_dir, name)
        for name in ("UPTREND", "DOWNTREND", "MEANREV", "NEUTRAL", "SHORT", "CONST")
    }
    payload = {"as_of": AS_OF, "per_symbol": per_symbol}
    validate_signals(payload)  # must not raise
    # every verdict is one of the four schema-enum values (rule table maps
    # the ticket's research vocabulary onto them — see notes[])
    for sig in per_symbol.values():
        assert sig["verdict"] in signals.VERDICT_VALUES


def test_load_model_code_files_returns_all_expected_files():
    files = _load_model_code_files()
    assert set(files) == {f"/home/user/desk_model_code/{n}" for n in MODEL_CODE_FILES}
    for content in files.values():
        assert content.strip()  # non-empty


def test_bootstrap_code_is_valid_python_and_embeds_scraped_data():
    scraped = {"as_of": AS_OF, "universe": ["AAPL"], "earnings": [], "headlines": [], "quotes": {}, "provenance": {"sessions": []}, "warnings": []}
    code = _bootstrap_code(scraped)
    ast.parse(code)  # raises SyntaxError if malformed
    assert RESULT_START_MARKER in code
    assert RESULT_END_MARKER in code
    assert '"AAPL"' in code


def test_extract_signals_json_pulls_payload_from_noisy_stdout():
    payload = {"as_of": AS_OF, "per_symbol": {}}
    stdout = (
        "Collecting numpy\n"
        "Successfully installed numpy-2.0\n"
        f"{RESULT_START_MARKER}\n"
        f"{json.dumps(payload)}\n"
        f"{RESULT_END_MARKER}\n"
    )
    assert _extract_signals_json(stdout) == payload


def test_extract_signals_json_raises_when_markers_missing():
    with pytest.raises(RuntimeError):
        _extract_signals_json("no markers here")


async def test_run_models_stamps_as_of_from_host_clock_not_sandbox(fixtures_dir, monkeypatch):
    """GRE-3464 regression: the sandbox VM's own clock can be stuck weeks in
    the past (model_code/fetch.py's TLS clock-skew note) — a real run once
    published a brief headed with a month-old `as_of` because driver.py
    stamped it using that stale in-sandbox clock. `run_models` must
    overwrite it with the host's own (trustworthy) clock before validating
    and returning."""
    scraped = json.loads((fixtures_dir / "scraped_data.json").read_text())
    stale_as_of = "2020-01-01T00:00:00Z"  # simulates the stuck sandbox clock
    sandbox_signals = {
        "as_of": stale_as_of,
        "per_symbol": {
            sym: {
                "garch_vol_forecast_1d": 0.01,
                "garch_vol_forecast_ann": 0.2,
                "ou_zscore": 0.0,
                "ou_half_life_d": 5.0,
                "momentum_5d": 0.0,
                "verdict": "neutral",
                "notes": [],
            }
            for sym in scraped["universe"]
        },
    }
    stdout = f"{RESULT_START_MARKER}\n{json.dumps(sandbox_signals)}\n{RESULT_END_MARKER}\n"

    async def _fake_run_in_sandbox(**kwargs):
        return ExecResult(stdout=stdout, stderr="", error=None, result=None)

    monkeypatch.setattr(models, "run_in_sandbox", _fake_run_in_sandbox)

    result = await run_models(str(fixtures_dir / "scraped_data.json"))

    assert result["as_of"] != stale_as_of
    stamped = datetime.fromisoformat(result["as_of"].replace("Z", "+00:00"))
    assert abs(datetime.now(timezone.utc) - stamped) < timedelta(minutes=1)
