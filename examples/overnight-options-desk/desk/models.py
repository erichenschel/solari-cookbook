"""models.py — local CLI/driver for the quant models lane (GRE-3461).

Reads a `scraped_data.json` (validated against the shared contract), runs
the model code in `desk/model_code/` inside a single Solari sandbox session
via `desk.solari_client.run_in_sandbox`, and writes a schema-valid
`signals.json`.

    python -m desk.models --scraped fixtures/scraped_data.json --out /tmp/signals.json

Everything that touches prices or fits models lives in `desk/model_code/`
so it can be uploaded into the sandbox verbatim; this file is just the local
orchestrator — it never does math itself.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from desk.contracts import validate_signals, load_scraped
from desk.solari_client import run_in_sandbox

logger = logging.getLogger("desk.models")

MODEL_CODE_DIR = Path(__file__).parent / "model_code"
SANDBOX_MODEL_CODE_DIR = "/home/user/desk_model_code"

# Uploaded in this order; driver.py's `import fetch, signals` and fetch.py's
# `from prices import ...` only need prices/signals/fetch present alongside
# it, order doesn't matter for correctness but keeps the upload deterministic.
# runner.py is the subprocess entry point that actually imports driver (see
# `_bootstrap_code` docstring for why it must run in a fresh process).
MODEL_CODE_FILES = ["prices.py", "signals.py", "fetch.py", "driver.py", "runner.py"]

# Sandbox pip installs -q the same way the GRE-3459 spike proved `numpy`
# resolves in seconds on the `base` template kernel; installed defensively
# every run rather than assuming the image has them.
#
# Deliberately NOT listing `numpy` here: live testing (GRE-3461) found that
# installing it explicitly pulls the newest numpy (2.x) ahead of `arch`
# and `statsmodels`, which pip then installs on top of — but the `base`
# image's preinstalled `scipy` is pinned to `numpy<1.27`, so the newer
# numpy silently breaks scipy/pandas' compiled C-extension ABI (surfaces
# as a cryptic "C extension: None not built" ImportError deep inside
# arch's GARCH fit / statsmodels' OLS, not as an install-time failure).
# Letting pip's resolver pull numpy in transitively as arch/statsmodels's
# own dependency keeps the whole numpy/scipy/pandas stack mutually
# compatible (observed: numpy 1.26.4, scipy 1.10.1, pandas 3.0.5 — GARCH
# fit verified working with that combination).
SANDBOX_PACKAGES = ["arch", "statsmodels"]

RESULT_START_MARKER = "===DESK_SIGNALS_JSON_START==="
RESULT_END_MARKER = "===DESK_SIGNALS_JSON_END==="

# NG-3: model run < 5 min for 5 symbols, one sandbox session.
SANDBOX_TIMEOUT_MS = 4 * 60_000 + 30_000  # 4m30s


def _load_model_code_files() -> dict[str, str]:
    files: dict[str, str] = {}
    for name in MODEL_CODE_FILES:
        path = MODEL_CODE_DIR / name
        if not path.exists():
            raise FileNotFoundError(f"expected model code file missing: {path}")
        files[f"{SANDBOX_MODEL_CODE_DIR}/{name}"] = path.read_text()
    return files


def _bootstrap_code(scraped_dict: dict) -> str:
    """Build the code string run in the sandbox kernel: install deps, then
    run `runner.py` (which imports `driver.build_signals`) in a FRESH
    subprocess rather than importing `driver` directly in the kernel's own
    process.

    Why a subprocess: live testing (GRE-3461) found the sandbox kernel
    process already has numpy imported at kernel startup; pip-installing a
    different numpy/scipy/pandas combination afterward only changes what's
    on disk; the kernel keeps the stale in-process numpy bound in
    `sys.modules`, and pandas' compiled C-extensions (checked against
    whatever numpy is actually imported) then fail with a cryptic
    `ImportError: C extension: None not built` deep inside arch's GARCH fit
    / statsmodels' OLS. A brand-new subprocess imports everything fresh
    from disk with no stale in-process state — see runner.py's docstring
    for the full writeup. `driver`'s own JSON output goes to a file (not
    stdout) so pip/subprocess chatter can never corrupt it; only the
    markers below are guaranteed clean on the kernel's own stdout.
    """
    scraped_json = json.dumps(scraped_dict)
    packages = " ".join(SANDBOX_PACKAGES)
    return f"""
import json, subprocess, sys
subprocess.check_call([sys.executable, "-m", "pip", "install", "-q"] + {packages!r}.split())

scraped_path = "/tmp/desk_scraped_data.json"
out_path = "/tmp/desk_signals.json"
with open(scraped_path, "w") as fh:
    fh.write({scraped_json!r})

proc = subprocess.run(
    [
        sys.executable,
        {SANDBOX_MODEL_CODE_DIR!r} + "/runner.py",
        {SANDBOX_MODEL_CODE_DIR!r},
        scraped_path,
        out_path,
    ],
    capture_output=True,
    text=True,
)
if proc.stdout:
    print(proc.stdout)
if proc.returncode != 0:
    print(proc.stderr, file=sys.stderr)
    raise RuntimeError(f"runner.py subprocess failed (exit {{proc.returncode}}): {{proc.stderr}}")

with open(out_path) as fh:
    result_json = fh.read()

print({RESULT_START_MARKER!r})
print(result_json)
print({RESULT_END_MARKER!r})
"""


def _extract_signals_json(stdout: str) -> dict:
    start = stdout.find(RESULT_START_MARKER)
    end = stdout.find(RESULT_END_MARKER)
    if start == -1 or end == -1 or end < start:
        raise RuntimeError(
            "could not find signals JSON markers in sandbox stdout; full "
            f"stdout follows:\n{stdout}"
        )
    payload = stdout[start + len(RESULT_START_MARKER) : end].strip()
    return json.loads(payload)


async def run_models(scraped_path: str) -> dict:
    """Load + validate scraped_data, run the model code in one sandbox
    session, and return a validated signals dict. Raises on any failure
    (schema-invalid output, sandbox error, missing markers) — callers should
    not treat a non-raising return as anything but success."""
    scraped = load_scraped(scraped_path)  # raises jsonschema.ValidationError if bad
    scraped_dict = scraped.to_dict()

    files = _load_model_code_files()
    code = _bootstrap_code(scraped_dict)

    result = await run_in_sandbox(code=code, files=files, timeout_ms=SANDBOX_TIMEOUT_MS)

    if result.error:
        raise RuntimeError(f"sandbox kernel error running model code: {result.error}\nstderr: {result.stderr}")

    signals_dict = _extract_signals_json(result.stdout)
    validate_signals(signals_dict)  # raises jsonschema.ValidationError if bad

    missing = set(scraped_dict["universe"]) - set(signals_dict["per_symbol"])
    if missing:
        raise RuntimeError(f"signals output is missing symbols from the universe: {sorted(missing)}")

    return signals_dict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scraped", required=True, help="path to a scraped_data.json")
    parser.add_argument("--out", required=True, help="path to write signals.json")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    signals_dict = asyncio.run(run_models(args.scraped))

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(signals_dict, indent=2) + "\n")

    n = len(signals_dict["per_symbol"])
    print(f"wrote {n} symbol signal(s) to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
