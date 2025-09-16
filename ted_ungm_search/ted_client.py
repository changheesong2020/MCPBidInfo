"""Client utilities for the TED Search API v3."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Dict, Iterator, List, Optional, Sequence, Union

import requests
from ._tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .models import SearchConfig, TedNotice, TedSearchPage

logger = logging.getLogger(__name__)

API_URL = "https://api.ted.europa.eu/v3/notices/search"
DEFAULT_FIELDS: List[str] = [
    "publication-number",
    "title",
    "buyer-name",
    "publication-date",
    "classification-cpv",
    "place-of-performance.country",
]
REQUEST_TIMEOUT = 20
MAX_RETRIES = 5

_session = requests.Session()
_session.headers.update({"Accept": "application/json"})


class TedClientError(RuntimeError):
    """Base exception for TED client failures."""


class TedHTTPError(TedClientError):
    """Raised when the TED API returns a non-success HTTP status."""

    def __init__(self, message: str, status_code: int, payload: Optional[dict] = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload or {}


class TedTransientError(TedHTTPError):
    """Represents retryable HTTP failures."""


class ResponseDecodeError(TedClientError):
    """Raised when the API response cannot be decoded as JSON."""


def _format_date(value: Union[str, date, datetime]) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.isoformat()
    return value


def build_query(
    date_from: Union[str, date, datetime],
    date_to: Union[str, date, datetime],
    countries: Optional[Sequence[str]] = None,
    cpv_prefixes: Optional[Sequence[str]] = None,
    keywords: Optional[Sequence[str]] = None,
    form_types: Optional[Sequence[str]] = None,
) -> str:
    """Construct a TED API query string.

    Parameters
    ----------
    date_from: str | date | datetime
        Lower bound for the publication date filter (inclusive).
    date_to: str | date | datetime
        Upper bound for the publication date filter (inclusive).
    countries: Sequence[str] | None
        ISO alpha-2 country codes mapped to both place-of-performance and buyer country filters.
    cpv_prefixes: Sequence[str] | None
        List of CPV prefix strings (supports wildcards as provided by the API).
    keywords: Sequence[str] | None
        Keywords to search for in the notice title field.  Values containing whitespace are quoted.
    form_types: Sequence[str] | None
        Optional eForms form type filters.

    Returns
    -------
    str
        A query string suitable for the `q` parameter of the TED Search endpoint.
    """

    clauses: List[str] = []
    clauses.append(
        f"publication-date:[{_format_date(date_from)} TO {_format_date(date_to)}]"
    )

    if countries:
        country_clauses = []
        for code in countries:
            code_clean = code.strip().upper()
            if not code_clean:
                continue
            country_clauses.append(
                f"(place-of-performance.country:{code_clean} OR buyer-country:{code_clean})"
            )
        if country_clauses:
            clauses.append("(" + " OR ".join(country_clauses) + ")")

    if cpv_prefixes:
        cpv_clauses = []
        for prefix in cpv_prefixes:
            prefix_clean = prefix.strip()
            if not prefix_clean:
                continue
            cpv_clauses.append(f"classification-cpv:{prefix_clean}")
        if cpv_clauses:
            clauses.append("(" + " OR ".join(cpv_clauses) + ")")

    if keywords:
        keyword_tokens = []
        for word in keywords:
            token = word.strip()
            if not token:
                continue
            if " " in token or ":" in token:
                token = f'"{token}"'
            keyword_tokens.append(token)
        if keyword_tokens:
            clauses.append("title:(" + " OR ".join(keyword_tokens) + ")")

    if form_types:
        form_type_tokens = [ft.strip() for ft in form_types if ft.strip()]
        if form_type_tokens:
            clauses.append("form-type:(" + " OR ".join(form_type_tokens) + ")")

    return " AND ".join(clauses)


def _normalise_fields(fields: Optional[Sequence[str]]) -> List[str]:
    if not fields:
        return list(DEFAULT_FIELDS)
    result: List[str] = []
    for field in fields:
        field_name = field.strip()
        if field_name:
            result.append(field_name)
    return result or list(DEFAULT_FIELDS)


@retry(
    reraise=True,
    stop=stop_after_attempt(MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((requests.RequestException, TedTransientError)),
)
def _perform_request(payload: Dict[str, Union[str, int]]) -> dict:
    logger.debug("Performing TED request", extra={"payload": payload})
    response = _session.post(API_URL, json=payload, timeout=REQUEST_TIMEOUT)
    if response.status_code in {429} or 500 <= response.status_code < 600:
        raise TedTransientError(
            f"Retryable HTTP status {response.status_code}",
            status_code=response.status_code,
        )
    if response.status_code != 200:
        raise TedHTTPError(
            f"Unexpected HTTP status {response.status_code}",
            status_code=response.status_code,
            payload=_safe_json(response),
        )
    try:
        return response.json()
    except json.JSONDecodeError as exc:  # pragma: no cover - extremely rare
        raise ResponseDecodeError("Failed to decode TED response as JSON") from exc


def _safe_json(response: requests.Response) -> Optional[dict]:
    try:
        return response.json()
    except Exception:  # pragma: no cover - diagnostic helper
        return None


def _request_with_iteration_token(
    params: Dict[str, Union[str, int]], iteration_token: Optional[str] = None
) -> dict:
    if not iteration_token:
        return _perform_request(params)

    token_params = ["page-token", "next-page-token", "iteration-token", "page"]
    last_error: Optional[TedHTTPError] = None
    for token_param in token_params:
        params_with_token = params.copy()
        params_with_token[token_param] = iteration_token
        try:
            return _perform_request(params_with_token)
        except TedHTTPError as exc:
            if exc.status_code in {400, 422} and token_param != "page":
                last_error = exc
                continue
            raise
    if last_error:
        raise last_error
    raise TedClientError("Iteration token request failed with no response")


def search_once(
    q: str,
    fields: Optional[Sequence[str]] = None,
    page: Union[int, str] = 1,
    limit: int = 100,
    sort_field: str = "publication-date",
    sort_order: str = "desc",
    iteration_token: Optional[str] = None,
) -> TedSearchPage:
    """Execute a single TED search request."""

    selected_fields = _normalise_fields(fields)
    params: Dict[str, Union[str, int]] = {
        "q": q,
        "fields": ",".join(selected_fields),
        "limit": limit,
        "sort": sort_field,
        "order": sort_order,
    }

    if iteration_token:
        logger.info(
            "Requesting TED iteration batch",
            extra={"iteration_token": iteration_token, "limit": limit},
        )
    else:
        params["page"] = str(page)
        logger.info(
            "Requesting TED page",
            extra={"page": page, "limit": limit, "query": q},
        )

    payload = _request_with_iteration_token(params, iteration_token)
    logger.debug("TED response payload", extra={"keys": list(payload.keys())})

    search_page = TedSearchPage.from_api_payload(
        payload=payload,
        query=q,
        fields=selected_fields,
        sort_field=sort_field,
        sort_order=sort_order,
    )
    return search_page


def iterate_all(
    q: str,
    fields: Optional[Sequence[str]] = None,
    batch_limit: int = 250,
    sort_field: str = "publication-date",
    sort_order: str = "desc",
) -> Iterator[TedNotice]:
    """Iterate through all notices for a given query using the iteration token flow."""

    iteration_token: Optional[str] = None
    while True:
        page = search_once(
            q=q,
            fields=fields,
            page=1,
            limit=batch_limit,
            sort_field=sort_field,
            sort_order=sort_order,
            iteration_token=iteration_token,
        )
        for notice in page.notices:
            yield notice

        if not page.next_page_token:
            logger.info("Iteration completed", extra={"query": q})
            break
        iteration_token = page.next_page_token
        logger.info("Continuing iteration", extra={"next_token": iteration_token})


__all__ = [
    "API_URL",
    "DEFAULT_FIELDS",
    "SearchConfig",
    "TedNotice",
    "TedSearchPage",
    "build_query",
    "iterate_all",
    "search_once",
    "TedClientError",
    "TedHTTPError",
    "TedTransientError",
]
