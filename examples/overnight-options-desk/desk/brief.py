"""desk/brief.py — render `scraped_data` + `signals` into one self-contained
`brief.html`.

Pure and hermetic (NG-3): bytes in (two validated contract objects) -> one
HTML string out. No Solari calls live here — `desk/serve.py` is the only
module in this lane that touches the API. No external requests from the
rendered page either (NG-1): no CDN fonts, no analytics, no JS at all —
structure is plain HTML tables/sections plus inline SVG bars (NG-2, no
charting framework), so the page is fully readable with JS disabled and from
a bare `file://` URL.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from jinja2 import Environment

from desk.contracts import ScrapedData, Signals, load_scraped, load_signals

DISCLAIMER = "Research only — not investment advice."
_ALLOWED_URL_SCHEMES = {"http", "https"}

# Visual scale caps for the inline SVG bars — clipped, not truncated data:
# a value beyond the cap still renders (full bar + exact number in the cell),
# it just stops growing the bar past 100% width.
_ZSCORE_SCALE = 3.0
_VOL_FLOOR = 0.10  # keep the vol bar legible even if every symbol is calm


# --------------------------------------------------------------------------
# small pure helpers
# --------------------------------------------------------------------------


def _fmt_dt(iso: str) -> str:
    """'2026-08-31T06:00:00Z' -> '2026-08-31 06:00 UTC'. Falls back to the
    raw string if it doesn't parse — never raise on display formatting."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, AttributeError):
        return iso


def _fmt_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return iso


def _safe_url(url: str) -> str:
    """Only ever emit an http(s) href — anything else (javascript:, data:,
    a malformed string) collapses to '#' rather than being trusted as-is."""
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        return "#"
    return url if scheme in _ALLOWED_URL_SCHEMES else "#"


def _session_label(session: str) -> str:
    return {"bmo": "Before open", "amc": "After close", "unknown": "Unknown"}.get(
        session, session
    )


def _verdict_class(verdict: str) -> str:
    return {
        "bullish": "v-bullish",
        "bearish": "v-bearish",
        "avoid": "v-avoid",
        "neutral": "v-neutral",
    }.get(verdict, "v-neutral")


def _pct(x: float, digits: int = 2) -> str:
    return f"{x * 100:+.{digits}f}%"


def _zscore_bar_svg(z: float) -> str:
    """Diverging horizontal bar centered at 0, clipped to +/- _ZSCORE_SCALE."""
    width, height, center = 100, 14, 50
    clipped = max(-_ZSCORE_SCALE, min(_ZSCORE_SCALE, z))
    half = clipped / _ZSCORE_SCALE * center
    color = "#3fb950" if z >= 0 else "#f85149"
    x = center if half >= 0 else center + half
    w = abs(half)
    return (
        f'<svg class="bar zbar" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="z-score {z:+.2f}">'
        f'<line x1="{center}" y1="0" x2="{center}" y2="{height}" stroke="#484f58" stroke-width="1"/>'
        f'<rect x="{x:.1f}" y="2" width="{w:.1f}" height="{height - 4}" fill="{color}" rx="1"/>'
        f"</svg>"
    )


def _vol_bar_svg(vol: float, vol_max: float) -> str:
    width, height = 100, 14
    frac = 0.0 if vol_max <= 0 else max(0.0, min(1.0, vol / vol_max))
    w = frac * width
    return (
        f'<svg class="bar volbar" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="annualized vol forecast {vol:.1%}">'
        f'<rect x="0" y="2" width="{width}" height="{height - 4}" fill="#21262d" rx="1"/>'
        f'<rect x="0" y="2" width="{w:.1f}" height="{height - 4}" fill="#d29922" rx="1"/>'
        f"</svg>"
    )


def _momentum_arrow(m: float) -> str:
    if m > 0:
        return f'<span class="mom mom-up">&#9650; {_pct(m)}</span>'
    if m < 0:
        return f'<span class="mom mom-down">&#9660; {_pct(m)}</span>'
    return f'<span class="mom mom-flat">&#8212; {_pct(m)}</span>'


# --------------------------------------------------------------------------
# context assembly
# --------------------------------------------------------------------------


def _build_context(scraped: ScrapedData, signals: Signals) -> dict:
    covered = signals.per_symbol
    vol_max = max([s.garch_vol_forecast_ann for s in covered.values()], default=0.0)
    vol_max = max(vol_max * 1.15, _VOL_FLOOR)

    ranked = sorted(covered.items(), key=lambda kv: abs(kv[1].ou_zscore), reverse=True)
    signal_rows = []
    for rank, (symbol, sig) in enumerate(ranked, start=1):
        quote = scraped.quotes.get(symbol)
        chg_pct = None
        if quote is not None and quote.prev_close:
            chg_pct = (quote.last - quote.prev_close) / quote.prev_close
        signal_rows.append(
            {
                "rank": rank,
                "symbol": symbol,
                "last": quote.last if quote else None,
                "chg_pct": chg_pct,
                "garch_1d": sig.garch_vol_forecast_1d,
                "garch_ann": sig.garch_vol_forecast_ann,
                "vol_svg": _vol_bar_svg(sig.garch_vol_forecast_ann, vol_max),
                "zscore": sig.ou_zscore,
                "zscore_svg": _zscore_bar_svg(sig.ou_zscore),
                "half_life": sig.ou_half_life_d,
                "momentum": sig.momentum_5d,
                "momentum_html": _momentum_arrow(sig.momentum_5d),
                "verdict": sig.verdict,
                "verdict_class": _verdict_class(sig.verdict),
                "notes": sig.notes,
            }
        )
    uncovered = [s for s in scraped.universe if s not in covered]

    earnings_sorted = sorted(scraped.earnings, key=lambda e: e.date)
    earnings_rows = []
    for e in earnings_sorted:
        sig = covered.get(e.symbol)
        earnings_rows.append(
            {
                "symbol": e.symbol,
                "date": _fmt_date(e.date),
                "session": _session_label(e.session),
                "verdict": sig.verdict if sig else None,
                "verdict_class": _verdict_class(sig.verdict) if sig else None,
                "zscore": sig.ou_zscore if sig else None,
            }
        )

    headline_groups = []
    for symbol in scraped.universe:
        items = [h for h in scraped.headlines if h.symbol == symbol]
        headline_groups.append(
            {
                "symbol": symbol,
                # NB: deliberately not called "items" — dict.items is a real
                # attribute, and Jinja's dot-lookup tries attribute access
                # before item access, so `g.items` would silently resolve to
                # the bound method instead of this list.
                "headlines": [
                    {
                        "title": h.title,
                        "source": h.source,
                        "url": _safe_url(h.url),
                        "published": _fmt_dt(h.published),
                    }
                    for h in items
                ],
            }
        )
    macro_headlines = [
        {
            "title": h.title,
            "source": h.source,
            "url": _safe_url(h.url),
            "published": _fmt_dt(h.published),
        }
        for h in scraped.headlines
        if h.symbol is None
    ]

    return {
        "as_of": _fmt_dt(scraped.as_of),
        "signals_as_of": _fmt_dt(signals.as_of),
        "universe": scraped.universe,
        "signal_rows": signal_rows,
        "uncovered": uncovered,
        "earnings_rows": earnings_rows,
        "headline_groups": headline_groups,
        "macro_headlines": macro_headlines,
        "sessions": scraped.provenance.sessions,
        "warnings": scraped.warnings,
        "disclaimer": DISCLAIMER,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


# --------------------------------------------------------------------------
# template
# --------------------------------------------------------------------------

_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Overnight Options Desk Brief — {{ as_of }}</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #0a0e14;
    --panel: #0d1117;
    --border: #21262d;
    --text: #c9d1d9;
    --dim: #7d8590;
    --accent: #58a6ff;
    --green: #3fb950;
    --red: #f85149;
    --amber: #d29922;
    --gray: #8b949e;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
    font-size: 14px;
    line-height: 1.5;
    padding: 0 0 3rem;
  }
  a { color: var(--accent); }
  .wrap { max-width: 960px; margin: 0 auto; padding: 0 1rem; }
  header.brief-header {
    border-bottom: 1px solid var(--border);
    background: var(--panel);
    padding: 1.25rem 0 1rem;
    margin-bottom: 1.5rem;
  }
  header.brief-header .wrap { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: baseline; gap: .5rem 1.5rem; }
  h1 { font-size: 1.1rem; letter-spacing: .04em; text-transform: uppercase; margin: 0; color: #e6edf3; }
  .as-of { color: var(--dim); font-size: .85rem; }
  .universe { color: var(--dim); font-size: .85rem; }
  .universe strong { color: var(--text); }
  section { margin: 0 0 2rem; }
  section > h2 {
    font-size: .8rem;
    letter-spacing: .08em;
    text-transform: uppercase;
    color: var(--dim);
    border-bottom: 1px solid var(--border);
    padding-bottom: .35rem;
    margin: 0 0 .75rem;
  }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: .5rem .6rem; border-bottom: 1px solid var(--border); vertical-align: middle; }
  th { font-size: .7rem; letter-spacing: .05em; text-transform: uppercase; color: var(--dim); font-weight: 600; }
  tbody tr:hover { background: rgba(255,255,255,0.02); }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  .bar { display: block; width: 90px; height: 14px; }
  .mom-up { color: var(--green); }
  .mom-down { color: var(--red); }
  .mom-flat { color: var(--dim); }
  .badge {
    display: inline-block;
    padding: .1rem .5rem;
    border-radius: 3px;
    font-size: .72rem;
    letter-spacing: .04em;
    text-transform: uppercase;
    font-weight: 700;
    border: 1px solid transparent;
  }
  .v-bullish { color: var(--green); border-color: var(--green); background: rgba(63,185,80,.1); }
  .v-bearish { color: var(--red); border-color: var(--red); background: rgba(248,81,73,.1); }
  .v-avoid   { color: var(--red); border-color: var(--red); background: rgba(248,81,73,.15); }
  .v-neutral { color: var(--gray); border-color: var(--gray); background: rgba(139,148,158,.08); }
  .uncovered-note { color: var(--dim); font-size: .8rem; margin-top: .5rem; }
  .callouts { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: .75rem; }
  .callout { border: 1px solid var(--border); background: var(--panel); border-radius: 6px; padding: .75rem .9rem; }
  .callout .sym { font-weight: 700; color: #e6edf3; font-size: .95rem; }
  .callout .date { color: var(--dim); font-size: .8rem; margin-top: .15rem; }
  .callout .badge { margin-top: .5rem; }
  .headline-group { margin-bottom: 1rem; }
  .headline-group h3 { font-size: .85rem; color: #e6edf3; margin: 0 0 .35rem; }
  .headline-group ul { list-style: none; margin: 0; padding: 0; }
  .headline-group li { padding: .35rem 0; border-bottom: 1px dotted var(--border); }
  .headline-group li:last-child { border-bottom: none; }
  .headline-meta { color: var(--dim); font-size: .78rem; }
  .empty { color: var(--dim); font-style: italic; font-size: .85rem; }
  footer.provenance {
    border-top: 1px solid var(--border);
    margin-top: 2rem;
    padding-top: 1rem;
    color: var(--dim);
    font-size: .78rem;
  }
  footer.provenance .disclaimer {
    color: var(--amber);
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .04em;
    font-size: .75rem;
    margin-bottom: .5rem;
  }
  footer.provenance .sessions code { color: var(--text); }
  .warnings { color: var(--amber); font-size: .8rem; margin-top: .5rem; }

  @media (max-width: 640px) {
    table.responsive thead { display: none; }
    table.responsive, table.responsive tbody, table.responsive tr, table.responsive td {
      display: block; width: 100%;
    }
    table.responsive tr { border: 1px solid var(--border); border-radius: 6px; margin-bottom: .6rem; padding: .3rem .6rem; }
    table.responsive td {
      display: flex; justify-content: space-between; align-items: center;
      border-bottom: 1px dotted var(--border); padding: .4rem 0; text-align: right;
    }
    table.responsive td:last-child { border-bottom: none; }
    table.responsive td::before {
      content: attr(data-label);
      color: var(--dim); text-transform: uppercase; font-size: .68rem;
      letter-spacing: .04em; margin-right: .5rem; text-align: left;
    }
  }
</style>
</head>
<body>
<header class="brief-header" id="header">
  <div class="wrap">
    <h1>Overnight Options Desk &mdash; Morning Brief</h1>
    <div class="as-of">As of <strong>{{ as_of }}</strong> &middot; signals as of {{ signals_as_of }}</div>
    <div class="universe">Universe: {% for s in universe %}<strong>{{ s }}</strong>{% if not loop.last %}, {% endif %}{% endfor %}</div>
  </div>
</header>

<div class="wrap">

  <section id="signals">
    <h2>Ranked signals (by |OU z-score|)</h2>
    <div style="overflow-x:auto">
    <table class="responsive">
      <thead>
        <tr>
          <th>#</th><th>Sym</th><th class="num">Last</th><th class="num">Chg</th>
          <th>Vol (1d / ann)</th><th>OU z-score</th><th class="num">Half-life (d)</th>
          <th>Mom 5d</th><th>Verdict</th>
        </tr>
      </thead>
      <tbody>
        {% for r in signal_rows %}
        <tr>
          <td data-label="#">{{ r.rank }}</td>
          <td data-label="Symbol"><strong>{{ r.symbol }}</strong></td>
          <td data-label="Last" class="num">{% if r.last is not none %}{{ "%.2f"|format(r.last) }}{% else %}&mdash;{% endif %}</td>
          <td data-label="Chg" class="num">{% if r.chg_pct is not none %}<span class="{{ 'mom-up' if r.chg_pct >= 0 else 'mom-down' }}">{{ "%+.2f%%"|format(r.chg_pct * 100) }}</span>{% else %}&mdash;{% endif %}</td>
          <td data-label="Vol 1d/ann">{{ r.vol_svg|safe }} {{ "%.2f%%"|format(r.garch_1d * 100) }} / {{ "%.1f%%"|format(r.garch_ann * 100) }}</td>
          <td data-label="OU z-score">{{ r.zscore_svg|safe }} {{ "%+.2f"|format(r.zscore) }}</td>
          <td data-label="Half-life" class="num">{{ "%.1f"|format(r.half_life) }}</td>
          <td data-label="Momentum 5d">{{ r.momentum_html|safe }}</td>
          <td data-label="Verdict"><span class="badge {{ r.verdict_class }}">{{ r.verdict }}</span></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    </div>
    {% if uncovered %}
    <div class="uncovered-note">No signal coverage: {% for s in uncovered %}{{ s }}{% if not loop.last %}, {% endif %}{% endfor %}</div>
    {% endif %}
    {% if warnings %}
    <div class="warnings">&#9888; {% for w in warnings %}{{ w }}{% if not loop.last %}; {% endif %}{% endfor %}</div>
    {% endif %}
  </section>

  <section id="earnings">
    <h2>Earnings in window</h2>
    {% if earnings_rows %}
    <div class="callouts">
      {% for e in earnings_rows %}
      <div class="callout">
        <div class="sym">{{ e.symbol }}</div>
        <div class="date">{{ e.date }} &middot; {{ e.session }}</div>
        {% if e.verdict %}
        <div class="badge {{ e.verdict_class }}">{{ e.verdict }} (z {{ "%+.2f"|format(e.zscore) }})</div>
        {% endif %}
      </div>
      {% endfor %}
    </div>
    {% else %}
    <div class="empty">No earnings in the current window.</div>
    {% endif %}
  </section>

  <section id="headlines">
    <h2>Headlines</h2>
    {% for g in headline_groups %}
    <div class="headline-group">
      <h3>{{ g.symbol }}</h3>
      {% if g.headlines %}
      <ul>
        {% for h in g.headlines %}
        <li>
          <a href="{{ h.url }}" rel="noopener noreferrer nofollow" target="_blank">{{ h.title }}</a>
          <div class="headline-meta">{{ h.source }} &middot; {{ h.published }}</div>
        </li>
        {% endfor %}
      </ul>
      {% else %}
      <div class="empty">No headlines captured.</div>
      {% endif %}
    </div>
    {% endfor %}
    {% if macro_headlines %}
    <div class="headline-group">
      <h3>Market-wide</h3>
      <ul>
        {% for h in macro_headlines %}
        <li>
          <a href="{{ h.url }}" rel="noopener noreferrer nofollow" target="_blank">{{ h.title }}</a>
          <div class="headline-meta">{{ h.source }} &middot; {{ h.published }}</div>
        </li>
        {% endfor %}
      </ul>
    </div>
    {% endif %}
  </section>

</div>

<footer class="provenance" id="provenance">
  <div class="wrap">
    <div class="disclaimer">{{ disclaimer }}</div>
    <div class="sessions">Solari session ids:
      {% if sessions %}
        {% for s in sessions %}<code>{{ s }}</code>{% if not loop.last %}, {% endif %}{% endfor %}
      {% else %}
        <span class="empty">none recorded</span>
      {% endif %}
    </div>
    <div>Rendered {{ generated_at }} &middot; desk/brief.py (hermetic, no external requests)</div>
  </div>
</footer>

</body>
</html>
"""


def render_brief(scraped: ScrapedData, signals: Signals) -> str:
    """Pure render: two validated contract objects -> one self-contained
    HTML document string. No I/O, no Solari calls."""
    env = Environment(autoescape=True)
    template = env.from_string(_TEMPLATE)
    return template.render(**_build_context(scraped, signals))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render scraped_data.json + signals.json into one self-contained brief.html."
    )
    parser.add_argument("--scraped", required=True, help="Path to a scraped_data JSON file.")
    parser.add_argument("--signals", required=True, help="Path to a signals JSON file.")
    parser.add_argument("--out", required=True, help="Path to write the rendered brief.html to.")
    args = parser.parse_args(argv)

    scraped = load_scraped(args.scraped)
    signals = load_signals(args.signals)
    html_out = render_brief(scraped, signals)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_out, encoding="utf-8")
    print(f"wrote {out_path} ({len(html_out)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
