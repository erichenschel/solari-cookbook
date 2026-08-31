"""desk.model_code — the quant model code that runs INSIDE the Solari sandbox
kernel (via `desk.solari_client.run_in_sandbox`) and is also imported
directly, locally, by the hermetic test suite.

Split by dependency shape so the local/sandbox boundary is explicit:
  prices.py   pure parsing (Stooq CSV, Yahoo chart JSON) -> list[float] closes
  signals.py  pure numeric models (GARCH, OU/AR(1), momentum, verdict rules)
  fetch.py    network I/O (Stooq primary, Yahoo fallback) -- sandbox-only,
              not exercised by hermetic tests
  driver.py   sandbox-side glue: fetch + signals -> full signals.json body;
              flat-imported (`import fetch, signals`) once uploaded next to
              each other in the sandbox, since the sandbox kernel has no
              `desk` package on its path.

`prices.py` and `signals.py` have zero solari/sandbox imports, so the exact
same file content is valid whether it's imported locally as
`desk.model_code.<name>` (tests) or uploaded flat into the sandbox and
imported as top-level `<name>` (driver.py, sandbox side).
"""
