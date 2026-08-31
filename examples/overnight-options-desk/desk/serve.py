"""desk/serve.py — publish a local file on a Solari sandbox port-preview
URL. This is the ONLY module in the brief lane that touches the Solari API
(desk/brief.py stays pure per NG-3) — it just stages the file into a temp
directory and hands it to `desk.solari_client.serve_preview`.

Usage:
    python -m desk.serve --file /tmp/brief.html
    # prints the public *.preview.getsolari.com URL, then blocks so the
    # sandbox stays reachable; curl it from another shell, then Ctrl-C to
    # release it (or pass --hold-seconds N for a bounded, scriptable run).
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional

from desk.solari_client import serve_preview

# GRE-3464: observed free-tier sandbox preview lifetime — see the root
# README limitations / this package's README "Live sandbox findings". A
# --hold-seconds beyond this is silently clamped for the sandbox's own
# timeout_ms (the process itself still sleeps for the full --hold-seconds
# requested, but the VM — and therefore the preview URL — will already be
# dead by then).
MAX_HOLD_S = 3600.0


async def _serve_file(file_path: str, port: int, hold_seconds: Optional[float]) -> None:
    src = Path(file_path)
    if not src.is_file():
        raise FileNotFoundError(f"not a file: {file_path}")

    if hold_seconds is not None and hold_seconds > MAX_HOLD_S:
        print(
            f"[warn] --hold-seconds {hold_seconds:.0f} exceeds the sandbox's "
            f"~{MAX_HOLD_S:.0f}s free-tier preview lifetime cap; the preview "
            f"will die at ~{MAX_HOLD_S:.0f}s regardless of the longer hold "
            "requested here.",
            file=sys.stderr,
        )

    # The sandbox VM's own idle-kill window has to cover the requested hold
    # (see solari_client.serve_preview's timeout_ms docstring) or it dies
    # under the still-running http.server before --hold-seconds elapses.
    # Capped at MAX_HOLD_S either way (GRE-3464), plus a small setup buffer.
    sandbox_hold_s = min(hold_seconds if hold_seconds is not None else MAX_HOLD_S, MAX_HOLD_S)
    timeout_ms = int(sandbox_hold_s * 1000) + 30_000

    with tempfile.TemporaryDirectory(prefix="desk-serve-") as tmp:
        tmp_dir = Path(tmp)
        # http.server maps "/" -> index.html; stage the file under that name
        # (unless it's already named index.html) so the bare preview URL
        # serves it directly instead of a directory listing.
        dest_name = "index.html" if src.suffix.lower() in (".html", ".htm") else src.name
        dest = tmp_dir / dest_name
        shutil.copy2(src, dest)

        handle = await serve_preview(str(tmp_dir), port=port, timeout_ms=timeout_ms)
        try:
            print(handle.url, flush=True)
            if hold_seconds is not None:
                await asyncio.sleep(hold_seconds)
            else:
                # Block until interrupted (Ctrl-C) rather than a fixed
                # duration — free tier is one concurrent sandbox, so the
                # operator releasing it as soon as they're done curling
                # matters more than a generous default timeout.
                await asyncio.Event().wait()
        finally:
            await handle.kill()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Publish a local file on a Solari sandbox port-preview URL."
    )
    parser.add_argument("--file", required=True, help="Local file to publish.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--hold-seconds",
        type=float,
        default=None,
        help=(
            "Kill the sandbox and exit automatically after N seconds "
            "(default: hold until Ctrl-C)."
        ),
    )
    args = parser.parse_args(argv)

    try:
        asyncio.run(_serve_file(args.file, args.port, args.hold_seconds))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
