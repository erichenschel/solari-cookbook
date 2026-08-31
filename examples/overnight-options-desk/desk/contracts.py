"""contracts.py — the two data contracts the four desk lanes hand off between
each other: `scraped_data` (scraper lane -> everyone) and `signals` (quant
lane -> everyone). Dataclasses + JSON Schema, so a producer can validate its
own output and a consumer can validate what it received without importing
the producer's code.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import jsonschema

SCHEMA_DIR = Path(__file__).parent / "schemas"


@lru_cache(maxsize=None)
def _schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text())


# --------------------------------------------------------------------------
# scraped_data
# --------------------------------------------------------------------------


@dataclass
class Earnings:
    symbol: str
    date: str
    session: str


@dataclass
class Headline:
    symbol: Optional[str]
    title: str
    source: str
    url: str
    published: str


@dataclass
class Quote:
    last: float
    prev_close: float


@dataclass
class Provenance:
    sessions: list = field(default_factory=list)
    # GRE-3464: optional subset of `sessions` recorded and eligible for
    # replay download (Solari's replay id == the session id — see
    # schemas/scraped_data.schema.json). Omitted from to_dict() output when
    # empty so old-shape fixtures/consumers round-trip unchanged.
    replays: list = field(default_factory=list)


@dataclass
class ScrapedData:
    as_of: str
    universe: list
    earnings: list
    headlines: list
    quotes: dict
    provenance: Provenance
    warnings: list

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of,
            "universe": list(self.universe),
            "earnings": [asdict(e) if isinstance(e, Earnings) else dict(e) for e in self.earnings],
            "headlines": [
                asdict(h) if isinstance(h, Headline) else dict(h) for h in self.headlines
            ],
            "quotes": {
                sym: (asdict(q) if isinstance(q, Quote) else dict(q))
                for sym, q in self.quotes.items()
            },
            "provenance": {
                k: v
                for k, v in (
                    asdict(self.provenance)
                    if isinstance(self.provenance, Provenance)
                    else dict(self.provenance)
                ).items()
                # GRE-3464: omit "replays" entirely when empty (recording
                # off, or an older-shape caller that never set it) instead
                # of emitting a spurious "replays": [] — keeps pre-existing
                # fixtures/consumers round-tripping byte-for-byte.
                if not (k == "replays" and not v)
            },
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ScrapedData":
        return cls(
            as_of=data["as_of"],
            universe=list(data["universe"]),
            earnings=[Earnings(**e) for e in data["earnings"]],
            headlines=[Headline(**h) for h in data["headlines"]],
            quotes={sym: Quote(**q) for sym, q in data["quotes"].items()},
            provenance=Provenance(**data["provenance"]),
            warnings=list(data["warnings"]),
        )


def validate_scraped(data: dict) -> None:
    """Raise `jsonschema.ValidationError` if `data` does not match
    scraped_data.schema.json."""
    jsonschema.validate(instance=data, schema=_schema("scraped_data.schema.json"))


def load_scraped(path: str) -> ScrapedData:
    """Read, validate, and parse a scraped_data JSON file."""
    data = json.loads(Path(path).read_text())
    validate_scraped(data)
    return ScrapedData.from_dict(data)


# --------------------------------------------------------------------------
# signals
# --------------------------------------------------------------------------


@dataclass
class SymbolSignal:
    garch_vol_forecast_1d: float
    garch_vol_forecast_ann: float
    ou_zscore: float
    ou_half_life_d: float
    momentum_5d: float
    verdict: str
    notes: list = field(default_factory=list)
    # GRE-3464: optional finer-grained research label (see
    # schemas/signals.schema.json) — populated by desk/model_code/signals.py,
    # displayed by desk/brief.py when present. Omitted from to_dict() output
    # when None so pre-existing fixtures/consumers round-trip unchanged.
    label: Optional[str] = None


@dataclass
class Signals:
    as_of: str
    per_symbol: dict

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of,
            "per_symbol": {
                sym: {
                    k: v
                    for k, v in (asdict(s) if isinstance(s, SymbolSignal) else dict(s)).items()
                    # GRE-3464: omit "label" when unset (see SymbolSignal docstring above).
                    if not (k == "label" and v is None)
                }
                for sym, s in self.per_symbol.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Signals":
        return cls(
            as_of=data["as_of"],
            per_symbol={sym: SymbolSignal(**s) for sym, s in data["per_symbol"].items()},
        )


def validate_signals(data: dict) -> None:
    """Raise `jsonschema.ValidationError` if `data` does not match
    signals.schema.json."""
    jsonschema.validate(instance=data, schema=_schema("signals.schema.json"))


def load_signals(path: str) -> Signals:
    """Read, validate, and parse a signals JSON file."""
    data = json.loads(Path(path).read_text())
    validate_signals(data)
    return Signals.from_dict(data)
