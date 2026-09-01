"""Hermetic contract tests — no network. Fixtures round-trip through the
dataclasses + JSON Schema, and mutated/invalid samples are rejected."""

import json

import jsonschema
import pytest

from desk.contracts import (
    ScrapedData,
    Signals,
    load_scraped,
    load_signals,
    validate_scraped,
    validate_signals,
)

pytestmark = pytest.mark.filterwarnings("ignore")


def test_scraped_fixture_validates(fixtures_dir):
    data = json.loads((fixtures_dir / "scraped_data.json").read_text())
    validate_scraped(data)  # must not raise


def test_scraped_fixture_round_trips(fixtures_dir):
    parsed = load_scraped(fixtures_dir / "scraped_data.json")
    assert isinstance(parsed, ScrapedData)
    original = json.loads((fixtures_dir / "scraped_data.json").read_text())
    assert parsed.to_dict() == original


def test_scraped_headline_symbol_may_be_null(fixtures_dir):
    parsed = load_scraped(fixtures_dir / "scraped_data.json")
    macro_headlines = [h for h in parsed.headlines if h.symbol is None]
    assert macro_headlines, "fixture should include at least one market-wide headline"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("as_of"),
        lambda d: d["quotes"].__setitem__("AAPL", {"last": "not-a-number", "prev_close": 1}),
        lambda d: d["earnings"][0].__setitem__("session", "midnight"),
        lambda d: d.__setitem__("universe", "AAPL"),  # must be an array
    ],
)
def test_invalid_scraped_samples_rejected(fixtures_dir, mutate):
    data = json.loads((fixtures_dir / "scraped_data.json").read_text())
    mutate(data)
    with pytest.raises(jsonschema.ValidationError):
        validate_scraped(data)


def test_signals_fixture_validates(fixtures_dir):
    data = json.loads((fixtures_dir / "signals.json").read_text())
    validate_signals(data)  # must not raise


def test_signals_fixture_round_trips(fixtures_dir):
    parsed = load_signals(fixtures_dir / "signals.json")
    assert isinstance(parsed, Signals)
    original = json.loads((fixtures_dir / "signals.json").read_text())
    assert parsed.to_dict() == original


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["per_symbol"]["AAPL"].pop("verdict"),
        lambda d: d["per_symbol"]["AAPL"].__setitem__("verdict", "moon"),  # not in enum
        lambda d: d["per_symbol"]["AAPL"].__setitem__("ou_half_life_d", -1),  # must be >= 0
        lambda d: d.pop("as_of"),
    ],
)
def test_invalid_signals_samples_rejected(fixtures_dir, mutate):
    data = json.loads((fixtures_dir / "signals.json").read_text())
    mutate(data)
    with pytest.raises(jsonschema.ValidationError):
        validate_signals(data)


def test_scraped_and_signals_fixtures_share_universe(fixtures_dir):
    scraped = json.loads((fixtures_dir / "scraped_data.json").read_text())
    signals = json.loads((fixtures_dir / "signals.json").read_text())
    # Not a schema requirement (signals may cover a subset), but the fixture
    # pair should be internally consistent for anyone using them together.
    assert set(signals["per_symbol"]) <= set(scraped["universe"])
