import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = PACKAGE_ROOT / "fixtures"

# The repo root .env (two levels above examples/overnight-options-desk) holds
# SOLARI_API_KEY for live runs.
load_dotenv(PACKAGE_ROOT.parent.parent / ".env")


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture(scope="session", autouse=True)
def _require_key_for_live(request):
    """Fail live tests fast with a clear message instead of an opaque 401 if
    SOLARI_API_KEY isn't loaded."""
    if not os.environ.get("SOLARI_API_KEY") and _selected_live(request):
        pytest.exit(
            "SOLARI_API_KEY is not set — `set -a; source .env; set +a` from "
            "the repo root before running -m live.",
            returncode=1,
        )


def _selected_live(request) -> bool:
    expr = request.config.getoption("-m", default="")
    # Only hard-fail when the caller actually asked for live tests; a plain
    # `pytest tests/` (no -m) should still collect everything without a key
    # present, and `-m "not live"` must never require one.
    return expr.strip() == "live"
