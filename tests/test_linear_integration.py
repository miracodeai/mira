from __future__ import annotations

import httpx
import pytest

from mira.integrations.linear import extract_linear_issue_identifiers, fetch_linear_context
from mira.models import PRInfo


def _pr(**overrides) -> PRInfo:
    values = {
        "title": "Implement checkout",
        "description": "",
        "base_branch": "main",
        "head_branch": "feat/epic-123-checkout",
        "url": "https://github.com/acme/shop/pull/1",
        "number": 1,
        "owner": "acme",
        "repo": "shop",
    }
    values.update(overrides)
    return PRInfo(**values)


def test_extracts_and_deduplicates_identifiers() -> None:
    pr_info = _pr(
        title="EPIC-123 Add checkout",
        description="Implements epic-123 and API-9",
        head_branch="feat/API-9-checkout",
    )

    assert extract_linear_issue_identifiers(pr_info) == ["EPIC-123", "API-9"]


@pytest.mark.asyncio
async def test_fetches_issue_requirements_and_discussion() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "secret"
        return httpx.Response(
            200,
            json={
                "data": {
                    "issue": {
                        "identifier": "EPIC-123",
                        "title": "Add checkout",
                        "description": "Acceptance criteria:\n- Reject expired cards",
                        "url": "https://linear.app/acme/issue/EPIC-123/add-checkout",
                        "priorityLabel": "High",
                        "state": {"name": "In Progress"},
                        "assignee": {"name": "Alex"},
                        "project": {"name": "Payments"},
                        "labels": {"nodes": [{"name": "backend"}]},
                        "comments": {
                            "nodes": [{"body": "Use the existing gateway", "user": {"name": "Sam"}}]
                        },
                    }
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        context = await fetch_linear_context(_pr(), api_key="secret", client=client)

    assert "EPIC-123: Add checkout" in context
    assert "Reject expired cards" in context
    assert "Sam: Use the existing gateway" in context
    assert "Project: Payments" in context


@pytest.mark.asyncio
async def test_missing_key_or_identifier_skips_request() -> None:
    assert await fetch_linear_context(_pr(), api_key="") == ""
    assert await fetch_linear_context(_pr(head_branch="feat/checkout"), api_key="secret") == ""


@pytest.mark.asyncio
async def test_api_failure_does_not_block_review() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(500))
    ) as client:
        context = await fetch_linear_context(_pr(), api_key="secret", client=client)

    assert context == ""
