"""signals.py — the textbook quant models (GARCH(1,1), OU/AR(1) mean
reversion, 5-day momentum) and the verdict rule table.

Pure numpy/arch/statsmodels, no network, no solari/sandbox imports: this
file is uploaded flat into the sandbox verbatim AND imported locally by the
hermetic test suite as `desk.model_code.signals` against bundled price
fixtures (`fixtures/prices/*.csv`, parsed via `prices.parse_stooq_csv`). Same
code path either way — GRE-3461 AC-2.

PUBLIC-SAFE: every formula here is the standard textbook definition (a
GARCH(1,1) variance forecast via the `arch` package, an AR(1) fit of
log-price as a discretized Ornstein-Uhlenbeck process, a naive N-day percent
change). Nothing here encodes proprietary signal logic — see the README
"Model rule table" section for the full, human-readable verdict rules.

GRE-3464 UPDATE: the CONTRACT GAP this file used to flag (research labels
like "mean-reversion-watch"/"trend-watch"/"event-risk"/"insufficient-data"
not fitting the closed `verdict` enum) is resolved by an additive `label`
field on the signals contract (`schemas/signals.schema.json`,
`desk/contracts.py`'s `SymbolSignal.label`). `decide_verdict` below now
returns the literal research label alongside the coarse `verdict` and the
human-readable note; `compute_symbol_signal` sets both. `verdict` stays the
closed four-value enum (bullish|bearish|neutral|avoid) — `label` is the
carrier for the ticket's real vocabulary, optional so older consumers that
only read `verdict`/`notes` are unaffected.
"""

from __future__ import annotations

import math
import warnings
from datetime import date, datetime
from typing import Optional

import numpy as np

MIN_TRADING_DAYS = 60           # below this, GARCH/OU fits are unreliable
MIN_RETURNS_FOR_GARCH = 20      # arch_model needs a reasonable sample
MIN_PRICES_FOR_OU = 10          # AR(1) needs a reasonable sample
TRADING_DAYS_PER_YEAR = 252

VOL_HIGH_ANN = 0.35             # annualized vol forecast considered "high"
VOL_LOW_ANN = 0.20              # annualized vol forecast considered "low"
Z_STRETCH = 1.5                 # |OU z-score| considered "stretched"
MOMENTUM_POS = 0.02             # 5d momentum considered positive trend
MOMENTUM_NEG = -0.02            # 5d momentum considered negative trend
EARNINGS_WINDOW_DAYS = 3        # calendar days considered "near-term"

VERDICT_VALUES = ("bullish", "bearish", "neutral", "avoid")


LABEL_VALUES = (
    "insufficient-data",
    "mean-reversion-watch",
    "trend-watch",
    "event-risk",
    "no-strong-signal",
)


def _signal_dict(
    vol_1d: float,
    vol_ann: float,
    zscore: float,
    half_life: float,
    momentum: float,
    verdict: str,
    label: str,
    notes: list[str],
) -> dict:
    assert verdict in VERDICT_VALUES, f"verdict {verdict!r} not in schema enum"
    assert label in LABEL_VALUES, f"label {label!r} not in schema enum"
    return {
        "garch_vol_forecast_1d": float(vol_1d),
        "garch_vol_forecast_ann": float(vol_ann),
        "ou_zscore": float(zscore),
        "ou_half_life_d": float(half_life),
        "momentum_5d": float(momentum),
        "verdict": verdict,
        "label": label,
        "notes": list(notes),
    }


def _fit_garch(pct_returns: np.ndarray) -> tuple[float, float, list[str]]:
    """Fit a textbook GARCH(1,1) (zero-mean, normal innovations) on percent
    returns and forecast 1-day-ahead variance. Returns (vol_1d, vol_ann,
    notes) with vol expressed as a fraction (not percent)."""
    from arch import arch_model  # local import: only needed on this path

    notes: list[str] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        am = arch_model(pct_returns, mean="Zero", vol="Garch", p=1, q=1, dist="normal")
        res = am.fit(disp="off", show_warning=False)

    if getattr(res, "convergence_flag", 0) != 0:
        notes.append(
            "garch-non-convergence: optimizer did not fully converge; forecast "
            "used anyway, treat as lower-confidence"
        )

    forecast = res.forecast(horizon=1, reindex=False)
    var_1d_pct2 = float(forecast.variance.values[-1, 0])
    if not math.isfinite(var_1d_pct2) or var_1d_pct2 < 0:
        raise ValueError(f"non-finite/negative GARCH variance forecast: {var_1d_pct2}")

    vol_1d = math.sqrt(var_1d_pct2) / 100.0  # back out of percent scale
    vol_ann = vol_1d * math.sqrt(TRADING_DAYS_PER_YEAR)
    return vol_1d, vol_ann, notes


def compute_garch_vol(pct_returns: np.ndarray) -> tuple[float, float, list[str]]:
    """GARCH(1,1) 1-day and annualized vol forecast, with a sample-std
    fallback on any fit failure (non-convergence exception, too little
    data, NaN/negative forecast) — NG-5: never let a numerical edge case
    crash the run."""
    if len(pct_returns) < MIN_RETURNS_FOR_GARCH:
        sample_vol_1d = _sample_vol_1d(pct_returns)
        return (
            sample_vol_1d,
            sample_vol_1d * math.sqrt(TRADING_DAYS_PER_YEAR),
            [
                f"garch-skipped: only {len(pct_returns)} return(s), need >= "
                f"{MIN_RETURNS_FOR_GARCH}; used sample-std annualized vol instead"
            ],
        )
    try:
        return _fit_garch(pct_returns)
    except Exception as exc:  # noqa: BLE001 - any arch/statsmodels failure
        sample_vol_1d = _sample_vol_1d(pct_returns)
        return (
            sample_vol_1d,
            sample_vol_1d * math.sqrt(TRADING_DAYS_PER_YEAR),
            [f"garch-fit-failed: {exc}; fell back to sample-std annualized vol"],
        )


def _sample_vol_1d(pct_returns: np.ndarray) -> float:
    if len(pct_returns) < 2:
        return 0.0
    return float(np.std(pct_returns, ddof=1)) / 100.0


def _fit_ou(closes: np.ndarray) -> tuple[float, float, list[str]]:
    """Fit AR(1) on log(price): log(P_t) = c + phi*log(P_t-1) + e_t, the
    standard discretization of an OU process. Returns (zscore, half_life_d,
    notes). zscore is the last log-price's distance from the fitted
    long-run mean in residual-std units; half_life is ln(0.5)/ln(phi)."""
    import statsmodels.api as sm

    notes: list[str] = []
    log_p = np.log(closes)
    y = log_p[1:]
    x = sm.add_constant(log_p[:-1])

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = sm.OLS(y, x).fit()

    const, phi = float(model.params[0]), float(model.params[1])
    resid_std = float(np.std(model.resid, ddof=1)) if len(model.resid) > 1 else 0.0

    if not (0.0 < phi < 1.0):
        notes.append(
            f"ou-ar1-phi-out-of-range: fitted phi={phi:.4f} is outside (0,1) "
            "(series isn't clearly mean-reverting over this window); clamped "
            "for a finite half-life"
        )
        phi_clamped = min(max(phi, 0.01), 0.99)
    else:
        phi_clamped = phi

    mean_level = const / (1.0 - phi_clamped) if phi_clamped != 1.0 else float(log_p.mean())
    zscore = (float(log_p[-1]) - mean_level) / resid_std if resid_std > 0 else 0.0
    half_life = math.log(0.5) / math.log(phi_clamped)
    half_life = min(half_life, 9999.0)  # keep JSON-finite for degenerate phi

    return zscore, half_life, notes


def compute_ou_stats(closes: np.ndarray) -> tuple[float, float, list[str]]:
    """OU z-score + half-life, with a zeroed fallback on any fit failure or
    too-short history — NG-5."""
    if len(closes) < MIN_PRICES_FOR_OU:
        return (
            0.0,
            0.0,
            [
                f"ou-skipped: only {len(closes)} price(s), need >= "
                f"{MIN_PRICES_FOR_OU}; z-score/half-life set to 0"
            ],
        )
    try:
        return _fit_ou(closes)
    except Exception as exc:  # noqa: BLE001
        return 0.0, 0.0, [f"ou-fit-failed: {exc}; z-score/half-life set to 0"]


def compute_momentum_5d(closes: np.ndarray) -> float:
    """5-trading-day percent change. 0.0 if there isn't a 6th price to
    compare against."""
    if len(closes) < 6:
        return 0.0
    return float(closes[-1] / closes[-6] - 1.0)


def has_earnings_soon(
    symbol: str,
    earnings: list[dict],
    as_of: str,
    window_days: int = EARNINGS_WINDOW_DAYS,
) -> tuple[bool, Optional[dict]]:
    """True if `symbol` has an earnings date within `window_days` calendar
    days on/after `as_of` (textbook simplicity: calendar days, not trading
    days). Returns the matching earnings row too, for the note."""
    try:
        as_of_date = datetime.fromisoformat(as_of.replace("Z", "+00:00")).date()
    except ValueError:
        return False, None

    for row in earnings:
        if row.get("symbol") != symbol:
            continue
        try:
            e_date = date.fromisoformat(row["date"])
        except (KeyError, ValueError):
            continue
        delta = (e_date - as_of_date).days
        if 0 <= delta <= window_days:
            return True, row
    return False, None


def decide_verdict(
    *,
    insufficient_data: bool,
    earnings_row: Optional[dict],
    vol_ann: float,
    zscore: float,
    momentum: float,
) -> tuple[str, str, str]:
    """Apply the rule table (first match wins) and return (verdict, label, note).

    `verdict` is one of the schema's four enum values; `label` is the
    literal research label (GRE-3464, `signals.schema.json`'s optional
    `label` field / `LABEL_VALUES` above); `note` is the same information as
    human-readable prose for `notes[]` (kept verbatim from the pre-GRE-3464
    text so existing substring-matching tests/consumers are unaffected).
    """
    if insufficient_data:
        return "avoid", "insufficient-data", (
            "insufficient-data: fewer than "
            f"{MIN_TRADING_DAYS} trading days of history; model fit is "
            "lower-confidence, treat as a conservative default"
        )

    if earnings_row is not None:
        return "avoid", "event-risk", (
            f"event-risk: earnings on {earnings_row['date']} "
            f"({earnings_row.get('session', 'unknown')}) within "
            f"{EARNINGS_WINDOW_DAYS}d — GARCH/OU forecasts likely understate "
            "event-driven vol"
        )

    if vol_ann >= VOL_HIGH_ANN and abs(zscore) >= Z_STRETCH:
        return "avoid", "mean-reversion-watch", (
            f"mean-reversion-watch: annualized vol forecast {vol_ann:.1%} >= "
            f"{VOL_HIGH_ANN:.0%} and |OU z-score| {abs(zscore):.2f} >= "
            f"{Z_STRETCH} (stretched vs fitted mean, high forecast vol)"
        )

    if vol_ann < VOL_LOW_ANN and momentum >= MOMENTUM_POS:
        return "bullish", "trend-watch", (
            f"trend-watch: annualized vol forecast {vol_ann:.1%} < "
            f"{VOL_LOW_ANN:.0%} and 5d momentum {momentum:+.1%} >= "
            f"{MOMENTUM_POS:.0%}"
        )

    if vol_ann < VOL_LOW_ANN and momentum <= MOMENTUM_NEG:
        return "bearish", "trend-watch", (
            f"trend-watch: annualized vol forecast {vol_ann:.1%} < "
            f"{VOL_LOW_ANN:.0%} and 5d momentum {momentum:+.1%} <= "
            f"{MOMENTUM_NEG:.0%}"
        )

    return "neutral", "no-strong-signal", (
        "no-strong-signal: vol forecast, OU z-score, and momentum are all "
        "inside normal ranges"
    )


def compute_symbol_signal(
    symbol: str,
    closes: list[float],
    earnings: list[dict],
    as_of: str,
) -> dict:
    """Compute the full per-symbol signal dict (matches
    `signals.schema.json`'s per-symbol object exactly) from a list of daily
    closes, oldest first. Never raises — any numerical edge case degrades to
    a fallback value plus a `notes[]` entry (NG-5)."""
    notes: list[str] = []
    closes_arr = np.asarray(closes, dtype=float)
    n = len(closes_arr)

    if n < 2:
        notes.append(f"insufficient-data: only {n} price point(s) fetched; no returns computable")
        verdict, label, verdict_note = decide_verdict(
            insufficient_data=True, earnings_row=None, vol_ann=0.0, zscore=0.0, momentum=0.0
        )
        notes.append(verdict_note)
        return _signal_dict(0.0, 0.0, 0.0, 0.0, 0.0, verdict, label, notes)

    pct_returns = np.diff(closes_arr) / closes_arr[:-1] * 100.0

    insufficient = n < MIN_TRADING_DAYS
    if insufficient:
        notes.append(
            f"insufficient-data: {n} trading days available (< {MIN_TRADING_DAYS} "
            "required); GARCH/OU fits below this threshold are lower-confidence "
            "even when individually computable"
        )

    vol_1d, vol_ann, garch_notes = compute_garch_vol(pct_returns)
    notes.extend(garch_notes)

    zscore, half_life, ou_notes = compute_ou_stats(closes_arr)
    notes.extend(ou_notes)

    momentum = compute_momentum_5d(closes_arr)

    earnings_soon, earnings_row = has_earnings_soon(symbol, earnings, as_of)

    verdict, label, verdict_note = decide_verdict(
        insufficient_data=insufficient,
        earnings_row=earnings_row,
        vol_ann=vol_ann,
        zscore=zscore,
        momentum=momentum,
    )
    notes.append(verdict_note)

    return _signal_dict(vol_1d, vol_ann, zscore, half_life, momentum, verdict, label, notes)
