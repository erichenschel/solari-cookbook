"""run_overnight.py — single entry point chaining scrape -> models -> render
-> serve for the overnight options desk.

Orchestration only (NG-2): this module owns sequencing, checkpointing,
retry/degrade behavior, budget accounting, and the final publish step. It
does not scrape, model, or render anything itself — each stage is either a
subprocess call into the real sibling CLI (`desk.scraper` / `desk.models` /
`desk.brief` / `desk.serve`, built by parallel lanes) or, when stubs are
selected, a fixture-derived stand-in from `desk/stubs.py`.

    # hermetic — zero API calls, all four stages stubbed
    python -m desk.run_overnight --dry-run --symbols AAPL,NVDA

    # live chain (once the sibling CLIs exist)
    python -m desk.run_overnight --symbols AAPL,NVDA,MSFT,TSLA

    # resume a partial run, skipping stages with valid artifacts
    python -m desk.run_overnight --resume runs/2026-08-31

Each stage opens and kills its own Solari sessions (NG-1): real stages are
invoked as a fresh subprocess per stage (a new process, so no session can
leak across stage boundaries); stub stages never open a session at all.

Env:
    DESK_STUBS=1        same effect as --stubs
    DESK_FAIL_STAGE=X   force stage X (scraper|models|brief|serve) to fail
                        on every attempt — used by hermetic tests to exercise
                        the retry-then-degrade path (AC-2).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from desk import contracts, stubs

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = PACKAGE_ROOT / "fixtures"

STAGE_NAMES = ["scraper", "models", "brief", "serve"]

_SPEND_LINE_RE = re.compile(r"\[spend\].*?\$([0-9]*\.?[0-9]+)")

# GRE-3464: how long to wait for `desk.serve` to print its preview URL
# before giving up. `desk.serve` prints the URL as its very first line of
# output (right after the sandbox comes up), then blocks — see
# `_do_serve`'s docstring for why the orchestrator must not wait for it to
# exit the way `_run_subprocess_stage` does for the other three stages.
SERVE_URL_WAIT_S = 90.0

# Mirrors desk.solari_client.SANDBOX_RATE_PER_HOUR — kept as a local
# constant (not an import) so the orchestrator never depends on the Solari
# SDK directly (NG-2: it only ever talks to Solari via a subprocess into a
# sibling CLI). Used only to print an *estimated* spend line for the serve
# stage (see `_do_serve`).
_SANDBOX_RATE_PER_HOUR_ESTIMATE = 0.10

logger = logging.getLogger("desk.run_overnight")


class StageSkipped(Exception):
    """Raised internally to signal a stage should be skipped (not retried,
    not counted as a failure) because a required upstream input is
    missing."""


@dataclass
class StageOutcome:
    name: str
    status: str  # "ok" | "failed" | "skipped"
    attempts: int = 0
    duration_s: float = 0.0
    spend_usd: float = 0.0
    error: Optional[str] = None
    reason: Optional[str] = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "attempts": self.attempts,
            "duration_s": round(self.duration_s, 3),
            "spend_usd": round(self.spend_usd, 6),
            "error": self.error,
            "reason": self.reason,
            **self.extra,
        }


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m desk.run_overnight",
        description="Chain scrape -> models -> render -> serve for the overnight options desk.",
    )
    p.add_argument(
        "--symbols",
        help="Comma-separated symbol universe, e.g. AAPL,NVDA. Required unless "
        "--resume finds a valid scraped_data.json already checkpointed.",
    )
    p.add_argument(
        "--out-root",
        default="runs",
        help="Root directory for per-day run artifacts (default: runs/).",
    )
    p.add_argument(
        "--resume",
        metavar="RUN_DIR",
        help="Resume an existing runs/<date> directory, skipping stages whose "
        "valid artifacts already exist.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the full chain on fixtures/stubs with ZERO API calls (forces stub stages).",
    )
    p.add_argument(
        "--stubs",
        action="store_true",
        help="Use fixture-derived stub stages instead of the real sibling CLIs "
        "(also settable via DESK_STUBS=1). Implied by --dry-run.",
    )
    p.add_argument(
        "--retries",
        type=int,
        default=1,
        help="Retries per stage after the first attempt (default: 1).",
    )
    p.add_argument(
        "--docs-out",
        default=None,
        help="Where to publish the final brief.html copy "
        "(default: <package root>/docs/latest/index.html).",
    )
    p.add_argument(
        "--serve-port",
        type=int,
        default=8000,
        help="Port passed to the real desk.serve CLI (ignored in stub mode).",
    )
    p.add_argument(
        "--serve-hold-seconds",
        type=float,
        default=3600.0,
        help=(
            "How long the real desk.serve subprocess holds the preview open "
            "after this run finishes (default: 3600s = 1hr, desk/serve.py's "
            "free-tier sandbox lifetime cap — see MAX_HOLD_S there). The "
            "orchestrator reads the preview URL from desk.serve's stdout as "
            "soon as it's printed and does NOT wait for the subprocess to "
            "exit; the server keeps running in the background afterward. "
            "Ignored in stub mode."
        ),
    )
    return p


def _split_symbols(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


# --------------------------------------------------------------------------
# spend parsing
# --------------------------------------------------------------------------


def _parse_spend_from_output(output: str) -> float:
    """Parse solari_client's `[spend] resource: Ns ~= $X` log lines, summed
    across every resource the stage opened. Falls back to a trailing
    `{"spend_usd": ...}` JSON line if a stage self-reports instead."""
    total = 0.0
    found = False
    for line in output.splitlines():
        m = _SPEND_LINE_RE.search(line)
        if m:
            total += float(m.group(1))
            found = True
    if found:
        return total
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            break
        if isinstance(obj, dict) and "spend_usd" in obj:
            return float(obj["spend_usd"])
        break
    return 0.0


def _parse_preview_url(output: str) -> Optional[str]:
    for line in reversed(output.strip().splitlines()):
        m = re.search(r"https?://\S+", line)
        if m:
            return m.group(0)
    return None


def _run_subprocess_stage(cmd: list[str], cwd: Path, timeout: int = 600) -> tuple[str, float]:
    """Run one sibling-lane CLI as its own process (own Solari session
    lifecycle, per NG-1). Returns (stdout, spend_usd); raises on nonzero
    exit."""
    proc = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"`{' '.join(cmd)}` exited {proc.returncode}: {proc.stderr.strip()[:2000]}"
        )
    return proc.stdout, _parse_spend_from_output(proc.stdout)


# --------------------------------------------------------------------------
# stage bodies
# --------------------------------------------------------------------------


def _check_injected_failure(name: str) -> None:
    if os.environ.get("DESK_FAIL_STAGE") == name:
        raise RuntimeError(f"injected failure via DESK_FAIL_STAGE={name}")


def _do_scraper(symbols: list[str], out_path: Path, use_stubs: bool) -> float:
    _check_injected_failure("scraper")
    if use_stubs:
        spend = stubs.stub_scrape(symbols, out_path, FIXTURES_DIR)
    else:
        _, spend = _run_subprocess_stage(
            [sys.executable, "-m", "desk.scraper", "--symbols", ",".join(symbols), "--out", str(out_path)],
            cwd=PACKAGE_ROOT,
        )
    contracts.load_scraped(str(out_path))  # validate what landed on disk
    return spend


def _do_models(scraped_path: Path, out_path: Path, use_stubs: bool) -> float:
    _check_injected_failure("models")
    if use_stubs:
        spend = stubs.stub_models(scraped_path, out_path, FIXTURES_DIR)
    else:
        _, spend = _run_subprocess_stage(
            [sys.executable, "-m", "desk.models", "--scraped", str(scraped_path), "--out", str(out_path)],
            cwd=PACKAGE_ROOT,
        )
    contracts.load_signals(str(out_path))
    return spend


def _do_brief(
    scraped_path: Path, signals_path: Path, out_path: Path, use_stubs: bool
) -> float:
    _check_injected_failure("brief")
    if use_stubs:
        spend = stubs.stub_brief(
            scraped_path if scraped_path.exists() else None,
            signals_path if signals_path.exists() else None,
            out_path,
        )
    else:
        _, spend = _run_subprocess_stage(
            [
                sys.executable,
                "-m",
                "desk.brief",
                "--scraped",
                str(scraped_path),
                "--signals",
                str(signals_path),
                "--out",
                str(out_path),
            ],
            cwd=PACKAGE_ROOT,
        )
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError("brief stage reported success but wrote no output")
    return spend


def _do_serve(
    brief_path: Path, use_stubs: bool, port: int, hold_seconds: float
) -> tuple[str, float]:
    """Start `desk.serve` and return as soon as it prints a preview URL —
    NOT when it exits.

    GRE-3464 fix: real `desk.serve` prints the URL as its first line, then
    BLOCKS (holding the sandbox open for `--hold-seconds`, or until Ctrl-C)
    so the URL stays curlable. The original assumption here — that
    `desk.serve` prints the URL as its LAST stdout line and then returns —
    doesn't hold; running it through `_run_subprocess_stage` (which waits
    for exit) would hang this stage for the full hold duration instead of
    the few seconds sandbox startup actually takes.

    Fix: launch it as a live subprocess, stream its stdout on a background
    thread, and return the URL the moment a line matching it appears —
    leaving the subprocess (and the sandbox it's holding open) running
    untouched in the background after this function returns. The real
    `[spend] sandbox (preview): ...` line desk/solari_client.py emits is
    only printed when the subprocess is eventually killed (hold elapses, or
    someone Ctrl-C's it) — which happens after this function has already
    returned — so `spend_usd` here is an ESTIMATE from the requested hold
    time and the documented sandbox rate, not a measured actual. Documented
    in budget.json via the stage's `spend_estimated` extra field.
    """
    _check_injected_failure("serve")
    if use_stubs:
        return stubs.stub_serve(brief_path)

    cmd = [
        sys.executable,
        "-m",
        "desk.serve",
        "--file",
        str(brief_path),
        "--port",
        str(port),
        "--hold-seconds",
        str(hold_seconds),
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(PACKAGE_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        # Detach into its own session so it outlives this orchestrator
        # process (and the shell that launched it) rather than being sent
        # SIGHUP when the parent's controlling terminal/session goes away —
        # the whole point is that the preview stays up after this run ends.
        start_new_session=True,
    )

    lines: "queue.Queue[Optional[str]]" = queue.Queue()

    def _pump() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                lines.put(line)
        finally:
            lines.put(None)  # EOF sentinel

    threading.Thread(target=_pump, daemon=True).start()

    url: Optional[str] = None
    deadline = time.monotonic() + SERVE_URL_WAIT_S
    while url is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            line = lines.get(timeout=remaining)
        except queue.Empty:
            break
        if line is None:  # process closed stdout without printing a URL
            break
        url = _parse_preview_url(line)

    if not url:
        # Never printed a URL — the process either failed fast or is stuck.
        # Kill it (nothing worth leaving running) and surface stderr.
        proc.kill()
        try:
            _, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            stderr = ""
        raise RuntimeError(
            f"desk.serve did not print a preview URL within {SERVE_URL_WAIT_S:.0f}s "
            f"(exit={proc.poll()}): {stderr.strip()[:2000]}"
        )

    est_spend = hold_seconds / 3600 * _SANDBOX_RATE_PER_HOUR_ESTIMATE
    return url, est_spend


# --------------------------------------------------------------------------
# retry / degrade wrapper
# --------------------------------------------------------------------------


def _run_with_retry(name: str, func, retries: int) -> StageOutcome:
    attempts = 0
    last_err: Optional[str] = None
    start = time.monotonic()
    while attempts <= retries:
        attempts += 1
        try:
            result = func()
            duration = time.monotonic() - start
            logger.info("stage=%s status=ok attempts=%d duration=%.2fs", name, attempts, duration)
            return StageOutcome(
                name=name, status="ok", attempts=attempts, duration_s=duration, extra={"_result": result}
            )
        except StageSkipped as exc:
            logger.info("stage=%s status=skipped reason=%s", name, exc)
            return StageOutcome(
                name=name, status="skipped", attempts=0, duration_s=0.0, reason=str(exc)
            )
        except Exception as exc:  # noqa: BLE001 - any stage failure is retried/degraded
            last_err = str(exc)
            if attempts <= retries:
                logger.warning("stage=%s attempt=%d failed: %s — retrying", name, attempts, last_err)
            else:
                logger.warning("stage=%s attempt=%d failed: %s", name, attempts, last_err)
    duration = time.monotonic() - start
    logger.error(
        "stage=%s status=failed attempts=%d error=%s — continuing degraded",
        name,
        attempts,
        last_err,
    )
    return StageOutcome(name=name, status="failed", attempts=attempts, duration_s=duration, error=last_err)


# --------------------------------------------------------------------------
# resume / checkpoint helpers
# --------------------------------------------------------------------------


def _load_state(run_dir: Path) -> dict:
    state_path = run_dir / "state.json"
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_state(run_dir: Path, stage_outcomes: dict[str, StageOutcome], status: str) -> None:
    data = {
        "status": status,
        "stages": {name: outcome.to_dict() for name, outcome in stage_outcomes.items()},
    }
    (run_dir / "state.json").write_text(json.dumps(data, indent=2) + "\n")


def _artifact_valid(name: str, path: Path) -> bool:
    try:
        if name == "scraper":
            contracts.load_scraped(str(path))
            return True
        if name == "models":
            contracts.load_signals(str(path))
            return True
        if name == "brief":
            return path.exists() and path.stat().st_size > 0
    except Exception:
        return False
    return False


def _resumed_outcome(name: str, prev: dict) -> StageOutcome:
    return StageOutcome(
        name=name,
        status="ok",
        attempts=prev.get("attempts", 0),
        duration_s=0.0,
        spend_usd=prev.get("spend_usd", 0.0),
        extra={k: v for k, v in prev.items() if k not in {"name", "status", "attempts", "duration_s", "spend_usd", "error", "reason"}},
    )


# --------------------------------------------------------------------------
# logging setup
# --------------------------------------------------------------------------


def _configure_logging(run_dir: Path) -> logging.FileHandler:
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False

    file_handler = logging.FileHandler(run_dir / "run.log")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(stream_handler)
    return file_handler


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    use_stubs = bool(args.dry_run or args.stubs or os.environ.get("DESK_STUBS") == "1")

    if args.resume:
        run_dir = Path(args.resume)
        if not run_dir.exists():
            print(f"--resume path does not exist: {run_dir}", file=sys.stderr)
            return 2
        run_date = run_dir.name
    else:
        run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        run_dir = Path(args.out_root) / run_date
        run_dir.mkdir(parents=True, exist_ok=True)

    file_handler = _configure_logging(run_dir)
    try:
        return _run(args, run_dir, run_date, use_stubs)
    finally:
        file_handler.close()
        logger.removeHandler(file_handler)


def _run(args: argparse.Namespace, run_dir: Path, run_date: str, use_stubs: bool) -> int:
    symbols = _split_symbols(args.symbols)
    scraped_path = run_dir / "scraped_data.json"
    signals_path = run_dir / "signals.json"
    brief_path = run_dir / "brief.html"

    prev_state = _load_state(run_dir) if args.resume else {}
    prev_stages = prev_state.get("stages", {})

    logger.info(
        "run start date=%s dir=%s symbols=%s stubs=%s resume=%s",
        run_date, run_dir, symbols or "(none — resume mode)", use_stubs, bool(args.resume),
    )

    outcomes: dict[str, StageOutcome] = {}

    # ---- scraper -----------------------------------------------------
    prev = prev_stages.get("scraper")
    if prev and prev.get("status") == "ok" and _artifact_valid("scraper", scraped_path):
        logger.info("stage=scraper status=skipped reason=resumed-valid-artifact")
        outcomes["scraper"] = _resumed_outcome("scraper", prev)
    else:
        if not symbols:
            logger.error("stage=scraper has no valid checkpoint and --symbols was not given")
            outcomes["scraper"] = StageOutcome(
                name="scraper", status="failed", error="--symbols required (no checkpoint to resume)"
            )
        else:
            outcomes["scraper"] = _run_with_retry(
                "scraper", lambda: _do_scraper(symbols, scraped_path, use_stubs), args.retries
            )

    # ---- models --------------------------------------------------------
    prev = prev_stages.get("models")
    if prev and prev.get("status") == "ok" and _artifact_valid("models", signals_path):
        logger.info("stage=models status=skipped reason=resumed-valid-artifact")
        outcomes["models"] = _resumed_outcome("models", prev)
    elif not scraped_path.exists():
        logger.info("stage=models status=skipped reason=no-scraped-data-input")
        outcomes["models"] = StageOutcome(name="models", status="skipped", reason="no scraped_data input available")
    else:
        outcomes["models"] = _run_with_retry(
            "models", lambda: _do_models(scraped_path, signals_path, use_stubs), args.retries
        )

    # ---- brief -----------------------------------------------------------
    prev = prev_stages.get("brief")
    if prev and prev.get("status") == "ok" and _artifact_valid("brief", brief_path):
        logger.info("stage=brief status=skipped reason=resumed-valid-artifact")
        outcomes["brief"] = _resumed_outcome("brief", prev)
    else:
        # Always attempted, even with missing scraped/signals inputs — the
        # degrade-gracefully contract this ticket's AC-2 exercises.
        outcomes["brief"] = _run_with_retry(
            "brief", lambda: _do_brief(scraped_path, signals_path, brief_path, use_stubs), args.retries
        )

    # ---- serve -------------------------------------------------------------
    prev = prev_stages.get("serve")
    preview_url: Optional[str] = None
    if prev and prev.get("status") == "ok" and prev.get("preview_url"):
        logger.info("stage=serve status=skipped reason=resumed-valid-artifact")
        preview_url = prev.get("preview_url")
        outcomes["serve"] = _resumed_outcome("serve", prev)
    elif not brief_path.exists() or brief_path.stat().st_size == 0:
        logger.info("stage=serve status=skipped reason=no-brief-to-serve")
        outcomes["serve"] = StageOutcome(name="serve", status="skipped", reason="no brief.html to serve")
    else:
        result_holder: dict = {}

        def _serve_call():
            url, spend = _do_serve(brief_path, use_stubs, args.serve_port, args.serve_hold_seconds)
            result_holder["url"] = url
            result_holder["spend"] = spend
            return url

        outcomes["serve"] = _run_with_retry("serve", _serve_call, args.retries)
        if outcomes["serve"].status == "ok":
            preview_url = result_holder.get("url")
            outcomes["serve"].spend_usd = result_holder.get("spend", 0.0)
            outcomes["serve"].extra["preview_url"] = preview_url
            if not use_stubs:
                # GRE-3464: real serve spend is an estimate — see
                # _do_serve's docstring — the sandbox is left running past
                # this function returning, so the actual "[spend]" line
                # isn't observable synchronously here.
                outcomes["serve"].extra["spend_estimated"] = True

    # pull spend out of the "_result" extras used for scraper/models/brief
    for name in ("scraper", "models", "brief"):
        outcome = outcomes[name]
        if outcome.status == "ok" and "_result" in outcome.extra:
            outcome.spend_usd = outcome.extra.pop("_result") or 0.0

    # ---- status -------------------------------------------------------------
    brief_ok = brief_path.exists() and brief_path.stat().st_size > 0
    if not brief_ok:
        status = "failed"
    elif all(outcomes[n].status == "ok" for n in STAGE_NAMES):
        status = "full"
    else:
        status = "partial"

    # ---- budget ---------------------------------------------------------
    budget = {
        "run_date": run_date,
        "stages": {name: round(outcomes[name].spend_usd, 6) for name in STAGE_NAMES},
        "total_usd": round(sum(outcomes[name].spend_usd for name in STAGE_NAMES), 6),
    }
    if outcomes["serve"].extra.get("spend_estimated"):
        budget["notes"] = [
            "serve stage spend is an ESTIMATE (hold_seconds * sandbox rate), not a "
            "measured actual — the sandbox is left running past this run finishing "
            "(GRE-3464); see README 'Orchestrator <-> serve reconciliation'."
        ]
    (run_dir / "budget.json").write_text(json.dumps(budget, indent=2) + "\n")

    _write_state(run_dir, outcomes, status)

    # ---- publish ---------------------------------------------------------
    docs_out = Path(args.docs_out) if args.docs_out else PACKAGE_ROOT / "docs" / "latest" / "index.html"
    if brief_ok:
        docs_out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(brief_path, docs_out)
        logger.info("published brief.html -> %s", docs_out)

    # ---- summary ---------------------------------------------------------
    logger.info("run status=%s", status)
    print(f"\n=== overnight desk run {run_date} — status: {status} ===")
    for name in STAGE_NAMES:
        o = outcomes[name]
        line = f"  {name:8s} {o.status:8s} attempts={o.attempts} duration={o.duration_s:.2f}s spend=${o.spend_usd:.4f}"
        if o.error:
            line += f" error={o.error}"
        if o.reason:
            line += f" reason={o.reason}"
        print(line)
    print(f"  budget total: ${budget['total_usd']:.4f}")
    if preview_url:
        print(f"  preview: {preview_url}")
    print(f"  brief: {docs_out if brief_ok else '(not produced)'}")
    print(f"  run dir: {run_dir}")

    return 0 if status in ("full", "partial") else 1


if __name__ == "__main__":
    sys.exit(main())
