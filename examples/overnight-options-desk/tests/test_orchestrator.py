"""Hermetic tests for desk/run_overnight.py — sequencing, checkpointing,
partial-failure degrade, and budget accounting. No network, no key needed.

    pytest examples/overnight-options-desk/tests -m "not live" -q
"""

from __future__ import annotations

import json

import pytest

from desk import run_overnight

pytestmark = pytest.mark.filterwarnings("ignore")


def _run(tmp_path, **extra_env):
    """Invoke main() in-process against tmp_path, returning (exit_code, run_dir)."""
    out_root = tmp_path / "runs"
    docs_out = tmp_path / "docs" / "latest" / "index.html"
    argv = [
        "--dry-run",
        "--symbols",
        "AAPL,NVDA",
        "--out-root",
        str(out_root),
        "--docs-out",
        str(docs_out),
    ]
    code = run_overnight.main(argv)
    run_dirs = list(out_root.glob("*"))
    assert len(run_dirs) == 1, f"expected exactly one run dir, found {run_dirs}"
    return code, run_dirs[0], docs_out


def test_dry_run_writes_all_artifacts_and_is_hermetic(tmp_path, monkeypatch):
    monkeypatch.delenv("DESK_FAIL_STAGE", raising=False)
    monkeypatch.delenv("SOLARI_API_KEY", raising=False)  # prove no key is needed

    code, run_dir, docs_out = _run(tmp_path)

    assert code == 0
    for fname in ("scraped_data.json", "signals.json", "brief.html", "run.log", "budget.json"):
        p = run_dir / fname
        assert p.exists(), f"missing {fname}"
        assert p.stat().st_size > 0, f"{fname} is empty"

    state = json.loads((run_dir / "state.json").read_text())
    assert state["status"] == "full"
    for name in ("scraper", "models", "brief", "serve"):
        assert state["stages"][name]["status"] == "ok"

    # published copy
    assert docs_out.exists()
    assert docs_out.read_text() == (run_dir / "brief.html").read_text()

    # contract-valid outputs
    scraped = json.loads((run_dir / "scraped_data.json").read_text())
    assert scraped["universe"] == ["AAPL", "NVDA"]
    signals = json.loads((run_dir / "signals.json").read_text())
    assert set(signals["per_symbol"]) == {"AAPL", "NVDA"}


def test_dry_run_sequencing_order_in_log(tmp_path):
    _, run_dir, _ = _run(tmp_path)
    log = (run_dir / "run.log").read_text()
    order = [name for name in ("scraper", "models", "brief", "serve") if f"stage={name}" in log]
    idx = {name: log.index(f"stage={name}") for name in order}
    assert idx["scraper"] < idx["models"] < idx["brief"] < idx["serve"]


def test_injected_models_failure_degrades_to_partial(tmp_path, monkeypatch):
    monkeypatch.setenv("DESK_FAIL_STAGE", "models")
    code, run_dir, docs_out = _run(tmp_path)
    monkeypatch.delenv("DESK_FAIL_STAGE", raising=False)

    assert code == 0  # partial is still a successful exit
    state = json.loads((run_dir / "state.json").read_text())
    assert state["status"] == "partial"
    assert state["stages"]["scraper"]["status"] == "ok"
    assert state["stages"]["models"]["status"] == "failed"
    assert state["stages"]["models"]["attempts"] == 2  # first try + one retry
    assert state["stages"]["brief"]["status"] == "ok"
    assert state["stages"]["serve"]["status"] == "ok"

    # signals.json was never produced
    assert not (run_dir / "signals.json").exists()

    # brief still rendered, degraded, and got published
    brief_html = (run_dir / "brief.html").read_text()
    assert "AAPL" in brief_html
    assert "signals unavailable" in brief_html
    assert docs_out.exists()

    log = (run_dir / "run.log").read_text()
    assert "stage=models attempt=1 failed" in log
    assert "retrying" in log
    assert "stage=models status=failed" in log
    assert "continuing degraded" in log


def test_injected_scraper_failure_skips_models_but_brief_still_renders(tmp_path, monkeypatch):
    monkeypatch.setenv("DESK_FAIL_STAGE", "scraper")
    code, run_dir, docs_out = _run(tmp_path)
    monkeypatch.delenv("DESK_FAIL_STAGE", raising=False)

    assert code == 0
    state = json.loads((run_dir / "state.json").read_text())
    assert state["status"] == "partial"
    assert state["stages"]["scraper"]["status"] == "failed"
    assert state["stages"]["models"]["status"] == "skipped"
    assert state["stages"]["models"]["reason"] == "no scraped_data input available"
    assert state["stages"]["brief"]["status"] == "ok"

    assert not (run_dir / "scraped_data.json").exists()
    assert not (run_dir / "signals.json").exists()
    brief_html = (run_dir / "brief.html").read_text()
    assert "scraped_data unavailable" in brief_html
    assert "signals unavailable" in brief_html
    assert docs_out.exists()


def test_injected_brief_failure_yields_failed_run_and_skips_serve(tmp_path, monkeypatch):
    monkeypatch.setenv("DESK_FAIL_STAGE", "brief")
    code, run_dir, docs_out = _run(tmp_path)
    monkeypatch.delenv("DESK_FAIL_STAGE", raising=False)

    assert code == 1
    state = json.loads((run_dir / "state.json").read_text())
    assert state["status"] == "failed"
    assert state["stages"]["brief"]["status"] == "failed"
    assert state["stages"]["serve"]["status"] == "skipped"
    assert not (run_dir / "brief.html").exists()
    assert not docs_out.exists()


def test_budget_json_structure(tmp_path):
    _, run_dir, _ = _run(tmp_path)
    budget = json.loads((run_dir / "budget.json").read_text())
    assert set(budget) == {"run_date", "stages", "total_usd"}
    assert set(budget["stages"]) == {"scraper", "models", "brief", "serve"}
    assert all(isinstance(v, (int, float)) for v in budget["stages"].values())
    assert budget["total_usd"] == pytest.approx(sum(budget["stages"].values()))
    # dry-run makes zero real API calls -> zero spend
    assert budget["total_usd"] == 0.0


def test_resume_skips_completed_stages(tmp_path, monkeypatch):
    out_root = tmp_path / "runs"
    docs_out = tmp_path / "docs" / "latest" / "index.html"
    code = run_overnight.main(
        ["--dry-run", "--symbols", "AAPL,NVDA", "--out-root", str(out_root), "--docs-out", str(docs_out)]
    )
    assert code == 0
    run_dir = next(out_root.glob("*"))
    scraped_mtime = (run_dir / "scraped_data.json").stat().st_mtime_ns
    signals_mtime = (run_dir / "signals.json").stat().st_mtime_ns
    brief_mtime = (run_dir / "brief.html").stat().st_mtime_ns

    calls = {"scraper": 0, "models": 0, "brief": 0}

    def _fail(name):
        def _inner(*a, **kw):
            calls[name] += 1
            raise AssertionError(f"stub_{name} should not be called on resume")

        return _inner

    monkeypatch.setattr("desk.stubs.stub_scrape", _fail("scraper"))
    monkeypatch.setattr("desk.stubs.stub_models", _fail("models"))
    monkeypatch.setattr("desk.stubs.stub_brief", _fail("brief"))

    code2 = run_overnight.main(["--resume", str(run_dir), "--dry-run", "--docs-out", str(docs_out)])

    assert code2 == 0
    assert calls == {"scraper": 0, "models": 0, "brief": 0}
    assert (run_dir / "scraped_data.json").stat().st_mtime_ns == scraped_mtime
    assert (run_dir / "signals.json").stat().st_mtime_ns == signals_mtime
    assert (run_dir / "brief.html").stat().st_mtime_ns == brief_mtime

    state = json.loads((run_dir / "state.json").read_text())
    assert state["status"] == "full"


def test_resume_reruns_only_the_previously_failed_stage(tmp_path, monkeypatch):
    out_root = tmp_path / "runs"
    docs_out = tmp_path / "docs" / "latest" / "index.html"

    monkeypatch.setenv("DESK_FAIL_STAGE", "models")
    code = run_overnight.main(
        ["--dry-run", "--symbols", "AAPL,NVDA", "--out-root", str(out_root), "--docs-out", str(docs_out)]
    )
    monkeypatch.delenv("DESK_FAIL_STAGE", raising=False)
    assert code == 0
    run_dir = next(out_root.glob("*"))
    state = json.loads((run_dir / "state.json").read_text())
    assert state["status"] == "partial"
    assert not (run_dir / "signals.json").exists()

    # models now succeeds on resume (no DESK_FAIL_STAGE this time)
    code2 = run_overnight.main(["--resume", str(run_dir), "--dry-run", "--docs-out", str(docs_out)])
    assert code2 == 0
    state2 = json.loads((run_dir / "state.json").read_text())
    assert state2["status"] == "full"
    assert (run_dir / "signals.json").exists()
