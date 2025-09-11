import os
import json
import time
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any

import requests
from requests.adapters import HTTPAdapter, Retry
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, render_template, redirect, url_for
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import Column, String, DateTime, Integer, Text, create_engine, UniqueConstraint
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, scoped_session
from logging.handlers import RotatingFileHandler

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

class TenderCrawler:
    def __init__(self):
        self.session = requests.Session()
        retry_strategy = Retry(
            total=5,
            backoff_factor=0.3,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET", "POST"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
    
    def get_search_config(self, site: str) -> str:
        """검색 설정 조회"""
        config = session.query(SearchConfig).filter_by(site=site).first()
        return config.query if config else ""
    
    def save_to_db(self, tender_data: Dict[str, Any]) -> bool:
        """입찰 정보를 DB에 저장 (UPSERT)"""
        try:
            existing = session.query(Tender).filter_by(
                site=tender_data["site"], 
                reference_no=tender_data["reference_no"]
            ).first()
            
            if existing:
                # 기존 레코드 업데이트
                for key, value in tender_data.items():
                    if key != "site" and key != "reference_no":
                        setattr(existing, key, value)
                existing.last_updated = datetime.utcnow()
                app.logger.info(f"Updated tender: {tender_data['site']} - {tender_data['reference_no']}")
            else:
                # 신규 레코드 생성
                tender_data['last_updated'] = datetime.utcnow()
                tender = Tender(**tender_data)
                session.add(tender)
                app.logger.info(f"Added new tender: {tender_data['site']} - {tender_data['reference_no']}")
            
            session.commit()
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
        
        # 검색식 구성
        search_query = self.get_search_config('TED')
        if not search_query:
            search_query = 'FT=(solar OR wind OR renewable OR energy)'
        
        payload = {
            "query": search_query,
            "fields": [
                "publication-number", "title", "description", 
                "publication-date", "deadline-date", "buyer-country", 
                "buyer-name", "notice-type"
            ],
            "limit": 100,
            "scope": "ACTIVE",
            "sortBy": "publication-date",
            "order": "DESC"
        }
        
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
scheduler = BackgroundScheduler()
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
    
    tenders = query.order_by(Tender.published_date.desc()).limit(limit).all()
    
    result = []
    for t in tenders:
        if keyword and keyword.lower() not in (t.title or "").lower():
            continue
            
        result.append({
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
            "last_updated": t.last_updated.isoformat() if t.last_updated else None
        })
    
    return jsonify({
        "total": len(result),
        "tenders": result
    })

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
        ungm_keywords = request.form.get('ungm_keywords', '')
        ted_query = request.form.get('ted_query', '')
        
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
            ted_config.query = ted_query
            ted_config.last_updated = datetime.utcnow()
        else:
            ted_config = SearchConfig(site='TED', query=ted_query, last_updated=datetime.utcnow())
            session.add(ted_config)
        
        session.commit()
        app.logger.info("Search configurations updated")
        return redirect(url_for('search_config'))
    
    # GET 요청 - 현재 설정 로드
    ungm_config = session.query(SearchConfig).filter_by(site='UNGM').first()
    ted_config = session.query(SearchConfig).filter_by(site='TED').first()
    
    return render_template('search_config.html', 
                         ungm_keywords=ungm_config.query if ungm_config else '',
                         ted_query=ted_config.query if ted_config else '')

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
        scheduler.start()
        app.logger.info("MCP Tender Server started")
        app.run(debug=True, host='0.0.0.0', port=5000)
    except KeyboardInterrupt:
        scheduler.shutdown()
        app.logger.info("MCP Tender Server stopped")