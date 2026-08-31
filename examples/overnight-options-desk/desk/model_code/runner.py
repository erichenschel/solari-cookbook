"""runner.py — subprocess entry point invoked by `desk/models.py`'s
bootstrap code as a BRAND-NEW interpreter process, not the sandbox kernel's
long-lived one.

SANDBOX FINDING (GRE-3461 live testing): the sandbox's Python kernel
(the persistent process `run_in_sandbox`'s `run_code` executes in) already
has `numpy` imported at kernel startup. `pip install`-ing a *different*
numpy afterward only updates what's on disk — the kernel process keeps the
old numpy object bound in `sys.modules`. `pandas`'s compiled `_libs`
extensions are linked against a specific numpy ABI and check it at import
time; when the in-process numpy doesn't match what's on disk, that check
fails with a cryptic `ImportError: C extension: None not built` deep
inside `arch`'s GARCH fit / `statsmodels`' OLS — not at install time, and
not in any way traceable to "wrong numpy version" from the error text
alone. Running this file via `python runner.py ...` in a fresh subprocess
(after `pip install`) sidesteps the whole problem: a new process imports
numpy/scipy/pandas fresh from disk, all mutually consistent.

Flat module (imports `driver`, itself flat) — uploaded alongside
prices.py/signals.py/fetch.py/driver.py and invoked as:

    python runner.py <model_code_dir> <scraped_data.json path> <out signals.json path>
"""

from __future__ import annotations

import json
import sys


def main() -> None:
    model_code_dir, scraped_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    sys.path.insert(0, model_code_dir)
    import driver  # flat import, sandbox-side; deferred until sys.path is set

    with open(scraped_path, "r", encoding="utf-8") as fh:
        scraped_data = json.load(fh)

    result = driver.build_signals(scraped_data)

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh)


if __name__ == "__main__":
    main()
