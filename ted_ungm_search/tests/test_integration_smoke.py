"""Opt-in smoke test that validates the live TED API."""
from __future__ import annotations

import pytest

from ted_ungm_search.ted_client import DEFAULT_FIELDS, build_query, search_once


@pytest.mark.live
@pytest.mark.usefixtures("runlive")
def test_live_search_smoke(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--runlive"):
        pytest.skip("Live TED API tests disabled. Use --runlive to enable.")

    query = build_query("2024-01-01", "2024-01-07", countries=["DE"], cpv_prefixes=["33*"])
    page = search_once(q=query, fields=DEFAULT_FIELDS, limit=5)
    assert page.count >= 0
    assert page.notices, "Expected at least one notice"
    first_notice = page.notices[0].to_dict()
    for field in DEFAULT_FIELDS:
        assert field in first_notice
