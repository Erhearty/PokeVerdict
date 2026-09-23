"""
Bridges the script-style check() harness in tools/test_*.py to pytest.

Each test module records failures by appending labels to a module-level
``FAILURES`` (or ``failures``) list instead of raising. The hook below turns
any labels appended during a test function into a pytest failure, and the
``gd`` fixture supplies the GameData those functions take as an argument.

    python3 -m pytest
"""
from __future__ import annotations

import os

import pytest

import solver_ref

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "gamedata.sqlite")


@pytest.fixture(scope="session")
def gd() -> solver_ref.GameData:
    """Shared GameData for the session; skips when gamedata.sqlite is absent."""
    if not os.path.exists(DB) or os.path.getsize(DB) == 0:
        pytest.skip("gamedata.sqlite not built - run python3 tools/build_gamedata.py --fetch")
    return solver_ref.GameData(DB)


def _failure_list(module: object) -> list | None:
    """Return the module's FAILURES/failures list, or None if it has neither."""
    for attr in ("FAILURES", "failures"):
        found = getattr(module, attr, None)
        if isinstance(found, list):
            return found
    return None


@pytest.hookimpl(wrapper=True)
def pytest_pyfunc_call(pyfuncitem):
    """Fail the test if check() recorded new failures while it ran."""
    failures = _failure_list(pyfuncitem.module)
    if failures is None:
        return (yield)
    before = len(failures)
    res = yield
    new = failures[before:]
    if new:
        raise AssertionError(f"{len(new)} check(s) failed: {new}")
    return res
