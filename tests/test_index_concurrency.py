"""index.llm_concurrency must bound concurrent summarization batches.

Subscription and self-hosted endpoints with low concurrency limits need to
throttle indexing; the previous hard-coded value (8) caused sustained 429
backoff loops on such endpoints during full-index runs.
"""

import pytest
from pydantic import ValidationError

from mira.config import IndexConfig


def test_default_is_eight():
    cfg = IndexConfig()
    assert cfg.llm_concurrency == 8


def test_accepts_low_concurrency_for_throttled_endpoints():
    assert IndexConfig(llm_concurrency=1).llm_concurrency == 1
    assert IndexConfig(llm_concurrency=2).llm_concurrency == 2


@pytest.mark.parametrize("value", [0, -1, 33])
def test_rejects_out_of_bounds(value):
    with pytest.raises(ValidationError):
        IndexConfig(llm_concurrency=value)
