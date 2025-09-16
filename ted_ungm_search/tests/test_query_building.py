"""Tests for the query building logic."""
from __future__ import annotations

from ted_ungm_search.ted_client import build_query


def test_build_query_with_all_filters() -> None:
    query = build_query(
        date_from="2025-06-18",
        date_to="2025-09-16",
        countries=["DE", "FR"],
        cpv_prefixes=["33*"],
        keywords=["PCR", "reagent", "diagnostic"],
        form_types=["F15"],
    )
    expected = (
        "publication-date:[2025-06-18 TO 2025-09-16]"
        " AND ((place-of-performance.country:DE OR buyer-country:DE)"
        " OR (place-of-performance.country:FR OR buyer-country:FR))"
        " AND (classification-cpv:33*)"
        " AND title:(PCR OR reagent OR diagnostic)"
        " AND form-type:(F15)"
    )
    assert query == expected


def test_build_query_minimal() -> None:
    query = build_query("2025-06-18", "2025-09-16")
    assert query == "publication-date:[2025-06-18 TO 2025-09-16]"
