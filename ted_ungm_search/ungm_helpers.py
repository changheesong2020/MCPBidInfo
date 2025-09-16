"""Utilities to synchronise UNGM helper datasets."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

import requests
from ._tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .models import Country, Unspsc

logger = logging.getLogger(__name__)

COUNTRY_URL = "https://www.ungm.org/Public/Api/Country"
UNSPSC_URL = "https://www.ungm.org/Public/Api/UNSPSC"
CACHE_DIR = Path(os.getenv("TED_UNGM_CACHE_DIR", Path.home() / ".cache" / "ted-ungm-search"))
COUNTRY_CACHE_FILE = CACHE_DIR / "countries.json"
UNSPSC_CACHE_FILE = CACHE_DIR / "unspsc_segments.json"
REQUEST_TIMEOUT = 20
MAX_RETRIES = 5

_session = requests.Session()
_session.headers.update({"Accept": "application/json"})


class UngmError(RuntimeError):
    """Base class for UNGM helper synchronisation errors."""


class UngmHTTPError(UngmError):
    """Raised for non-success HTTP responses."""

    def __init__(self, message: str, status_code: int, payload: Optional[dict] = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload or {}


class UngmTransientError(UngmHTTPError):
    """Retryable HTTP failure."""


@retry(
    reraise=True,
    stop=stop_after_attempt(MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((requests.RequestException, UngmTransientError)),
)
def _http_get_json(url: str, params: Optional[dict] = None) -> List[dict]:
    logger.debug("Fetching UNGM dataset", extra={"url": url, "params": params})
    response = _session.get(url, params=params, timeout=REQUEST_TIMEOUT)
    if response.status_code in {429} or 500 <= response.status_code < 600:
        raise UngmTransientError(
            f"Retryable HTTP status {response.status_code}", status_code=response.status_code
        )
    if response.status_code != 200:
        try:
            payload = response.json()
        except Exception:  # pragma: no cover - diagnostics only
            payload = None
        raise UngmHTTPError(
            f"Unexpected HTTP status {response.status_code}",
            status_code=response.status_code,
            payload=payload,
        )
    data = response.json()
    if isinstance(data, dict):
        # some endpoints wrap the list under a known key
        for key in ("items", "results", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        raise UngmError(f"Unexpected payload shape: {data}")
    if not isinstance(data, list):
        raise UngmError(f"Expected list payload but received: {type(data)!r}")
    return data


def _ensure_cache_dir() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _write_cache(path: Path, data: Iterable[dict]) -> None:
    _ensure_cache_dir()
    with path.open("w", encoding="utf-8") as handle:
        json.dump(list(data), handle, ensure_ascii=False, indent=2)
    logger.info("Updated cache", extra={"path": str(path)})


def _read_cache(path: Path) -> Optional[List[dict]]:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _fetch_dataset(url: str, cache_path: Path, params: Optional[dict] = None) -> List[dict]:
    try:
        data = _http_get_json(url, params=params)
        _write_cache(cache_path, data)
        return data
    except Exception as exc:
        logger.warning(
            "Failed to download UNGM dataset, attempting to reuse cache",
            extra={"url": url, "cache": str(cache_path)},
        )
        cached = _read_cache(cache_path)
        if cached is not None:
            return cached
        raise


def sync_country_codes() -> List[Country]:
    """Synchronise and return the UNGM country helper dataset."""

    data = _fetch_dataset(COUNTRY_URL, COUNTRY_CACHE_FILE)
    countries: List[Country] = []
    for item in data:
        try:
            countries.append(Country.from_api(item))
        except ValueError as exc:
            logger.debug("Skipping malformed country entry", extra={"error": str(exc)})
    return countries


def sync_unspsc_segments() -> List[Unspsc]:
    """Synchronise and return the UNGM UNSPSC segment helper dataset."""

    data = _fetch_dataset(UNSPSC_URL, UNSPSC_CACHE_FILE, params={"level": "segment"})
    segments: List[Unspsc] = []
    for item in data:
        try:
            segments.append(Unspsc.from_api(item))
        except ValueError as exc:
            logger.debug("Skipping malformed UNSPSC entry", extra={"error": str(exc)})
    return segments


def build_ungm_deeplink(
    countries: Optional[Sequence[str]] = None,
    unspsc_codes: Optional[Sequence[str]] = None,
    keywords: Optional[Sequence[str]] = None,
) -> str:
    """Build a shallow UNGM public notice search URL."""

    from urllib.parse import urlencode

    params: List[Tuple[str, str]] = []
    if countries:
        for code in countries:
            cleaned = code.strip().upper()
            if cleaned:
                params.append(("Country", cleaned))
    if unspsc_codes:
        for code in unspsc_codes:
            cleaned = code.strip()
            if cleaned:
                params.append(("Unspsc", cleaned))
    if keywords:
        joined = " ".join(keyword.strip() for keyword in keywords if keyword.strip())
        if joined:
            params.append(("searchText", joined))

    query_string = urlencode(params, doseq=True)
    base_url = "https://www.ungm.org/Public/Notice"
    return f"{base_url}?{query_string}" if query_string else base_url


__all__ = [
    "build_ungm_deeplink",
    "sync_country_codes",
    "sync_unspsc_segments",
]
