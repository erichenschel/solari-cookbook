"""solari_client.py — thin wrapper over the Solari SDKs for the desk pipeline.

Every helper here is a *complete* unit of work: it creates exactly the
resource it needs, uses it, and kills it before returning — mirroring the
gotchas called out in the cookbook root README (browser `await
browser.close()` + `await solari.close()`; sandbox `kill()` not `close()`;
commands are argv, not shell). `serve_preview` is the one exception: a
preview has to keep running to be curled from outside the VM, so it hands
back a `PreviewHandle` with an explicit `.kill()` instead of closing itself.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from solari_browser import Solari
from solari_sandbox import SandboxClient

logger = logging.getLogger("desk.solari_client")

# The standalone SandboxClient (unlike the umbrella @solarisdk/sdk client)
# requires base_url explicitly — see sandbox-code-interpreter-py.
SANDBOX_BASE_URL = "https://api.getsolari.com"

# Free-tier list price, used only to print an estimated-spend line per call.
# Not billing-accurate — just enough to keep a live run inside the $3 budget
# honest without needing an account API call.
BROWSER_RATE_PER_HOUR = 0.15
SANDBOX_RATE_PER_HOUR = 0.10


def _api_key() -> str:
    load_dotenv()
    key = os.environ.get("SOLARI_API_KEY")
    if not key:
        raise RuntimeError(
            "SOLARI_API_KEY is not set. `set -a; source .env; set +a` from the "
            "repo root, or export it directly."
        )
    return key


def _log_spend(resource: str, seconds: float, rate_per_hour: float) -> None:
    estimate = seconds / 3600 * rate_per_hour
    msg = f"[spend] {resource}: {seconds:.1f}s ~= ${estimate:.4f} (@ ${rate_per_hour:.2f}/hr)"
    logger.info(msg)
    print(msg)


@dataclass
class PageResult:
    """Everything a scraper lane needs from one page load."""

    title: str
    text: str
    html: str
    session_id: str
    replay_hint: str


async def open_browser_page(url: str, *, recording: bool = False) -> PageResult:
    """Open one page in a fresh cloud browser, read it, and release the
    session.

    `recording=True` opts the session into rrweb capture (per the root
    README gotcha: it is per-session, not account-level, and the replay
    upload happens async after release — poll ~30s before concluding there
    is no replay).
    """
    solari = Solari(api_key=_api_key())
    started = time.monotonic()
    browser = await solari.launch(recording=recording)
    try:
        page = await browser.new_page()
        await page.goto(url)
        title = await page.title()
        html = await page.content()
        text = await page.inner_text("body")
        session_id = browser.id
    finally:
        # browser.close() closes the tab AND releases the session — closing
        # only the tab would leave the slot held until the plan deadline.
        await browser.close()
        # The TS root-README gotcha ("call await solari.close() or the
        # process hangs on the loopback retry proxy") has a Python
        # equivalent: Solari.close() stops the httpx client + patchright
        # driver. It's easy to miss because the Python cookbook examples
        # don't call it explicitly (they exit right after), but a
        # long-lived process like a test suite should.
        await solari.close()
        _log_spend("browser", time.monotonic() - started, BROWSER_RATE_PER_HOUR)

    replay_hint = (
        f"recording=True: poll solari.sessions.download_replay({session_id!r}) "
        "for ~30s after release; the rrweb upload happens async."
        if recording
        else "recording=False: no replay was captured for this session."
    )
    return PageResult(
        title=title, text=text, html=html, session_id=session_id, replay_hint=replay_hint
    )


@dataclass
class ExecResult:
    """Result of one sandbox code-interpreter run."""

    stdout: str
    stderr: str
    error: Optional[str]
    result: Optional[str]
    artifacts: list = field(default_factory=list)


async def run_in_sandbox(
    code: Optional[str] = None,
    files: Optional[dict[str, str]] = None,
    *,
    template: str = "base",
    timeout_ms: int = 5 * 60_000,
) -> ExecResult:
    """Run code in a fresh sandbox's stateful Python kernel, optionally
    seeding it with files first.

    `files` maps in-guest path -> text/bytes content, written before `code`
    runs. The VM is always killed before returning — `kill()`, not
    `close()`: `close()` only drops the local control channel and the VM
    would linger until its idle timeout.
    """
    async with SandboxClient(api_key=_api_key(), base_url=SANDBOX_BASE_URL) as client:
        sandbox = await client.create(template=template, timeout_ms=timeout_ms)
        started = time.monotonic()
        try:
            await sandbox.connect()

            if files:
                for path, content in files.items():
                    await sandbox.files.write(path, content)

            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []
            error: Optional[str] = None
            result_text: Optional[str] = None
            artifacts: list = []

            if code:
                ctx = await sandbox.create_code_context("python")
                run = await sandbox.run_code(code, context_id=ctx)
                error = run.error
                for item in run.results:
                    if item.type == "stdout" and item.text:
                        stdout_chunks.append(item.text)
                    elif item.type == "stderr" and item.text:
                        stderr_chunks.append(item.text)
                    elif item.type == "result" and item.text:
                        result_text = item.text
                    elif item.text or item.png or item.svg or item.html:
                        artifacts.append({"type": item.type, "text": item.text})

            return ExecResult(
                stdout="".join(stdout_chunks),
                stderr="".join(stderr_chunks),
                error=error,
                result=result_text,
                artifacts=artifacts,
            )
        finally:
            await sandbox.kill()
            _log_spend("sandbox", time.monotonic() - started, SANDBOX_RATE_PER_HOUR)


@dataclass
class PreviewHandle:
    """A live port preview. Stays up until `.kill()` — this is the one
    helper that does NOT clean itself up, because the whole point is a URL
    that's still reachable after the caller gets it back."""

    url: str
    sandbox_id: str
    _sandbox: Any
    _client: Any
    _started: float

    async def kill(self) -> None:
        try:
            await self._sandbox.kill()
        finally:
            await self._client.aclose()
            _log_spend(
                "sandbox (preview)", time.monotonic() - self._started, SANDBOX_RATE_PER_HOUR
            )

    async def __aenter__(self) -> "PreviewHandle":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.kill()


async def serve_preview(
    directory: str, port: int = 8000, *, timeout_ms: int = 5 * 60_000
) -> PreviewHandle:
    """Upload `directory`'s files into a fresh sandbox and serve them on
    `port` behind a public `*.preview.getsolari.com` URL.

    `commands.run` waits for the process to exit by default, so the server
    is started with `background=True` — the Python SDK's direct equivalent
    of the `nohup ... &` shell trick the TS port-preview example needs
    (Python's `commands.run` takes the flag natively; TS's does not).

    `timeout_ms` is the sandbox VM's own idle-kill window (GRE-3464): the
    default 5 minutes is fine for a quick smoke test but far too short for
    `desk/serve.py --hold-seconds N` to hold a preview open for N seconds —
    the VM would die out from under the still-running http.server before
    the caller's hold elapses. Callers that want the preview reachable for
    longer must pass a `timeout_ms` that covers their intended hold.
    """
    client = SandboxClient(api_key=_api_key(), base_url=SANDBOX_BASE_URL)
    sandbox = await client.create(template="base", timeout_ms=timeout_ms)
    started = time.monotonic()
    try:
        await sandbox.connect()

        remote_dir = "/tmp/site"
        local_dir = Path(directory)
        for local_path in sorted(local_dir.rglob("*")):
            if local_path.is_file():
                rel = local_path.relative_to(local_dir)
                await sandbox.files.write(
                    f"{remote_dir}/{rel.as_posix()}", local_path.read_bytes()
                )

        # Commands are argv, not shell-interpreted — no `sh -c` needed here,
        # just `background=True` so `run()` doesn't block on the server.
        await sandbox.commands.run(
            "python3",
            args=["-m", "http.server", str(port), "--directory", remote_dir],
            background=True,
        )

        preview = await sandbox.preview_url(port)
        url = preview["url"]
        return PreviewHandle(
            url=url,
            sandbox_id=sandbox.sandboxId,
            _sandbox=sandbox,
            _client=client,
            _started=started,
        )
    except Exception:
        await sandbox.kill()
        await client.aclose()
        raise
