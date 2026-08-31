"""Live smoke tests against the real free-tier Solari API.

Run sequentially (`-p no:randomly`-safe by construction — no parallel
fixtures) and each budgeted well under 2 minutes / a few cents. Every
resource is killed in a `finally` (or by the `desk.solari_client` helper
itself). See the root README "Gotchas" and desk/solari_client.py for the
reasoning behind each cleanup step.

    pytest examples/overnight-options-desk/tests -m live -q
"""

import asyncio
import os
import time

import httpx
import pytest

from desk.solari_client import open_browser_page, run_in_sandbox, serve_preview
from solari_browser import Solari
from solari_browser.errors import SolariError

pytestmark = pytest.mark.live


async def test_browser_open_and_read_example_com():
    result = await open_browser_page("https://example.com")
    assert result.title == "Example Domain"
    assert "Example Domain" in result.text
    assert "<h1>" in result.html
    assert result.session_id


async def test_sandbox_kernel_arithmetic_and_numpy_import():
    # `base` isn't guaranteed to ship numpy, so install it inside the same
    # kernel run via subprocess before importing — one round trip, still
    # well under the 2-minute budget.
    code = (
        "import subprocess, sys\n"
        "subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', 'numpy'])\n"
        "import numpy as np\n"
        "print('sum:', 1 + 1)\n"
        "print('numpy version:', np.__version__)\n"
        "np.array([1, 2, 3]).sum()\n"
    )
    result = await run_in_sandbox(code=code, timeout_ms=90_000)
    assert result.error is None, f"sandbox kernel error: {result.error}"
    assert "sum: 2" in result.stdout
    assert "numpy version" in result.stdout


async def test_port_preview_serves_static_file_and_is_publicly_reachable(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<h1>desk spike preview</h1>\n")

    handle = await serve_preview(str(site), port=8000)
    try:
        # Give the backgrounded http.server a moment to bind before the
        # first fetch, then retry briefly — mirrors sandbox-port-preview-ts.
        last_status = None
        async with httpx.AsyncClient(timeout=10) as client:
            for _ in range(10):
                try:
                    res = await client.get(handle.url)
                    last_status = res.status_code
                    if res.status_code == 200:
                        assert "desk spike preview" in res.text
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1)
            else:
                pytest.fail(f"preview never returned 200 (last status: {last_status})")
    finally:
        await handle.kill()


async def test_recording_availability_probe():
    """Create a recording=True session, release it, and report whether a
    replay is retrievable on the free tier. This test PASSES either way —
    it records the finding, it does not assert availability (ticket spec)."""
    result = await open_browser_page("https://example.com", recording=True)
    session_id = result.session_id

    solari = Solari(api_key=os.environ["SOLARI_API_KEY"])
    replay_available = False
    detail = ""
    try:
        started = time.monotonic()
        for attempt in range(1, 11):
            await asyncio.sleep(3)
            try:
                blob = await solari.sessions.download_replay(session_id)
            except SolariError as err:
                if err.status == 404:
                    continue
                detail = f"error (not 404): {err}"
                break
            replay_available = True
            detail = f"{len(blob)} bytes after {time.monotonic() - started:.1f}s (attempt {attempt})"
            break
        else:
            detail = "no replay after ~30s of polling"
    finally:
        await solari.close()

    finding = (
        f"[finding] recording probe: replay_available={replay_available} ({detail})"
    )
    print(finding)
    # Never assert on replay_available — free-tier availability is the thing
    # being probed, not a requirement.
    assert True
