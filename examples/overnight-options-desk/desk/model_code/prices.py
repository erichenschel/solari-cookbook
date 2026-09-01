"""prices.py — pure parsing of daily-close price history. No network, no
sandbox-specific imports: this file is uploaded flat into the sandbox
verbatim AND imported locally by the hermetic tests (`desk.model_code.prices`)
against the same bundled CSV fixtures used by production fetches.
"""

from __future__ import annotations

import csv
import io


def parse_stooq_csv(text: str) -> list[float]:
    """Parse a Stooq daily-history CSV (`Date,Open,High,Low,Close,Volume`)
    into an ordered list of closes, oldest first.

    Stooq returns rows oldest-to-newest already; we don't re-sort so a
    caller can feed in any CSV with that same header shape (including the
    bundled fixtures, which are hand-built in this format for exactly that
    reason).
    """
    reader = csv.DictReader(io.StringIO(text.strip()))
    closes: list[float] = []
    for row in reader:
        raw = row.get("Close") or row.get("close")
        if raw is None:
            continue
        try:
            closes.append(float(raw))
        except ValueError:
            continue
    return closes


def parse_yahoo_chart(payload: dict) -> list[float]:
    """Parse a Yahoo `v8/finance/chart` JSON payload into an ordered list of
    closes, oldest first, dropping the `null` gaps Yahoo emits for
    non-trading timestamps."""
    try:
        result = payload["chart"]["result"][0]
        raw_closes = result["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"unexpected yahoo chart payload shape: {exc}") from exc
    return [float(c) for c in raw_closes if c is not None]


def closes_from_csv_file(path: str) -> list[float]:
    """Convenience wrapper: read a Stooq-format CSV file from disk and parse
    it. Used by the hermetic test suite against `fixtures/prices/*.csv`."""
    with open(path, "r", encoding="utf-8") as fh:
        return parse_stooq_csv(fh.read())
