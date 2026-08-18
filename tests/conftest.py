"""Pytest configuration and shared fixtures for benchkit tests."""

import sys
from pathlib import Path

import pytest

# Ensure src/ is on sys.path so tests can import the benchkit package directly.
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture
def tmp_root(tmp_path):
    """Isolated on-disk project root for a test (results/, benchmarks/, etc.)."""
    root = tmp_path / "work"
    root.mkdir()
    (root / "benchmarks").mkdir()
    (root / "models").mkdir()
    (root / "configs" / "bundles").mkdir(parents=True)
    (root / "results").mkdir()
    return root


@pytest.fixture
def env(monkeypatch, tmp_root):
    """Set RESULTS_ROOT and BENCKKIT_ROOT to the tmp_root for isolation."""
    monkeypatch.setenv("BENCKKIT_ROOT", str(tmp_root))
    monkeypatch.setenv("RESULTS_ROOT", str(tmp_root / "results"))
    return tmp_root