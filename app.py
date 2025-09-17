import os
import re
import json
import time
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any, Tuple, Sequence, Iterator

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover - optional dependency
    requests = None
    HTTPAdapter = None
    Retry = None
try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - optional dependency
    BeautifulSoup = None
from flask import Flask, request, jsonify, render_template, redirect, url_for
try:
    from apscheduler.schedulers.background import BackgroundScheduler
except Exception:  # pragma: no cover - optional dependency
    BackgroundScheduler = None
from sqlalchemy import (
    Column,
    String,
    DateTime,
    Integer,
    Text,
    create_engine,
    UniqueConstraint,
    Index,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, scoped_session, load_only
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from logging.handlers import RotatingFileHandler

from ted_ungm_search.ted_client import (
    DEFAULT_FIELDS as TED_DEFAULT_FIELDS,
    TedClientError,
    TedHTTPError,
    build_query as build_ted_query,
    iterate_all as ted_iterate_all,
    search_once as ted_search_once,
)
from ted_ungm_search.ungm_helpers import build_ungm_deeplink

# Logging / parsing helpers
_API_KEY_PATTERN = re.compile(r"(api_key=)([^&]+)", re.IGNORECASE)


def _mask_api_key(text: str) -> str:
    """Mask API keys embedded in log messages."""

    if not text:
        return text

    def _replace(match: re.Match[str]) -> str:
        prefix = match.group(1)
        return f"{prefix}***"

    return _API_KEY_PATTERN.sub(_replace, text)

# 데이터베이스 모델 설정
Base = declarative_base()

class Tender(Base):
    __tablename__ = 'tenders'
    
    id = Column(Integer, primary_key=True)
    site = Column(String(10), nullable=False)
    reference_no = Column(String(100), nullable=False)
    title = Column(Text, nullable=False)
    description = Column(Text)
    published_date = Column(DateTime)
    deadline_date = Column(DateTime)
    organization = Column(String(255))
    notice_type = Column(String(100))
    country = Column(String(100))
    detail_url = Column(Text)
    last_updated = Column(DateTime)
    
    __table_args__ = (
        UniqueConstraint('site', 'reference_no', name='uix_site_ref'),
        Index('idx_tenders_site', 'site'),
        Index('idx_tenders_published_date', 'published_date'),
    )

class SearchConfig(Base):
    __tablename__ = 'search_configs'
    
    id = Column(Integer, primary_key=True)
    site = Column(String(10), nullable=False, unique=True)
    query = Column(Text)
    last_updated = Column(DateTime)

# Flask 앱 설정
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here'

# 데이터베이스 설정
engine = create_engine('sqlite:///mcp_tenders.db', echo=False)
Base.metadata.create_all(engine)
SessionLocal = sessionmaker(bind=engine)
session = scoped_session(SessionLocal)

# 로깅 설정
def setup_logging():
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    # 파일 핸들러
    handler = RotatingFileHandler('mcp_server.log', maxBytes=1000000, backupCount=3)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    
    # 콘솔 핸들러
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

setup_logging()

DEFAULT_UNGM_KEYWORDS = ["PCR", "reagent", "diagnostic"]
DEFAULT_TED_LOOKBACK_DAYS = 30
DEFAULT_TED_COUNTRIES = ["DE", "FR"]
DEFAULT_TED_CPV_PREFIXES = ["33*"]
DEFAULT_TED_KEYWORDS = ["PCR", "reagent", "diagnostic"]
DEFAULT_TED_FORM_TYPES = ["F15"]
DEFAULT_TED_FIELDS = list(TED_DEFAULT_FIELDS)
for _extra_field in [
    "description",
    "deadline-date",
    "buyer-country",
    "notice-type",
    "form-type",
]:
    if _extra_field not in DEFAULT_TED_FIELDS:
        DEFAULT_TED_FIELDS.append(_extra_field)

DEFAULT_SAM_KEYWORDS = ["PCR", "reagent", "diagnostic"]
DEFAULT_SAM_LOOKBACK_DAYS = 30
DEFAULT_SAM_NOTICE_TYPES: List[str] = []
DEFAULT_SAM_SET_ASIDES: List[str] = []
DEFAULT_SAM_PAGE_SIZE = 100
DEFAULT_SAM_MAX_PAGES = 1


def _default_ted_builder() -> Dict[str, Any]:
    today = datetime.utcnow().date()
    start = today - timedelta(days=DEFAULT_TED_LOOKBACK_DAYS)
    return {
        "date_from": start.isoformat(),
        "date_to": today.isoformat(),
        "countries": list(DEFAULT_TED_COUNTRIES),
        "cpv_prefixes": list(DEFAULT_TED_CPV_PREFIXES),
        "keywords": list(DEFAULT_TED_KEYWORDS),
        "form_types": list(DEFAULT_TED_FORM_TYPES),
    }


def _build_query_from_builder(builder: Dict[str, Any]) -> str:
    countries = [value.upper() for value in _ensure_list(builder.get("countries"))]
    cpv_prefixes = _ensure_list(builder.get("cpv_prefixes"))
    keywords = _ensure_list(builder.get("keywords"))
    form_types = [value.upper() for value in _ensure_list(builder.get("form_types"))]

    return build_ted_query(
        date_from=builder.get("date_from"),
        date_to=builder.get("date_to"),
        countries=countries or None,
        cpv_prefixes=cpv_prefixes or None,
        keywords=keywords or None,
        form_types=form_types or None,
    )


def _default_ted_query() -> str:
    return _build_query_from_builder(_default_ted_builder())


def _merge_ted_builder(
    base: Dict[str, Any], overrides: Dict[str, Any]
) -> Dict[str, Any]:
    merged = dict(base)

    date_from = overrides.get("date_from")
    if isinstance(date_from, str) and date_from.strip():
        merged["date_from"] = date_from.strip()
    date_to = overrides.get("date_to")
    if isinstance(date_to, str) and date_to.strip():
        merged["date_to"] = date_to.strip()

    for key, uppercase in (
        ("countries", True),
        ("cpv_prefixes", False),
        ("keywords", False),
        ("form_types", True),
    ):
        if key in overrides:
            values = _ensure_list(overrides.get(key))
            if uppercase:
                values = [value.upper() for value in values]
            merged[key] = values

    return merged


def _split_tokens(raw: str) -> List[str]:
    if not raw:
        return []
    tokens: List[str] = []
    for part in raw.replace("\n", ",").split(","):
        token = part.strip()
        if token:
            tokens.append(token)
    return tokens


def _ensure_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return _split_tokens(value)
    return []


def _parse_ted_config(config_text: Optional[str]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    default_builder = _default_ted_builder()
    settings: Dict[str, Any] = {
        "query": _default_ted_query(),
        "fields": list(DEFAULT_TED_FIELDS),
        "limit": 100,
        "sort_field": "publication-date",
        "sort_order": "DESC",
        "mode": "page",
        "page": 1,
    }
    builder: Dict[str, Any] = dict(default_builder)

    if not config_text:
        return settings, builder

    try:
        data = json.loads(config_text)
    except (TypeError, ValueError):
        settings["query"] = config_text
        return settings, builder

    if not isinstance(data, dict):
        settings["query"] = config_text
        return settings, builder

    query_value = data.get("query")
    if isinstance(query_value, str) and query_value.strip():
        settings["query"] = query_value

    fields_value = data.get("fields")
    if isinstance(fields_value, list):
        cleaned_fields = [str(field).strip() for field in fields_value if str(field).strip()]
        if cleaned_fields:
            settings["fields"] = cleaned_fields

    limit_value = data.get("limit")
    try:
        if limit_value is not None:
            limit_int = int(limit_value)
            if limit_int > 0:
                settings["limit"] = limit_int
    except (TypeError, ValueError):
        pass

    sort_field = data.get("sort_field") or data.get("sortBy")
    if isinstance(sort_field, str) and sort_field.strip():
        settings["sort_field"] = sort_field.strip()

    sort_order = data.get("sort_order") or data.get("order")
    if isinstance(sort_order, str) and sort_order.strip():
        settings["sort_order"] = sort_order.strip().upper()

    mode_value = data.get("mode")
    if isinstance(mode_value, str) and mode_value.strip():
        settings["mode"] = mode_value.strip()

    page_value = data.get("page")
    try:
        if page_value is not None:
            page_int = int(page_value)
            if page_int > 0:
                settings["page"] = page_int
    except (TypeError, ValueError):
        pass

    builder_candidate = data.get("builder")
    if isinstance(builder_candidate, dict):
        builder = _merge_ted_builder(builder, builder_candidate)

    if not settings.get("query"):
        settings["query"] = _build_query_from_builder(builder)

    return settings, builder


def _default_sam_settings() -> Dict[str, Any]:
    today = datetime.utcnow().date()
    return {
        "keywords": list(DEFAULT_SAM_KEYWORDS),
        "posted_from": (today - timedelta(days=DEFAULT_SAM_LOOKBACK_DAYS)).isoformat(),
        "posted_to": today.isoformat(),
        "notice_types": list(DEFAULT_SAM_NOTICE_TYPES),
        "set_asides": list(DEFAULT_SAM_SET_ASIDES),
        "naics": [],
        "limit": DEFAULT_SAM_PAGE_SIZE,
        "max_pages": DEFAULT_SAM_MAX_PAGES,
        "sort": "-modifiedDate",
    }


def _parse_sam_config(config_text: Optional[str]) -> Dict[str, Any]:
    settings = _default_sam_settings()

    if not config_text:
        return settings

    try:
        data = json.loads(config_text)
    except (TypeError, ValueError):
        keywords = _split_tokens(config_text)
        if keywords:
            settings["keywords"] = keywords
        return settings

    if not isinstance(data, dict):
        keywords = _split_tokens(config_text)
        if keywords:
            settings["keywords"] = keywords
        return settings

    keywords_value = data.get("keywords")
    keywords_list = _ensure_list(keywords_value)
    if keywords_list:
        settings["keywords"] = keywords_list

    for key in ("notice_types", "set_asides", "naics"):
        value_list = _ensure_list(data.get(key))
        if value_list:
            settings[key] = value_list

    posted_from = data.get("posted_from") or data.get("postedFrom")
    if isinstance(posted_from, str) and posted_from.strip():
        settings["posted_from"] = posted_from.strip()

    posted_to = data.get("posted_to") or data.get("postedTo")
    if isinstance(posted_to, str) and posted_to.strip():
        settings["posted_to"] = posted_to.strip()

    sort_value = data.get("sort")
    if isinstance(sort_value, str) and sort_value.strip():
        settings["sort"] = sort_value.strip()

    for key in ("limit", "max_pages"):
        try:
            raw = data.get(key)
            if raw is not None:
                parsed = int(raw)
                if parsed > 0:
                    settings[key] = parsed
        except (TypeError, ValueError):
            continue

    return settings


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None

    text = str(value).strip()
    if not text:
        return None

    text = text.replace("Z", "+00:00")
    if re.match(r".*[\+\-]\d{4}$", text) and text[-3] != ":":
        text = f"{text[:-4]}{text[-4:-2]}:{text[-2:]}"

    try:
        return datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None


def _coerce_text(value: Any) -> str:
    """Return a safe string representation for response text payloads."""

    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.decode("latin-1", errors="ignore")
    if value is None:
        return ""
    return str(value)


def _preview_text(value: Any, length: int = 200) -> str:
    """Return a short preview of a response payload for logging."""

    text = _coerce_text(value)
    return text[:length] if length >= 0 else text


def _normalise_ungm_value(value: Any) -> Optional[str]:
    """Normalise nested UNGM JSON fields to a plain string."""

    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("value", "Value", "Name", "name", "Description", "description", "label", "Label", "text", "Text"):
            if key in value:
                nested = _normalise_ungm_value(value[key])
                if nested:
                    return nested
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            nested = _normalise_ungm_value(item)
            if nested:
                return nested
        return None
    text = str(value).strip()
    return text or None


def _extract_ungm_field(record: Dict[str, Any], keys: Sequence[str]) -> Optional[str]:
    """Return the first matching value for any of the candidate keys."""

    lower_map = {str(key).lower(): value for key, value in record.items()}
    for key in keys:
        value = record.get(key)
        if value is None:
            value = lower_map.get(str(key).lower())
        text = _normalise_ungm_value(value)
        if text:
            return text
    return None


def _parse_ungm_date(value: Optional[str]) -> Optional[datetime]:
    """Parse UNGM date strings with multiple fallback formats."""

    if not value:
        return None

    text = str(value).strip()
    if not text:
        return None

    text = re.sub(r"\s*\(.*?\)\s*$", "", text)
    text = text.replace("GMT", "").replace("UTC", "").strip()
    iso_candidate = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso_candidate)
    except ValueError:
        pass

    epoch_match = re.search(r"/Date\((\d+)\)/", text)
    if epoch_match:
        try:
            milliseconds = int(epoch_match.group(1))
            return datetime.utcfromtimestamp(milliseconds / 1000.0)
        except ValueError:
            return None

    for fmt in (
        "%d-%b-%Y",
        "%d-%b-%Y %H:%M",
        "%d-%b-%Y %H:%M:%S",
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y",
        "%d/%m/%Y %H:%M",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    return None

class TenderCrawler:
    def __init__(self):
        if requests is None:
            raise RuntimeError("requests library is required for crawling")
        if HTTPAdapter is None:
            raise RuntimeError("requests.adapters.HTTPAdapter is required for crawling")

        self.session = requests.Session()
        retry_strategy = None
        if Retry is not None:
            retry_strategy = Retry(
                total=5,
                backoff_factor=0.3,
                status_forcelist=[500, 502, 503, 504],
                allowed_methods=frozenset({"GET", "POST"}),
            )

        adapter = (
            HTTPAdapter(max_retries=retry_strategy) if retry_strategy else HTTPAdapter()
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

    def _extract_ungm_tokens(
        self, response: "requests.Response"
    ) -> Tuple[Optional[str], Optional[str]]:
        form_token: Optional[str] = None
        cookie_token: Optional[str] = None

        if BeautifulSoup is not None:
            try:
                soup = BeautifulSoup(response.text, "html.parser")
                token_input = soup.select_one("input[name='__RequestVerificationToken']")
                if token_input and token_input.get("value"):
                    form_token = token_input.get("value")
            except Exception as parse_error:
                app.logger.debug(
                    "Failed to parse UNGM verification token via BeautifulSoup",
                    exc_info=parse_error,
                )

        if not cookie_token:
            cookie_token = response.cookies.get("__RequestVerificationToken")

        if not cookie_token:
            cookie_token = self.session.cookies.get("__RequestVerificationToken")

        if not cookie_token:
            for cookie in self.session.cookies:
                if cookie.name.startswith("__RequestVerificationToken") and cookie.value:
                    cookie_token = cookie.value
                    break

        text = _coerce_text(getattr(response, "text", ""))

        if not form_token:
            for pattern in (
                r"name=\"__RequestVerificationToken\"[^>]*value=\"([^\"]+)\"",
                r"__RequestVerificationToken\"\s*:\s*\"([^\"]+)\"",
                r"__RequestVerificationToken'\s*:\s*'([^']+)'",
            ):
                match = re.search(pattern, text)
                if match and match.group(1):
                    form_token = match.group(1)
                    break

        if not cookie_token:
            match = re.search(
                r"RequestVerificationToken['\"]\s*:\s*['\"]([^'\"]+)['\"]",
                text,
            )
            if match:
                combined = match.group(1)
                if ":" in combined:
                    cookie_token, request_token = combined.split(":", 1)
                    form_token = form_token or request_token
                elif not form_token:
                    form_token = combined

        return form_token, cookie_token

    def _bootstrap_ungm_tokens(
        self, referer_url: str, search_url: str
    ) -> Tuple[str, Optional[str]]:
        """Fetch the anti-forgery tokens required for UNGM AJAX search."""

        verification_token: Optional[str] = None
        verification_cookie: Optional[str] = None

        for bootstrap_url in (referer_url, search_url):
            try:
                bootstrap_response = self.session.get(bootstrap_url, timeout=30)
                bootstrap_response.raise_for_status()
            except requests.HTTPError as exc:
                response = getattr(exc, "response", None)
                should_skip = False
                if response is not None and response.status_code == 404:
                    preview = _preview_text(getattr(response, "text", ""))
                    target_url = getattr(response, "url", "") or ""
                    if "FileNotFound" in target_url or "FileNotFound" in preview:
                        should_skip = True
                if should_skip:
                    app.logger.debug(
                        "Skipping UNGM bootstrap URL that returned FileNotFound",
                        extra={
                            "url": bootstrap_url,
                            "status": response.status_code if response else None,
                        },
                    )
                    continue
                raise
            form_token, cookie_token = self._extract_ungm_tokens(bootstrap_response)
            if form_token and not verification_token:
                verification_token = form_token
            if cookie_token and not verification_cookie:
                verification_cookie = cookie_token
            if verification_token and verification_cookie:
                break

        if not verification_token:
            raise RuntimeError("UNGM crawling failed: unable to locate verification token")

        if verification_cookie:
            try:
                self.session.cookies.set(
                    "__RequestVerificationToken",
                    verification_cookie,
                    domain="www.ungm.org",
                    path="/",
                )
            except Exception:
                # Gracefully continue if cookie setting fails; the session may already hold it.
                pass

        return verification_token, verification_cookie

    def _post_ungm_search(
        self,
        url: str,
        referer_url: str,
        payload: Dict[str, Any],
        verification_token: str,
        verification_cookie: Optional[str],
    ) -> "requests.Response":
        """Submit the UNGM search request with resilient header fallbacks."""

        request_payload = dict(payload)
        request_payload["__RequestVerificationToken"] = verification_token

        base_headers = {
            "Referer": referer_url,
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://www.ungm.org",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }

        attempts: List[Tuple[str, Dict[str, str]]] = []

        header_with_cookie = dict(base_headers)
        header_with_cookie["Content-Type"] = "application/json; charset=UTF-8"
        header_with_cookie["RequestVerificationToken"] = (
            f"{verification_cookie}:{verification_token}"
            if verification_cookie
            else verification_token
        )
        header_with_cookie["X-RequestVerificationToken"] = verification_token
        attempts.append(("json-cookie", header_with_cookie))

        header_simple = dict(base_headers)
        header_simple["Content-Type"] = "application/json; charset=UTF-8"
        header_simple["RequestVerificationToken"] = verification_token
        header_simple["X-RequestVerificationToken"] = verification_token
        if verification_cookie:
            attempts.append(("json-simple", header_simple))
        elif header_simple["RequestVerificationToken"] != header_with_cookie["RequestVerificationToken"]:
            attempts.append(("json-simple", header_simple))

        last_response: Optional["requests.Response"] = None
        for attempt_name, headers in attempts:
            response = self.session.post(
                url,
                json=request_payload,
                headers=headers,
                timeout=30,
            )
            if response.ok:
                return response
            body = _coerce_text(getattr(response, "text", ""))
            if response.status_code == 404 and "FileNotFound" in body:
                app.logger.debug(
                    "UNGM search attempt returned FileNotFound",
                    extra={"attempt": attempt_name, "status": response.status_code},
                )
                last_response = response
                continue
            response.raise_for_status()
        if last_response is not None:
            last_response.raise_for_status()
        raise requests.HTTPError("UNGM search failed", response=last_response)

    def _process_ungm_json(self, payload: Any, keywords: Sequence[str]) -> int:
        """Convert a JSON UNGM payload into tender records."""

        records: Sequence[Any] = []
        if isinstance(payload, dict):
            for key in (
                "items",
                "results",
                "data",
                "notices",
                "Notices",
                "records",
                "Records",
                "rows",
                "Rows",
            ):
                value = payload.get(key)
                if isinstance(value, list):
                    records = value
                    break
        elif isinstance(payload, list):
            records = payload
        else:
            app.logger.warning(
                "UNGM JSON payload has unexpected shape",
                extra={"payload_type": type(payload).__name__},
            )
            return 0

        if not records:
            return 0

        count = 0
        for record in records:
            if not isinstance(record, dict):
                continue

            title = _extract_ungm_field(
                record,
                ("title", "noticeTitle", "notice_title", "Name", "TenderTitle"),
            )
            if not title:
                continue

            if keywords:
                title_lower = title.lower()
                if not any(keyword in title_lower for keyword in keywords):
                    continue

            reference_no = _extract_ungm_field(
                record,
                (
                    "reference",
                    "referenceNumber",
                    "reference_number",
                    "noticeReference",
                    "noticeNumber",
                    "refNo",
                    "referenceNo",
                ),
            )
            if not reference_no:
                continue

            description = _extract_ungm_field(
                record,
                ("description", "summary", "shortDescription"),
            ) or ""

            published_raw = _extract_ungm_field(
                record,
                ("published", "publishedDate", "datePublished", "publicationDate"),
            )
            deadline_raw = _extract_ungm_field(
                record,
                ("deadline", "deadlineDate", "submissionDeadline", "dateDeadline"),
            )
            agency = _extract_ungm_field(
                record,
                ("agency", "agencyName", "organisation", "organization", "buyer"),
            ) or ""
            notice_type = _extract_ungm_field(
                record,
                ("noticeType", "noticeTypeName", "type"),
            ) or ""
            country = _extract_ungm_field(
                record,
                ("country", "countryName", "location"),
            ) or ""
            detail_url = _extract_ungm_field(
                record,
                ("detailUrl", "noticeUrl", "url", "detailLink"),
            )
            if detail_url and detail_url.startswith("/"):
                detail_url = f"https://www.ungm.org{detail_url}"

            pub_date = _parse_ungm_date(published_raw)
            dead_date = _parse_ungm_date(deadline_raw)

            tender_data = {
                "site": "UNGM",
                "reference_no": reference_no,
                "title": title,
                "description": description,
                "published_date": pub_date,
                "deadline_date": dead_date,
                "organization": agency,
                "notice_type": notice_type,
                "country": country,
                "detail_url": detail_url,
            }

            if self.save_to_db(tender_data):
                count += 1

        return count

    def _should_retry_ted_without_fields(self, error: TedHTTPError) -> Optional[str]:
        """Return the error message when a TED field projection should be retried."""

        if not isinstance(error, TedHTTPError):
            return None
        if error.status_code != 400:
            return None

        message = ""
        if isinstance(error.payload, dict):
            message = str(
                error.payload.get("message")
                or error.payload.get("error")
                or error.payload.get("detail")
                or ""
            )
        if not message:
            message = str(error)

        if "unsupported" in message.lower() and "field" in message.lower():
            return message
        return None

    def get_search_config(self, site: str) -> str:
        """검색 설정 조회"""
        config = session.query(SearchConfig).filter_by(site=site).first()
        if config and config.query is not None:
            return config.query
        if site.upper() == "UNGM":
            return ", ".join(DEFAULT_UNGM_KEYWORDS)
        return ""

    def get_ted_settings(self) -> Dict[str, Any]:
        """Parse TED 검색 설정을 구조화된 dict로 반환"""
        config = session.query(SearchConfig).filter_by(site="TED").first()
        settings, _ = _parse_ted_config(config.query if config else None)
        return settings

    def get_sam_settings(self) -> Dict[str, Any]:
        """SAM.gov 검색 설정을 구조화된 dict로 반환"""
        config = session.query(SearchConfig).filter_by(site="SAM").first()
        return _parse_sam_config(config.query if config else None)
    
    def save_to_db(self, tender_data: Dict[str, Any]) -> bool:
        """입찰 정보를 DB에 저장 (UPSERT)"""
        try:
            data = dict(tender_data)
            data["last_updated"] = datetime.utcnow()

            stmt = sqlite_insert(Tender).values(**data)
            update_cols = {
                c: stmt.excluded[c]
                for c in data.keys()
                if c not in {"site", "reference_no"}
            }
            stmt = stmt.on_conflict_do_update(
                index_elements=["site", "reference_no"],
                set_=update_cols,
            )
            session.execute(stmt)
            session.commit()
            app.logger.info(
                f"Upserted tender: {data['site']} - {data['reference_no']}"
            )
            return True
        except Exception as e:
            session.rollback()
            app.logger.error(f"DB save error: {e}")
            return False
    
    def crawl_ungm(self) -> int:
        """UNGM 사이트 크롤링"""
        app.logger.info("Starting UNGM crawling...")

        if BeautifulSoup is None:
            app.logger.warning(
                "BeautifulSoup is not available; UNGM HTML responses may not be parsed"
            )

        # 날짜 설정 (어제부터 오늘까지)
        today = datetime.utcnow().strftime("%d-%b-%Y")
        yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%d-%b-%Y")

        url = 'https://www.ungm.org/Public/Notice/Search'
        referer_url = 'https://www.ungm.org/Public/Notice'
        payload = {
            "PageIndex": 1,
            "PageSize": 100,
            "Title": "",
            "Description": "",
            "Reference": "",
            "PublishedFrom": yesterday,
            "PublishedTo": today,
            "DeadlineFrom": "",
            "DeadlineTo": "",
            "Countries": [],
            "Agencies": [],
            "UNSPSCs": [],
            "NoticeTypes": [],
            "SortField": "DatePublished",
            "SortAscending": False,
            "NoticeSearchTotalLabelId": "noticeSearchTotal",
            "TypeOfCompetitions": []
        }

        config_raw = self.get_search_config('UNGM')
        keywords: List[str] = []
        if config_raw:
            tokens = _split_tokens(config_raw)
            if tokens:
                payload["Title"] = " ".join(tokens)
                payload["Description"] = " ".join(tokens)
                keywords = [token.lower() for token in tokens]

        try:
            verification_token, verification_cookie = self._bootstrap_ungm_tokens(
                referer_url, url
            )
        except requests.RequestException as exc:
            app.logger.error(f"UNGM crawling failed: {exc}")
            return 0
        except Exception as exc:
            app.logger.error(f"UNGM crawling failed: {exc}")
            return 0

        try:
            response = self._post_ungm_search(
                url,
                referer_url,
                payload,
                verification_token,
                verification_cookie,
            )
        except requests.HTTPError as exc:
            response = exc.response
            should_retry = False
            if response is not None and response.status_code == 404:
                body_preview = _preview_text(response.text)
                if "FileNotFound" in (response.url or "") or "FileNotFound" in body_preview:
                    should_retry = True
            if should_retry:
                app.logger.warning(
                    "UNGM search returned a FileNotFound page, refreshing verification token and retrying",
                    extra={"status": response.status_code if response else None},
                )
                try:
                    verification_token, verification_cookie = self._bootstrap_ungm_tokens(
                        referer_url, url
                    )
                    response = self._post_ungm_search(
                        url,
                        referer_url,
                        payload,
                        verification_token,
                        verification_cookie,
                    )
                except Exception as retry_exc:
                    detail = ""
                    if isinstance(retry_exc, requests.HTTPError) and retry_exc.response is not None:
                        detail = f" - {_preview_text(retry_exc.response.text)}"
                    app.logger.error(
                        f"UNGM crawling failed after retry: {retry_exc}{detail}"
                    )
                    return 0
            else:
                detail = ""
                if response is not None:
                    detail = f" - {_preview_text(response.text)}"
                app.logger.error(f"UNGM crawling failed: {exc}{detail}")
                return 0
        except requests.RequestException as exc:
            app.logger.error(f"UNGM crawling failed: {exc}")
            return 0

        content_type = (response.headers.get("Content-Type") or "").lower()
        body_preview = _coerce_text(getattr(response, "text", "")).lstrip()
        if "application/json" in content_type or body_preview.startswith("{") or body_preview.startswith("["):
            try:
                json_payload = response.json()
            except ValueError as exc:
                app.logger.error(
                    f"UNGM crawling failed: invalid JSON response - {exc}"
                )
                return 0
            count = self._process_ungm_json(json_payload, keywords)
            app.logger.info(
                f"UNGM crawling completed: {count} tenders processed (JSON response)"
            )
            return count

        if BeautifulSoup is None:
            app.logger.error(
                "UNGM crawling failed: BeautifulSoup is required to parse HTML responses"
            )
            return 0

        soup = BeautifulSoup(response.content, 'html.parser')
        rows = soup.select('.tableRow')

        count = 0

        for row in rows:
            cells = [cell.get_text(strip=True) for cell in row.select('.tableCell')]
            if len(cells) < 8:
                continue

            title = cells[1]
            deadline = cells[2]
            published_date = cells[3]
            agency = cells[4]
            notice_type = cells[5]
            reference_no = cells[6]
            country = cells[7]

            if keywords:
                title_lower = title.lower()
                if not any(keyword in title_lower for keyword in keywords):
                    continue

            pub_date = _parse_ungm_date(published_date)
            dead_date = _parse_ungm_date(deadline.split()[0] if deadline else deadline)

            detail_url = None
            link = row.select_one('a')
            if link and link.get('href'):
                detail_url = f"https://www.ungm.org{link['href']}"

            tender_data = {
                "site": "UNGM",
                "reference_no": reference_no,
                "title": title,
                "description": "",
                "published_date": pub_date,
                "deadline_date": dead_date,
                "organization": agency,
                "notice_type": notice_type,
                "country": country,
                "detail_url": detail_url,
            }

            if self.save_to_db(tender_data):
                count += 1

        app.logger.info(f"UNGM crawling completed: {count} tenders processed")
        return count

    def crawl_sam(self) -> int:
        """SAM.gov API를 통해 입찰 정보를 수집"""
        app.logger.info("Starting SAM.gov crawling...")

        api_key = os.getenv("SAM_API_KEY") or os.getenv("SAM_GOV_API_KEY")
        if not api_key:
            app.logger.warning(
                "SAM.gov crawling skipped: SAM_API_KEY environment variable is not set"
            )
            return 0

        sam_settings = self.get_sam_settings()

        base_url = "https://api.sam.gov/prod/opportunities/v1/search"
        keywords = sam_settings.get("keywords", [])
        posted_from = sam_settings.get("posted_from")
        posted_to = sam_settings.get("posted_to")
        notice_types = sam_settings.get("notice_types", [])
        set_asides = sam_settings.get("set_asides", [])
        naics = sam_settings.get("naics", [])
        sort_value = sam_settings.get("sort", "-modifiedDate")

        try:
            page_limit = int(sam_settings.get("limit", DEFAULT_SAM_PAGE_SIZE))
        except (TypeError, ValueError):
            page_limit = DEFAULT_SAM_PAGE_SIZE
        page_limit = max(1, min(page_limit, 100))

        try:
            max_pages = int(sam_settings.get("max_pages", DEFAULT_SAM_MAX_PAGES))
        except (TypeError, ValueError):
            max_pages = DEFAULT_SAM_MAX_PAGES
        max_pages = max(1, max_pages)

        params: Dict[str, Any] = {
            "api_key": api_key,
            "limit": page_limit,
            "offset": 0,
        }

        if keywords:
            params["q"] = " ".join(keywords)
        if posted_from:
            params["postedFrom"] = posted_from
        if posted_to:
            params["postedTo"] = posted_to
        if notice_types:
            params["noticeType"] = ",".join(notice_types)
        if set_asides:
            params["setAside"] = ",".join(set_asides)
        if naics:
            params["naics"] = ",".join(naics)
        if sort_value:
            params["sort"] = sort_value

        total_saved = 0
        pages_fetched = 0

        while pages_fetched < max_pages:
            try:
                response = self.session.get(base_url, params=params, timeout=30)
                response.raise_for_status()
            except requests.HTTPError as exc:
                masked = _mask_api_key(str(exc))
                detail = ""
                if exc.response is not None:
                    try:
                        detail_payload = json.dumps(exc.response.json())[:200]
                    except ValueError:
                        detail_payload = _preview_text(exc.response.text)
                    if detail_payload:
                        detail = f" - {detail_payload}"
                app.logger.error(f"SAM.gov crawling failed: {masked}{detail}")
                return total_saved
            except requests.RequestException as exc:
                app.logger.error(
                    f"SAM.gov crawling failed: {_mask_api_key(str(exc))}"
                )
                return total_saved

            try:
                payload = response.json()
            except ValueError as exc:
                app.logger.error(
                    f"SAM.gov crawling failed: invalid JSON response - {exc}"
                )
                return total_saved

            notices = (
                payload.get("opportunitiesData")
                or payload.get("opportunities")
                or payload.get("data")
                or []
            )

            if not isinstance(notices, list):
                app.logger.error("SAM.gov crawling failed: unexpected payload structure")
                return total_saved

            if not notices:
                break

            for notice in notices:
                if not isinstance(notice, dict):
                    continue

                reference_no = (
                    notice.get("solicitationNumber")
                    or notice.get("noticeId")
                    or notice.get("id")
                    or ""
                )
                if not reference_no:
                    continue

                title = notice.get("title") or notice.get("subject") or ""
                description = (
                    notice.get("description")
                    or notice.get("descriptionText")
                    or notice.get("summary")
                    or ""
                )
                published_date = _parse_iso_datetime(
                    notice.get("postedDate") or notice.get("publishDate")
                )
                deadline_date = _parse_iso_datetime(
                    notice.get("responseDate")
                    or notice.get("closeDate")
                    or notice.get("dueDate")
                )
                organization = (
                    notice.get("organizationName")
                    or (notice.get("organizationHierarchy") or {}).get(
                        "organizationName"
                    )
                    or (notice.get("office") or {}).get("name")
                    or ""
                )
                notice_type = notice.get("type") or notice.get("noticeType") or ""

                place = notice.get("placeOfPerformance") or {}
                if isinstance(place, dict):
                    country = (
                        place.get("country")
                        or (place.get("address") or {}).get("country")
                        or ""
                    )
                else:
                    country = ""

                detail_url = notice.get("uiLink") or notice.get("link")

                tender_data = {
                    "site": "SAM",
                    "reference_no": str(reference_no),
                    "title": title or "(No title)",
                    "description": description,
                    "published_date": published_date,
                    "deadline_date": deadline_date,
                    "organization": organization,
                    "notice_type": notice_type,
                    "country": country,
                    "detail_url": detail_url,
                }

                if self.save_to_db(tender_data):
                    total_saved += 1

            total_records = payload.get("totalRecords")
            params["offset"] = params.get("offset", 0) + page_limit
            pages_fetched += 1

            if total_records is not None:
                try:
                    if params["offset"] >= int(total_records):
                        break
                except (TypeError, ValueError):
                    pass

            if len(notices) < page_limit:
                break

        app.logger.info(
            f"SAM.gov crawling completed: {total_saved} tenders processed"
        )
        return total_saved

    def crawl_ted(self) -> int:
        """TED 사이트 크롤링"""
        app.logger.info("Starting TED crawling...")

        ted_settings = self.get_ted_settings()
        search_query = ted_settings.get("query") or _default_ted_query()
        fields_config = ted_settings.get("fields")
        if isinstance(fields_config, list):
            fields_source = fields_config
        else:
            fields_source = list(DEFAULT_TED_FIELDS)
        limit = ted_settings.get("limit", 100)
        sort_field = ted_settings.get("sort_field", "publication-date")
        sort_order = ted_settings.get("sort_order", "DESC")
        page_number = ted_settings.get("page", 1)

        try:
            limit_value = int(limit)
            if limit_value <= 0:
                limit_value = 100
        except (TypeError, ValueError):
            limit_value = 100

        try:
            page_value = int(page_number)
            if page_value <= 0:
                page_value = 1
        except (TypeError, ValueError):
            page_value = 1

        sort_order_value = str(sort_order).lower() if sort_order else "desc"

        field_tokens: List[str] = []
        seen_fields: set[str] = set()
        for raw_field in fields_source:
            field_name = str(raw_field).strip()
            if not field_name:
                continue
            normalised = field_name.replace(".", "-")
            if normalised not in seen_fields:
                field_tokens.append(normalised)
                seen_fields.add(normalised)
        if not field_tokens:
            for default_field in DEFAULT_TED_FIELDS:
                cleaned = str(default_field).strip().replace(".", "-")
                if cleaned and cleaned not in seen_fields:
                    field_tokens.append(cleaned)
                    seen_fields.add(cleaned)

        mode_raw = ted_settings.get("mode")
        try:
            mode_value = str(mode_raw).strip().lower() if mode_raw is not None else "page"
        except Exception:
            mode_value = "page"
        if mode_value not in {"page", "iteration"}:
            mode_value = "page"

        fields_log = ",".join(field_tokens) if field_tokens else "(default)"
        app.logger.info(
            "Prepared TED search parameters",
            extra={
                "fields": fields_log,
                "limit": limit_value,
                "sort": sort_field,
                "order": sort_order_value,
                "page": page_value,
                "mode": mode_value,
            },
        )

        def _execute_ted_search(
            active_fields: Optional[List[str]],
        ) -> Iterator[Any]:
            fields_label = ",".join(active_fields) if active_fields else "(default)"
            search_kwargs = {
                "q": search_query,
                "fields": active_fields,
                "sort_field": sort_field,
                "sort_order": sort_order_value,
            }

            if mode_value == "iteration":
                app.logger.info(
                    "Executing TED iteration crawl",
                    extra={
                        "batch_limit": limit_value,
                        "sort": sort_field,
                        "order": sort_order_value,
                        "fields": fields_label,
                    },
                )
                return ted_iterate_all(
                    batch_limit=limit_value,
                    **search_kwargs,
                )

            app.logger.info(
                "Requesting TED page",
                extra={
                    "page": page_value,
                    "limit": limit_value,
                    "sort": sort_field,
                    "order": sort_order_value,
                    "fields": fields_label,
                },
            )
            page_result_local = ted_search_once(
                page=page_value,
                limit=limit_value,
                **search_kwargs,
            )
            app.logger.info(
                "Received TED page",
                extra={
                    "page": page_result_local.page,
                    "count": page_result_local.count,
                    "total": page_result_local.total,
                    "has_next": bool(page_result_local.next_page_token),
                    "fields": fields_label,
                },
            )
            return iter(page_result_local.notices)

        try:
            notice_iterable = _execute_ted_search(field_tokens)
        except TedHTTPError as exc:
            retry_message = self._should_retry_ted_without_fields(exc)
            if retry_message:
                app.logger.warning(
                    "TED API rejected field projection, retrying without explicit fields",
                    extra={
                        "fields": fields_log,
                        "error": retry_message[:180],
                    },
                )
                notice_iterable = _execute_ted_search([])
            else:
                raise

        try:
            count = 0

            for notice_obj in notice_iterable:
                notice = notice_obj.to_dict() if hasattr(notice_obj, "to_dict") else dict(notice_obj)
                reference_no = (
                    notice.get("publicationNumber")
                    or notice.get("publication-number")
                    or ""
                )
                title = notice.get("title", "")
                description = (
                    notice.get("description")
                    or notice.get("shortDescription")
                    or ""
                )
                pub_date_str = (
                    notice.get("publicationDate")
                    or notice.get("publication-date")
                    or ""
                )
                deadline_str = (
                    notice.get("deadlineDate")
                    or notice.get("deadline-date")
                    or ""
                )
                buyer_country = (
                    notice.get("buyerCountry")
                    or notice.get("buyer-country")
                    or notice.get("place-of-performance.country")
                    or notice.get("place-of-performance-country")
                    or notice.get("placeOfPerformanceCountry")
                    or ""
                )
                buyer_name = (
                    notice.get("buyerName")
                    or notice.get("buyer-name")
                    or ""
                )
                notice_type = (
                    notice.get("noticeType")
                    or notice.get("notice-type")
                    or ""
                )

                # 날짜 파싱
                try:
                    published_date = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00")) if pub_date_str else None
                except Exception:
                    published_date = None

                try:
                    deadline_date = datetime.fromisoformat(deadline_str.replace("Z", "+00:00")) if deadline_str else None
                except Exception:
                    deadline_date = None

                detail_url = f"https://ted.europa.eu/udl?uri=TED:NOTICE:{reference_no}" if reference_no else None

                tender_data = {
                    "site": "TED",
                    "reference_no": reference_no,
                    "title": title,
                    "description": description,
                    "published_date": published_date,
                    "deadline_date": deadline_date,
                    "organization": buyer_name,
                    "notice_type": notice_type,
                    "country": buyer_country,
                    "detail_url": detail_url,
                }

                if self.save_to_db(tender_data):
                    count += 1

            app.logger.info(f"TED crawling completed: {count} tenders processed")
            return count

        except TedHTTPError as exc:
            detail = ""
            if exc.payload:
                try:
                    detail_payload = json.dumps(exc.payload)[:200]
                except (TypeError, ValueError):
                    detail_payload = str(exc.payload)[:200]
                detail = f" - {detail_payload}"
            app.logger.error(f"TED crawling failed: {exc}{detail}")
            return 0
        except TedClientError as exc:
            app.logger.error(f"TED crawling failed: {exc}")
            return 0
        except requests.RequestException as exc:
            app.logger.error(f"TED crawling failed: {exc}")
            return 0
        except Exception as exc:
            app.logger.error(f"TED crawling failed: {exc}")
            return 0

# 크롤러 인스턴스
crawler = TenderCrawler()

def crawl_all() -> Dict[str, Any]:
    """전체 크롤링 작업"""
    app.logger.info("Starting scheduled crawling job...")
    start_time = datetime.now()

    ungm_count = crawler.crawl_ungm()
    time.sleep(2)  # 사이트 간 간격
    sam_count = crawler.crawl_sam()
    time.sleep(2)
    ted_count = crawler.crawl_ted()

    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()

    app.logger.info(
        "Crawling job completed in %.2fs - UNGM: %s, SAM: %s, TED: %s",
        duration,
        ungm_count,
        sam_count,
        ted_count,
    )

    return {
        "ungm": ungm_count,
        "sam": sam_count,
        "ted": ted_count,
        "duration": duration,
    }

# 스케줄러 설정
scheduler = BackgroundScheduler() if BackgroundScheduler else None
if scheduler:
    scheduler.add_job(func=crawl_all, trigger='cron', hour=2, minute=0, id='daily_crawl')

# REST API 엔드포인트
@app.route('/api/tenders', methods=['GET'])
def get_tenders():
    """입찰 정보 API"""
    site = request.args.get('site')
    keyword = request.args.get('keyword')
    since = request.args.get('since')
    limit = int(request.args.get('limit', 100))
    
    query = session.query(Tender)

    if site:
        query = query.filter_by(site=site.upper())

    if since:
        try:
            dt = datetime.fromisoformat(since)
            query = query.filter(Tender.published_date >= dt)
        except:
            pass

    if keyword:
        query = query.filter(Tender.title.ilike(f"%{keyword}%"))

    total = query.count()
    fields = (
        Tender.site,
        Tender.reference_no,
        Tender.title,
        Tender.description,
        Tender.published_date,
        Tender.deadline_date,
        Tender.organization,
        Tender.notice_type,
        Tender.country,
        Tender.detail_url,
        Tender.last_updated,
    )
    tenders = (
        query.options(load_only(*fields))
        .order_by(Tender.published_date.desc())
        .limit(limit)
        .all()
    )

    result = [
        {
            "site": t.site,
            "reference_no": t.reference_no,
            "title": t.title,
            "description": t.description,
            "published_date": t.published_date.isoformat() if t.published_date else None,
            "deadline_date": t.deadline_date.isoformat() if t.deadline_date else None,
            "organization": t.organization,
            "notice_type": t.notice_type,
            "country": t.country,
            "detail_url": t.detail_url,
            "last_updated": t.last_updated.isoformat() if t.last_updated else None,
        }
        for t in tenders
    ]

    return jsonify({"total": total, "tenders": result})

@app.route('/api/crawl', methods=['POST'])
def manual_crawl():
    """수동 크롤링 트리거"""
    try:
        results = crawl_all()
        return jsonify(
            {
                "status": "success",
                "message": "Crawling completed",
                "results": results,
            }
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# Web UI 라우트
@app.route('/')
def index():
    """메인 페이지"""
    return render_template('index.html')

@app.route('/search-config', methods=['GET', 'POST'])
def search_config():
    """검색식 관리"""
    if request.method == 'POST':
        ungm_keywords = request.form.get('ungm_keywords', '').strip()

        today = datetime.utcnow().date()
        default_ted_to = today.isoformat()
        default_ted_from = (today - timedelta(days=DEFAULT_TED_LOOKBACK_DAYS)).isoformat()
        default_sam_to = today.isoformat()
        default_sam_from = (today - timedelta(days=DEFAULT_SAM_LOOKBACK_DAYS)).isoformat()

        ted_date_from = request.form.get('ted_date_from', '').strip() or default_ted_from
        ted_date_to = request.form.get('ted_date_to', '').strip() or default_ted_to
        ted_countries = [token.upper() for token in _split_tokens(request.form.get('ted_countries', ''))]
        ted_cpv = _split_tokens(request.form.get('ted_cpv', ''))
        ted_keywords_input = request.form.get('ted_keywords', '')
        ted_keywords = _split_tokens(ted_keywords_input)
        ted_form_types = [token.upper() for token in _split_tokens(request.form.get('ted_form_types', ''))]
        ted_fields_raw = request.form.get('ted_fields', '')
        ted_fields = _split_tokens(ted_fields_raw)
        if not ted_fields:
            ted_fields = list(DEFAULT_TED_FIELDS)

        try:
            ted_limit = int(request.form.get('ted_limit', 100))
            if ted_limit <= 0:
                ted_limit = 100
        except (TypeError, ValueError):
            ted_limit = 100

        try:
            ted_page = int(request.form.get('ted_page', 1))
            if ted_page <= 0:
                ted_page = 1
        except (TypeError, ValueError):
            ted_page = 1

        ted_sort_field = request.form.get('ted_sort_field', 'publication-date').strip() or 'publication-date'
        ted_sort_order = request.form.get('ted_sort_order', 'DESC').strip().upper() or 'DESC'
        ted_mode = request.form.get('ted_mode', 'page').strip() or 'page'

        ted_query = build_ted_query(
            date_from=ted_date_from,
            date_to=ted_date_to,
            countries=ted_countries,
            cpv_prefixes=ted_cpv,
            keywords=ted_keywords,
            form_types=ted_form_types,
        )

        ted_payload = {
            "version": 1,
            "query": ted_query,
            "builder": {
                "date_from": ted_date_from,
                "date_to": ted_date_to,
                "countries": ted_countries,
                "cpv_prefixes": ted_cpv,
                "keywords": ted_keywords,
                "form_types": ted_form_types,
            },
            "fields": ted_fields,
            "limit": ted_limit,
            "sort_field": ted_sort_field,
            "sort_order": ted_sort_order,
            "mode": ted_mode,
            "page": ted_page,
        }
        ted_payload_text = json.dumps(ted_payload, ensure_ascii=False)

        sam_keywords_input = request.form.get('sam_keywords', '')
        sam_keywords = _split_tokens(sam_keywords_input)
        if not sam_keywords:
            sam_keywords = list(DEFAULT_SAM_KEYWORDS)
        sam_posted_from = request.form.get('sam_posted_from', '').strip() or default_sam_from
        sam_posted_to = request.form.get('sam_posted_to', '').strip() or default_sam_to
        sam_notice_types = [token.upper() for token in _split_tokens(request.form.get('sam_notice_types', ''))]
        sam_set_asides = [token.upper() for token in _split_tokens(request.form.get('sam_set_asides', ''))]
        sam_naics = _split_tokens(request.form.get('sam_naics', ''))

        try:
            sam_limit = int(request.form.get('sam_limit', DEFAULT_SAM_PAGE_SIZE))
        except (TypeError, ValueError):
            sam_limit = DEFAULT_SAM_PAGE_SIZE
        sam_limit = max(1, min(sam_limit, 100))

        try:
            sam_max_pages = int(request.form.get('sam_max_pages', DEFAULT_SAM_MAX_PAGES))
        except (TypeError, ValueError):
            sam_max_pages = DEFAULT_SAM_MAX_PAGES
        sam_max_pages = max(1, sam_max_pages)

        sam_sort = request.form.get('sam_sort', '-modifiedDate') or '-modifiedDate'
        sam_sort = sam_sort.strip() or '-modifiedDate'

        sam_payload = {
            "version": 1,
            "keywords": sam_keywords,
            "posted_from": sam_posted_from,
            "posted_to": sam_posted_to,
            "notice_types": sam_notice_types,
            "set_asides": sam_set_asides,
            "naics": sam_naics,
            "limit": sam_limit,
            "max_pages": sam_max_pages,
            "sort": sam_sort,
        }
        sam_payload_text = json.dumps(sam_payload, ensure_ascii=False)

        # UNGM 설정 저장
        ungm_config = session.query(SearchConfig).filter_by(site='UNGM').first()
        if ungm_config:
            ungm_config.query = ungm_keywords
            ungm_config.last_updated = datetime.utcnow()
        else:
            ungm_config = SearchConfig(site='UNGM', query=ungm_keywords, last_updated=datetime.utcnow())
            session.add(ungm_config)

        # TED 설정 저장
        ted_config = session.query(SearchConfig).filter_by(site='TED').first()
        if ted_config:
            ted_config.query = ted_payload_text
            ted_config.last_updated = datetime.utcnow()
        else:
            ted_config = SearchConfig(site='TED', query=ted_payload_text, last_updated=datetime.utcnow())
            session.add(ted_config)

        sam_config = session.query(SearchConfig).filter_by(site='SAM').first()
        if sam_config:
            sam_config.query = sam_payload_text
            sam_config.last_updated = datetime.utcnow()
        else:
            sam_config = SearchConfig(
                site='SAM', query=sam_payload_text, last_updated=datetime.utcnow()
            )
            session.add(sam_config)

        session.commit()
        app.logger.info("Search configurations updated")
        return redirect(url_for('search_config'))

    # GET 요청 - 현재 설정 로드
    ungm_config = session.query(SearchConfig).filter_by(site='UNGM').first()
    ted_config = session.query(SearchConfig).filter_by(site='TED').first()
    sam_config = session.query(SearchConfig).filter_by(site='SAM').first()

    if ungm_config is None:
        ungm_keywords_value = ", ".join(DEFAULT_UNGM_KEYWORDS)
    else:
        ungm_keywords_value = ungm_config.query or ''
    ungm_keyword_list = _split_tokens(ungm_keywords_value)
    ungm_preview_link = build_ungm_deeplink(keywords=ungm_keyword_list)

    ted_settings, ted_builder = _parse_ted_config(ted_config.query if ted_config else None)
    sam_settings = _parse_sam_config(sam_config.query if sam_config else None)

    today = datetime.utcnow().date()
    default_ted_to = today.isoformat()
    default_ted_from = (today - timedelta(days=DEFAULT_TED_LOOKBACK_DAYS)).isoformat()
    default_sam_to = today.isoformat()
    default_sam_from = (today - timedelta(days=DEFAULT_SAM_LOOKBACK_DAYS)).isoformat()

    builder_data = {
        "date_from": str(ted_builder.get("date_from") or default_ted_from),
        "date_to": str(ted_builder.get("date_to") or default_ted_to),
        "countries": [value.upper() for value in _ensure_list(ted_builder.get("countries"))],
        "cpv_prefixes": _ensure_list(ted_builder.get("cpv_prefixes")),
        "keywords": _ensure_list(ted_builder.get("keywords")),
        "form_types": [value.upper() for value in _ensure_list(ted_builder.get("form_types"))],
    }

    ted_fields_text = "\n".join(ted_settings.get("fields", DEFAULT_TED_FIELDS))
    ted_limit_value = ted_settings.get("limit", 100)
    ted_sort_field = ted_settings.get("sort_field", "publication-date")
    ted_sort_order = (ted_settings.get("sort_order") or "DESC").upper()
    ted_mode = ted_settings.get("mode", "page")
    ted_page_value = ted_settings.get("page", 1)

    sam_keywords_value = ", ".join(sam_settings.get("keywords", DEFAULT_SAM_KEYWORDS))
    sam_posted_from_value = sam_settings.get("posted_from") or default_sam_from
    sam_posted_to_value = sam_settings.get("posted_to") or default_sam_to
    sam_notice_types_value = ", ".join(sam_settings.get("notice_types", []))
    sam_set_asides_value = ", ".join(sam_settings.get("set_asides", []))
    sam_naics_value = ", ".join(sam_settings.get("naics", []))
    sam_limit_value = sam_settings.get("limit", DEFAULT_SAM_PAGE_SIZE)
    sam_max_pages_value = sam_settings.get("max_pages", DEFAULT_SAM_MAX_PAGES)
    sam_sort_value = sam_settings.get("sort", "-modifiedDate")

    return render_template(
        'search_config.html',
        ungm_keywords=ungm_keywords_value,
        ungm_preview_link=ungm_preview_link,
        ted_query_preview=ted_settings.get("query", _default_ted_query()),
        ted_date_from=builder_data["date_from"],
        ted_date_to=builder_data["date_to"],
        ted_countries=", ".join(builder_data["countries"]),
        ted_cpv=", ".join(builder_data["cpv_prefixes"]),
        ted_keywords=", ".join(builder_data["keywords"]),
        ted_form_types=", ".join(builder_data["form_types"]),
        ted_fields_text=ted_fields_text,
        ted_limit=ted_limit_value,
        ted_sort_field=ted_sort_field,
        ted_sort_order=ted_sort_order,
        ted_mode=ted_mode,
        ted_page=str(ted_page_value),
        ted_default_fields=DEFAULT_TED_FIELDS,
        sam_keywords=sam_keywords_value,
        sam_posted_from=sam_posted_from_value,
        sam_posted_to=sam_posted_to_value,
        sam_notice_types=sam_notice_types_value,
        sam_set_asides=sam_set_asides_value,
        sam_naics=sam_naics_value,
        sam_limit=sam_limit_value,
        sam_max_pages=sam_max_pages_value,
        sam_sort=sam_sort_value,
        default_sam_keywords=", ".join(DEFAULT_SAM_KEYWORDS),
    )

@app.route('/tenders')
def tender_list():
    """입찰 목록 조회"""
    page = int(request.args.get('page', 1))
    per_page = 50
    offset = (page - 1) * per_page
    
    site_filter = request.args.get('site', '')
    
    query = session.query(Tender)
    if site_filter:
        query = query.filter_by(site=site_filter)
    
    total = query.count()
    tenders = query.order_by(Tender.published_date.desc()).offset(offset).limit(per_page).all()
    
    return render_template('tenders.html', 
                         tenders=tenders, 
                         page=page, 
                         total=total, 
                         per_page=per_page,
                         site_filter=site_filter)

@app.route('/logs')
def view_logs():
    """로그 조회"""
    try:
        with open('mcp_server.log', 'r', encoding='utf-8') as f:
            logs = f.readlines()[-100:]  # 최근 100줄
        return render_template('logs.html', logs=logs)
    except:
        return render_template('logs.html', logs=[])

if __name__ == '__main__':
    try:
        if scheduler:
            scheduler.start()
        app.logger.info("MCP Tender Server started")
        app.run(debug=True, host='0.0.0.0', port=5000)
    except KeyboardInterrupt:
        if scheduler:
            scheduler.shutdown()
        app.logger.info("MCP Tender Server stopped")
