"""Shared fixtures.

The suite runs with NO env keys and never touches private seed data — every
fixture is built from `samples/`, which is synthetic.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from weaver import db, extract
from weaver import lenses as lenses_mod

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLES = REPO_ROOT / "samples"

SCRUBBED_ENV = (
    "OPENAI_API_KEY",
    "WEAVER_API_KEY",
    "SKYVERN_API_KEY",
    "WEAVER_MODEL",
    "WEAVER_BASE_URL",
    "OPENAI_BASE_URL",
    "WEAVER_DATA_DIR",
    "WEAVER_TEMPERATURE",
)


@pytest.fixture(autouse=True)
def no_env_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must pass with no provider keys; make that true regardless of shell."""
    for name in SCRUBBED_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def samples_dir() -> Path:
    if not SAMPLES.is_dir():
        pytest.skip("samples/ directory is missing")
    return SAMPLES


@pytest.fixture
def sample_resume(samples_dir: Path) -> Path:
    path = samples_dir / "sample-resume.md"
    if not path.exists():
        pytest.skip("samples/sample-resume.md is missing")
    return path


@pytest.fixture
def sample_job(samples_dir: Path) -> Path:
    path = samples_dir / "sample-job.txt"
    if not path.exists():
        pytest.skip("samples/sample-job.txt is missing")
    return path


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def conn(data_dir: Path):
    connection = db.init_db(data_dir)
    lenses_mod.seed(connection)
    yield connection
    connection.close()


@pytest.fixture
def graph(conn: sqlite3.Connection, samples_dir: Path):
    """A fact graph built from the synthetic sample resume."""
    extract.import_directory(conn, samples_dir, use_llm=False)
    return conn
