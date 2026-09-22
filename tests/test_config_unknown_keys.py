"""Unknown keys in configuration must fail validation, not silently no-op.

Regression context: ``max_context_tokens`` is an ``llm.*`` key, but placing it
under ``review:`` was silently ignored — chunking kept the 120k-token default
while the operator believed they had set 40k. Silent no-ops on config typos
are indistinguishable from applied settings.
"""

import pytest
from pydantic import ValidationError

from mira.config import FilterConfig, LLMConfig, MiraConfig, ReviewConfig


def test_llm_config_rejects_unknown_key():
    with pytest.raises(ValidationError, match="max_context_tokenz"):
        LLMConfig(max_context_tokenz=40000)


def test_review_config_rejects_llm_only_key():
    # regression: max_context_tokens belongs under llm:, not review:
    with pytest.raises(ValidationError, match="max_context_tokens"):
        ReviewConfig(max_context_tokens=40000)


def test_filter_config_rejects_unknown_key():
    with pytest.raises(ValidationError, match="confidence_treshold"):
        FilterConfig(confidence_treshold=0.5)


def test_mira_config_rejects_unknown_nested_key():
    with pytest.raises(ValidationError, match="max_context_tokens"):
        MiraConfig(review={"max_context_tokens": 40000})


def test_mira_config_rejects_unknown_top_level_key():
    with pytest.raises(ValidationError, match="walkthrough"):
        MiraConfig(walkthrough=True)


def test_valid_config_still_loads():
    cfg = MiraConfig(
        llm={"model": "glm-5.3-flash", "max_context_tokens": 40000},
        review={"max_chunks_per_review": 10, "max_concurrent_chunks": 2},
        filter={"confidence_threshold": 0.5, "allowed_authors": ["SylTi"]},
    )
    assert cfg.llm.max_context_tokens == 40000
    assert cfg.review.max_chunks_per_review == 10
    assert cfg.filter.confidence_threshold == 0.5
    assert cfg.filter.allowed_authors == ["SylTi"]
