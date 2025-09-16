"""Pydantic models used across the TED/UNGM toolkit."""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .pydantic_compat import BaseModel, Field


class SearchConfig(BaseModel):
    """Capture the high-level inputs for a TED notice search."""

    date_from: date
    date_to: date
    countries: List[str] = Field(default_factory=list)
    cpv_prefixes: List[str] = Field(default_factory=list)
    keywords: List[str] = Field(default_factory=list)
    form_types: List[str] = Field(default_factory=list)
    fields: List[str] = Field(default_factory=list)
    page: int = 1
    limit: int = 100
    sort_field: str = "publication-date"
    sort_order: str = "desc"
    mode: str = "page"


class TedNotice(BaseModel):
    """Represent a single TED notice as returned by the API."""

    data: Dict[str, Any] = Field(default_factory=dict)

    class Config:
        allow_population_by_field_name = True
        arbitrary_types_allowed = True

    def get_field(self, field_name: str, default: Optional[Any] = None) -> Any:
        """Convenience accessor returning ``default`` when missing."""

        return self.data.get(field_name, default)

    def to_dict(self) -> Dict[str, Any]:
        """Return the raw notice as a standard dict."""

        return dict(self.data)


class TedSearchPage(BaseModel):
    """Model a page or batch returned by the TED search endpoint."""

    query: str
    page: Optional[int]
    limit: Optional[int]
    count: int
    total: Optional[int]
    fields: List[str]
    sort_field: Optional[str]
    sort_order: Optional[str]
    notices: List[TedNotice]
    next_page_token: Optional[str] = None
    raw: Dict[str, Any] = Field(default_factory=dict)

    class Config:
        allow_population_by_field_name = True
        arbitrary_types_allowed = True

    @classmethod
    def from_api_payload(
        cls,
        payload: Dict[str, Any],
        query: str,
        fields: Sequence[str],
        sort_field: Optional[str] = None,
        sort_order: Optional[str] = None,
    ) -> "TedSearchPage":
        results = _extract_results(payload)
        notices = [TedNotice(data=item) for item in results]
        count = int(payload.get("count", len(results)))
        page_value = _first_int(payload, ("page", "currentPage"))
        limit_value = _first_int(payload, ("limit", "pageSize"))
        total_value = _first_int(payload, ("total", "totalResults"), allow_none=True)
        extracted_sort_field = payload.get("sort") or payload.get("sortField") or sort_field
        extracted_sort_order = payload.get("order") or payload.get("sortOrder") or sort_order
        token = _extract_iteration_token(payload)
        return cls(
            query=query,
            page=page_value,
            limit=limit_value,
            count=count,
            total=total_value,
            fields=list(fields),
            sort_field=extracted_sort_field,
            sort_order=extracted_sort_order,
            notices=notices,
            next_page_token=token,
            raw=payload,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialise the page to a JSON-friendly representation."""

        return {
            "query": self.query,
            "page": self.page,
            "limit": self.limit,
            "count": self.count,
            "total": self.total,
            "fields": list(self.fields),
            "sort_field": self.sort_field,
            "sort_order": self.sort_order,
            "notices": [notice.to_dict() for notice in self.notices],
            "next_page_token": self.next_page_token,
        }


class Country(BaseModel):
    """Country entry as defined by the UNGM helper dataset."""

    code: str
    name: str

    @classmethod
    def from_api(cls, payload: Dict[str, Any]) -> "Country":
        code = (
            payload.get("code")
            or payload.get("countryCode")
            or payload.get("CountryCode")
            or payload.get("isoCode")
        )
        name = (
            payload.get("name")
            or payload.get("countryName")
            or payload.get("CountryName")
            or payload.get("description")
        )
        if not code or not name:
            raise ValueError(f"Unsupported country payload: {payload}")
        return cls(code=str(code).upper(), name=str(name))


class Unspsc(BaseModel):
    """UNSPSC segment entry from the UNGM helper dataset."""

    code: str
    title: str
    level: Optional[str] = None

    @classmethod
    def from_api(cls, payload: Dict[str, Any]) -> "Unspsc":
        code = (
            payload.get("code")
            or payload.get("segment")
            or payload.get("segmentCode")
            or payload.get("Segment")
        )
        title = (
            payload.get("title")
            or payload.get("name")
            or payload.get("segmentTitle")
            or payload.get("SegmentTitle")
        )
        level = (
            payload.get("level")
            or payload.get("Level")
            or payload.get("segmentLevel")
        )
        if not code or not title:
            raise ValueError(f"Unsupported UNSPSC payload: {payload}")
        return cls(code=str(code), title=str(title), level=level)


def _extract_results(payload: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    for key in ("results", "items", "notices", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _first_int(
    payload: Dict[str, Any],
    keys: Sequence[str],
    allow_none: bool = False,
) -> Optional[int]:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    if allow_none:
        return None
    return None


def _extract_iteration_token(payload: Dict[str, Any]) -> Optional[str]:
    for key in ("nextPageToken", "next-page-token", "iterationToken"):
        token = payload.get(key)
        if token:
            return str(token)
    next_page = payload.get("nextPage") or payload.get("next-page")
    if isinstance(next_page, dict):
        token = next_page.get("token") or next_page.get("pageToken")
        if token:
            return str(token)
    return None


__all__ = [
    "Country",
    "SearchConfig",
    "TedNotice",
    "TedSearchPage",
    "Unspsc",
]
