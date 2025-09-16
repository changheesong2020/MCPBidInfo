from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the package root is importable when tests are executed from the project directory.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--runlive",
        action="store_true",
        default=False,
        help="run tests that call the live TED API",
    )


@pytest.fixture
def runlive(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--runlive"))
