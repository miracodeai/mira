"""Shared test fixtures."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest

from mira.config import MiraConfig
from mira.index import pg_store
from mira.models import (
    FileChangeType,
    FileDiff,
    HunkInfo,
    PatchSet,
    ReviewComment,
    Severity,
    WalkthroughFileEntry,
    WalkthroughResult,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_diff_text() -> str:
    return (FIXTURES_DIR / "sample.diff").read_text()


@pytest.fixture
def sample_config_path() -> Path:
    return FIXTURES_DIR / "sample_config.yml"


@pytest.fixture
def sample_llm_response_text() -> str:
    return (FIXTURES_DIR / "sample_llm_response.json").read_text()


@pytest.fixture
def sample_llm_response_data() -> dict:
    return json.loads((FIXTURES_DIR / "sample_llm_response.json").read_text())


@pytest.fixture
def default_config() -> MiraConfig:
    return MiraConfig()


@pytest.fixture
def sample_hunk() -> HunkInfo:
    return HunkInfo(
        source_start=10,
        source_length=5,
        target_start=10,
        target_length=7,
        content="@@ -10,5 +10,7 @@\n context\n-old line\n+new line\n+added line\n context",
    )


@pytest.fixture
def sample_file_diff(sample_hunk: HunkInfo) -> FileDiff:
    return FileDiff(
        path="src/utils.py",
        change_type=FileChangeType.MODIFIED,
        hunks=[sample_hunk],
        language="python",
        added_lines=2,
        deleted_lines=1,
    )


@pytest.fixture
def sample_patch_set(sample_file_diff: FileDiff) -> PatchSet:
    return PatchSet(files=[sample_file_diff])


@pytest.fixture
def sample_review_comment() -> ReviewComment:
    return ReviewComment(
        path="src/utils.py",
        line=15,
        end_line=None,
        severity=Severity.WARNING,
        category="security",
        title="Potential security issue",
        body="This could be a security vulnerability.",
        confidence=0.85,
        suggestion=None,
    )


@pytest.fixture
def sample_walkthrough_response_text() -> str:
    return (FIXTURES_DIR / "sample_walkthrough_response.json").read_text()


@pytest.fixture
def sample_walkthrough_response_data() -> dict:
    return json.loads((FIXTURES_DIR / "sample_walkthrough_response.json").read_text())


@pytest.fixture
def sample_walkthrough_result() -> WalkthroughResult:
    return WalkthroughResult(
        summary="This PR adds utility functions for shell commands and config parsing.",
        file_changes=[
            WalkthroughFileEntry(
                path="src/utils.py",
                change_type=FileChangeType.ADDED,
                description="New utility module with shell command runner and config reader",
                group="Core",
            ),
            WalkthroughFileEntry(
                path="src/main.py",
                change_type=FileChangeType.MODIFIED,
                description="Added debug parameter to App.start() method",
                group="App Shell",
            ),
        ],
    )


@pytest.fixture
def postgres_url(monkeypatch):
    url = os.environ.get("MIRA_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set MIRA_TEST_POSTGRES_URL to a disposable PostgreSQL database")
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql

    schema = "index_owner_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query))
    params["options"] = f"-c search_path={schema} -c statement_timeout=5000"
    scoped = urlunsplit(parts._replace(query=urlencode(params, quote_via=quote)))
    monkeypatch.setattr(pg_store, "_pg_conn", None)
    monkeypatch.setattr(pg_store, "_schema_initialized", False)
    try:
        yield scoped
    finally:
        pg_store._drop_pg_conn()
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
