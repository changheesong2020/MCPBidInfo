import os
import json
import time
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any, Tuple

try:
    import requests
    from requests.adapters import HTTPAdapter, Retry
except Exception:  # pragma: no cover - optional dependency
    requests = None
    HTTPAdapter = Retry = None
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
    build_query as build_ted_query,
)
from ted_ungm_search.ungm_helpers import build_ungm_deeplink

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

DEFAULT_TED_QUERY = "FT=(solar OR wind OR renewable OR energy)"
DEFAULT_TED_FIELDS = list(TED_DEFAULT_FIELDS)
for _extra_field in [
    "description",
    "deadline-date",
    "buyer-country",
    "notice-type",
]:
    if _extra_field not in DEFAULT_TED_FIELDS:
        DEFAULT_TED_FIELDS.append(_extra_field)


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
    settings: Dict[str, Any] = {
        "query": DEFAULT_TED_QUERY,
        "fields": list(DEFAULT_TED_FIELDS),
        "limit": 100,
        "sort_field": "publication-date",
        "sort_order": "DESC",
        "mode": "page",
        "page": 1,
    }
    builder: Dict[str, Any] = {}

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
        settings["sort_order"] = sort_order.strip()

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
        builder = builder_candidate

    return settings, builder

class TenderCrawler:
    def __init__(self):
        if requests is None:
            raise RuntimeError("requests library is required for crawling")
        self.session = requests.Session()
        retry_strategy = Retry(
            total=5,
            backoff_factor=0.3,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
    
    def get_search_config(self, site: str) -> str:
        """검색 설정 조회"""
        config = session.query(SearchConfig).filter_by(site=site).first()
        return config.query or "" if config else ""

    def get_ted_settings(self) -> Dict[str, Any]:
        """Parse TED 검색 설정을 구조화된 dict로 반환"""
        config = session.query(SearchConfig).filter_by(site="TED").first()
        settings, _ = _parse_ted_config(config.query if config else None)
        return settings
    
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
        
        # 날짜 설정 (어제부터 오늘까지)
        today = datetime.utcnow().strftime("%d-%b-%Y")
        yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%d-%b-%Y")
        
        url = 'https://www.ungm.org/Public/Notice/Search'
        payload = {
            "PageIndex": 0,
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
        
        try:
            response = self.session.post(url, json=payload, timeout=30)
            response.raise_for_status()
            
            # BeautifulSoup으로 파싱 (html5lib 파서 사용)
            soup = BeautifulSoup(response.content, 'html5lib')
            rows = soup.select('.tableRow')
            
            count = 0
            keywords = self.get_search_config('UNGM').split(',') if self.get_search_config('UNGM') else []
            keywords = [k.strip().lower() for k in keywords if k.strip()]
            
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
                
                # 키워드 필터링
                if keywords:
                    title_lower = title.lower()
                    if not any(keyword in title_lower for keyword in keywords):
                        continue
                
                # 날짜 파싱
                try:
                    pub_date = datetime.strptime(published_date, "%d-%b-%Y") if published_date else None
                except:
                    pub_date = None
                
                try:
                    dead_date = datetime.strptime(deadline.split()[0], "%d-%b-%Y") if deadline else None
                except:
                    dead_date = None
                
                # 상세 URL 추출
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
                    "detail_url": detail_url
                }
                
                if self.save_to_db(tender_data):
                    count += 1
            
            app.logger.info(f"UNGM crawling completed: {count} tenders processed")
            return count
            
        except Exception as e:
            app.logger.error(f"UNGM crawling failed: {e}")
            return 0
    
    def crawl_ted(self) -> int:
        """TED 사이트 크롤링"""
        app.logger.info("Starting TED crawling...")
        
        url = "https://api.ted.europa.eu/v3/notices/search"
        
        ted_settings = self.get_ted_settings()
        search_query = ted_settings.get("query") or DEFAULT_TED_QUERY
        fields = ted_settings.get("fields") or list(DEFAULT_TED_FIELDS)
        if not isinstance(fields, list):
            fields = list(DEFAULT_TED_FIELDS)
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

        sort_order_value = str(sort_order).upper() if sort_order else "DESC"

        payload = {
            "query": search_query,
            "fields": fields,
            "limit": limit_value,
            "scope": "ACTIVE",
            "sortBy": sort_field,
            "order": sort_order_value,
            "page": page_value,
        }

        app.logger.info(
            "Prepared TED search payload",
            extra={
                "fields": ",".join(fields),
                "limit": limit_value,
                "sort": sort_field,
                "order": sort_order_value,
                "page": page_value,
            },
        )
        
        try:
            response = self.session.post(url, json=payload, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            count = 0
            notices = data.get("notices", [])
            
            for notice in notices:
                reference_no = notice.get("publicationNumber", "")
                title = notice.get("title", "")
                description = notice.get("description", "")
                pub_date_str = notice.get("publicationDate", "")
                deadline_str = notice.get("deadlineDate", "")
                buyer_country = notice.get("buyerCountry", "")
                buyer_name = notice.get("buyerName", "")
                notice_type = notice.get("noticeType", "")
                
                # 날짜 파싱
                try:
                    published_date = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00")) if pub_date_str else None
                except:
                    published_date = None
                
                try:
                    deadline_date = datetime.fromisoformat(deadline_str.replace("Z", "+00:00")) if deadline_str else None
                except:
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
                    "detail_url": detail_url
                }
                
                if self.save_to_db(tender_data):
                    count += 1
            
            app.logger.info(f"TED crawling completed: {count} tenders processed")
            return count
            
        except Exception as e:
            app.logger.error(f"TED crawling failed: {e}")
            return 0

# 크롤러 인스턴스
crawler = TenderCrawler()

def crawl_all():
    """전체 크롤링 작업"""
    app.logger.info("Starting scheduled crawling job...")
    start_time = datetime.now()
    
    ungm_count = crawler.crawl_ungm()
    time.sleep(2)  # 사이트 간 간격
    ted_count = crawler.crawl_ted()
    
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()
    
    app.logger.info(f"Crawling job completed in {duration:.2f}s - UNGM: {ungm_count}, TED: {ted_count}")

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
        crawl_all()
        return jsonify({"status": "success", "message": "Crawling completed"})
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
        default_ted_from = (today - timedelta(days=30)).isoformat()

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

        session.commit()
        app.logger.info("Search configurations updated")
        return redirect(url_for('search_config'))

    # GET 요청 - 현재 설정 로드
    ungm_config = session.query(SearchConfig).filter_by(site='UNGM').first()
    ted_config = session.query(SearchConfig).filter_by(site='TED').first()

    ungm_keywords_value = ungm_config.query if ungm_config else ''
    ungm_keyword_list = _split_tokens(ungm_keywords_value)
    ungm_preview_link = build_ungm_deeplink(keywords=ungm_keyword_list)

    ted_settings, ted_builder = _parse_ted_config(ted_config.query if ted_config else None)

    today = datetime.utcnow().date()
    default_ted_to = today.isoformat()
    default_ted_from = (today - timedelta(days=30)).isoformat()

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

    return render_template(
        'search_config.html',
        ungm_keywords=ungm_keywords_value,
        ungm_preview_link=ungm_preview_link,
        ted_query_preview=ted_settings.get("query", DEFAULT_TED_QUERY),
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
