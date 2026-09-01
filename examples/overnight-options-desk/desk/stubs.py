"""stubs.py — offline stand-ins for the three sibling lanes (scraper, models,
brief) plus the serve step, selected by `run_overnight.py` when `--stubs`,
`DESK_STUBS=1`, or `--dry-run` is active.

Each stub derives its output from the GRE-3459 spike fixtures
(`fixtures/scraped_data.json`, `fixtures/signals.json`) so the orchestrator
can be exercised end-to-end with ZERO network/API calls. They are
intentionally simple — no scraping, no model math, no real rendering logic
(NG-2) — just enough shape to satisfy `desk/contracts.py` validation and let
`run_overnight.py`'s sequencing, checkpointing, and degrade-on-failure logic
be tested hermetically.

The integration ticket swaps these for subprocess calls into the real
`desk.scraper` / `desk.models` / `desk.brief` / `desk.serve` CLIs (see
`run_overnight.py`'s `_run_subprocess_stage`).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from desk import contracts

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = PACKAGE_ROOT / "fixtures"

_STUB_SYMBOL_DEFAULTS = {
    "quote": {"last": 100.0, "prev_close": 100.0},
    "signal": {
        "garch_vol_forecast_1d": 0.02,
        "garch_vol_forecast_ann": 0.3,
        "ou_zscore": 0.0,
        "ou_half_life_d": 5.0,
        "momentum_5d": 0.0,
        "verdict": "neutral",
        "notes": ["stub — no fixture coverage for this symbol"],
    },
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_json(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def _try_load_json(path: Optional[Path]) -> Optional[dict]:
    if path is None:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def stub_scrape(
    symbols: Iterable[str], out_path: Path, fixtures_dir: Path = FIXTURES_DIR
) -> float:
    """Derive a scraped_data.json for `symbols` from the spike fixture.
    Returns the (zero) estimated spend."""
    base = json.loads((fixtures_dir / "scraped_data.json").read_text())
    universe = list(symbols)
    quotes = {
        sym: base["quotes"].get(sym, dict(_STUB_SYMBOL_DEFAULTS["quote"])) for sym in universe
    }
    earnings = [e for e in base["earnings"] if e["symbol"] in universe]
    headlines = [h for h in base["headlines"] if h["symbol"] in universe or h["symbol"] is None]
    data = {
        "as_of": _now_iso(),
        "universe": universe,
        "earnings": earnings,
        "headlines": headlines,
        "quotes": quotes,
        "provenance": {"sessions": [f"stub_sess_{sym.lower()}" for sym in universe]},
        "warnings": ["stub data — no live scrape performed (DESK_STUBS)"],
    }
    contracts.validate_scraped(data)
    _atomic_write_json(Path(out_path), data)
    return 0.0


def stub_models(scraped_path: Path, out_path: Path, fixtures_dir: Path = FIXTURES_DIR) -> float:
    """Derive a signals.json for the universe found in `scraped_path` from
    the spike fixture. Returns the (zero) estimated spend."""
    base = json.loads((fixtures_dir / "signals.json").read_text())
    scraped = json.loads(Path(scraped_path).read_text())
    universe = scraped["universe"]
    per_symbol = {}
    for sym in universe:
        if sym in base["per_symbol"]:
            per_symbol[sym] = base["per_symbol"][sym]
        else:
            per_symbol[sym] = dict(_STUB_SYMBOL_DEFAULTS["signal"])
    data = {"as_of": _now_iso(), "per_symbol": per_symbol}
    contracts.validate_signals(data)
    _atomic_write_json(Path(out_path), data)
    return 0.0


def stub_brief(
    scraped_path: Optional[Path], signals_path: Optional[Path], out_path: Path
) -> float:
    """Render brief.html from whatever of scraped_data / signals is
    available — the degrade-gracefully contract AC-2 exercises. Never
    raises for missing inputs; only an injected DESK_FAIL_STAGE (checked by
    the caller) or a write error can fail this stage."""
    scraped = _try_load_json(scraped_path)
    signals = _try_load_json(signals_path)

    universe = []
    if scraped:
        universe = list(scraped.get("universe", []))
    elif signals:
        universe = list(signals.get("per_symbol", {}))

    rows = []
    for sym in universe:
        quote = (scraped or {}).get("quotes", {}).get(sym, {})
        sig = (signals or {}).get("per_symbol", {}).get(sym, {})
        rows.append(
            "<tr>"
            f"<td>{sym}</td>"
            f"<td>{quote.get('last', '—')}</td>"
            f"<td>{sig.get('verdict', 'no signal')}</td>"
            "</tr>"
        )

    warnings = list((scraped or {}).get("warnings", []))
    as_of = (scraped or {}).get("as_of") or (signals or {}).get("as_of") or "unknown"

    notes = []
    if not scraped:
        notes.append("<p><em>scraped_data unavailable — degraded brief.</em></p>")
    if not signals:
        notes.append("<p><em>signals unavailable — degraded brief.</em></p>")

    table_body = "".join(rows) or "<tr><td colspan=\"3\">No symbol data available</td></tr>"
    warnings_html = "".join(f"<li>{w}</li>" for w in warnings) or "<li>none</li>"

    html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Overnight Options Desk Brief</title></head>
<body>
<h1>Overnight Options Desk — {as_of}</h1>
{''.join(notes)}
<table border="1" cellpadding="4">
<tr><th>Symbol</th><th>Last</th><th>Verdict</th></tr>
{table_body}
</table>
<h2>Warnings</h2>
<ul>{warnings_html}</ul>
</body>
</html>
"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html)
    return 0.0


def stub_serve(file_path: Path) -> tuple[str, float]:
    """Return a fake local preview URL without touching the network.
    Returns (preview_url, spend)."""
    return f"http://127.0.0.1:8000/{Path(file_path).name}", 0.0
