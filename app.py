diff --git a/app.py b/app.py
index 20f3a7cb36574d8b012228ad05b2d2e5e150d2ea..f5bc3e9fc9c0835803795f60de8171b4e78da8ce 100644
--- a/app.py
+++ b/app.py
@@ -81,149 +81,219 @@ app.config['SECRET_KEY'] = 'your-secret-key-here'
 
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
 
-DEFAULT_TED_QUERY = "FT=(solar OR wind OR renewable OR energy)"
+DEFAULT_UNGM_KEYWORDS = ["PCR", "reagent", "diagnostic"]
+DEFAULT_TED_LOOKBACK_DAYS = 30
+DEFAULT_TED_COUNTRIES = ["DE", "FR"]
+DEFAULT_TED_CPV_PREFIXES = ["33*"]
+DEFAULT_TED_KEYWORDS = ["PCR", "reagent", "diagnostic"]
+DEFAULT_TED_FORM_TYPES = ["F15"]
 DEFAULT_TED_FIELDS = list(TED_DEFAULT_FIELDS)
 for _extra_field in [
     "description",
     "deadline-date",
     "buyer-country",
     "notice-type",
+    "form-type",
 ]:
     if _extra_field not in DEFAULT_TED_FIELDS:
         DEFAULT_TED_FIELDS.append(_extra_field)
 
 
+def _default_ted_builder() -> Dict[str, Any]:
+    today = datetime.utcnow().date()
+    start = today - timedelta(days=DEFAULT_TED_LOOKBACK_DAYS)
+    return {
+        "date_from": start.isoformat(),
+        "date_to": today.isoformat(),
+        "countries": list(DEFAULT_TED_COUNTRIES),
+        "cpv_prefixes": list(DEFAULT_TED_CPV_PREFIXES),
+        "keywords": list(DEFAULT_TED_KEYWORDS),
+        "form_types": list(DEFAULT_TED_FORM_TYPES),
+    }
+
+
+def _build_query_from_builder(builder: Dict[str, Any]) -> str:
+    countries = [value.upper() for value in _ensure_list(builder.get("countries"))]
+    cpv_prefixes = _ensure_list(builder.get("cpv_prefixes"))
+    keywords = _ensure_list(builder.get("keywords"))
+    form_types = [value.upper() for value in _ensure_list(builder.get("form_types"))]
+
+    return build_ted_query(
+        date_from=builder.get("date_from"),
+        date_to=builder.get("date_to"),
+        countries=countries or None,
+        cpv_prefixes=cpv_prefixes or None,
+        keywords=keywords or None,
+        form_types=form_types or None,
+    )
+
+
+def _default_ted_query() -> str:
+    return _build_query_from_builder(_default_ted_builder())
+
+
+def _merge_ted_builder(
+    base: Dict[str, Any], overrides: Dict[str, Any]
+) -> Dict[str, Any]:
+    merged = dict(base)
+
+    date_from = overrides.get("date_from")
+    if isinstance(date_from, str) and date_from.strip():
+        merged["date_from"] = date_from.strip()
+    date_to = overrides.get("date_to")
+    if isinstance(date_to, str) and date_to.strip():
+        merged["date_to"] = date_to.strip()
+
+    for key, uppercase in (
+        ("countries", True),
+        ("cpv_prefixes", False),
+        ("keywords", False),
+        ("form_types", True),
+    ):
+        if key in overrides:
+            values = _ensure_list(overrides.get(key))
+            if uppercase:
+                values = [value.upper() for value in values]
+            merged[key] = values
+
+    return merged
+
+
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
+    default_builder = _default_ted_builder()
     settings: Dict[str, Any] = {
-        "query": DEFAULT_TED_QUERY,
+        "query": _default_ted_query(),
         "fields": list(DEFAULT_TED_FIELDS),
         "limit": 100,
         "sort_field": "publication-date",
         "sort_order": "DESC",
         "mode": "page",
         "page": 1,
     }
-    builder: Dict[str, Any] = {}
+    builder: Dict[str, Any] = dict(default_builder)
 
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
-        settings["sort_order"] = sort_order.strip()
+        settings["sort_order"] = sort_order.strip().upper()
 
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
-        builder = builder_candidate
+        builder = _merge_ted_builder(builder, builder_candidate)
+
+    if not settings.get("query"):
+        settings["query"] = _build_query_from_builder(builder)
 
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
         self.session.headers.update(
             {
                 "User-Agent": (
                     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/123.0.0.0 Safari/537.36"
                 ),
                 "Accept-Language": "en-US,en;q=0.9",
diff --git a/app.py b/app.py
index 20f3a7cb36574d8b012228ad05b2d2e5e150d2ea..f5bc3e9fc9c0835803795f60de8171b4e78da8ce 100644
--- a/app.py
+++ b/app.py
@@ -252,51 +322,55 @@ class TenderCrawler:
             token = self.session.cookies.get("__RequestVerificationToken")
 
         if not token:
             for cookie in self.session.cookies:
                 if cookie.name.startswith("__RequestVerificationToken") and cookie.value:
                     token = cookie.value
                     break
 
         if not token:
             text = getattr(response, "text", "") or ""
             for pattern in (
                 r"name=\"__RequestVerificationToken\"[^>]*value=\"([^\"]+)\"",
                 r"__RequestVerificationToken\"\s*:\s*\"([^\"]+)\"",
                 r"__RequestVerificationToken'\s*:\s*'([^']+)'",
             ):
                 match = re.search(pattern, text)
                 if match and match.group(1):
                     token = match.group(1)
                     break
 
         return token
 
     def get_search_config(self, site: str) -> str:
         """검색 설정 조회"""
         config = session.query(SearchConfig).filter_by(site=site).first()
-        return config.query or "" if config else ""
+        if config and config.query is not None:
+            return config.query
+        if site.upper() == "UNGM":
+            return ", ".join(DEFAULT_UNGM_KEYWORDS)
+        return ""
 
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
diff --git a/app.py b/app.py
index 20f3a7cb36574d8b012228ad05b2d2e5e150d2ea..f5bc3e9fc9c0835803795f60de8171b4e78da8ce 100644
--- a/app.py
+++ b/app.py
@@ -323,90 +397,97 @@ class TenderCrawler:
         today = datetime.utcnow().strftime("%d-%b-%Y")
         yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%d-%b-%Y")
 
         url = 'https://www.ungm.org/Public/Notice/Search'
         referer_url = 'https://www.ungm.org/Public/Notice'
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
 
+        config_raw = self.get_search_config('UNGM')
+        keywords: List[str] = []
+        if config_raw:
+            tokens = _split_tokens(config_raw)
+            if tokens:
+                payload["Title"] = " ".join(tokens)
+                payload["Description"] = " ".join(tokens)
+                keywords = [token.lower() for token in tokens]
+
         try:
             verification_token: Optional[str] = None
 
             for bootstrap_url in (referer_url, url):
                 bootstrap_response = self.session.get(bootstrap_url, timeout=30)
                 bootstrap_response.raise_for_status()
                 verification_token = self._extract_ungm_token(bootstrap_response)
                 if verification_token:
                     break
 
             if not verification_token:
                 app.logger.error(
                     "UNGM crawling failed: unable to locate verification token"
                 )
                 return 0
 
             request_headers = {
                 "Referer": referer_url,
                 "X-Requested-With": "XMLHttpRequest",
                 "Origin": "https://www.ungm.org",
                 "RequestVerificationToken": verification_token,
             }
             request_payload = dict(payload)
             request_payload["__RequestVerificationToken"] = verification_token
 
             response = self.session.post(
                 url,
                 json=request_payload,
                 headers=request_headers,
                 timeout=30,
             )
             response.raise_for_status()
 
             # BeautifulSoup으로 파싱 (html5lib 파서 사용)
             soup = BeautifulSoup(response.content, 'html5lib')
             rows = soup.select('.tableRow')
 
             count = 0
-            config_raw = self.get_search_config('UNGM')
-            keywords = [token.lower() for token in _split_tokens(config_raw)] if config_raw else []
 
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
diff --git a/app.py b/app.py
index 20f3a7cb36574d8b012228ad05b2d2e5e150d2ea..f5bc3e9fc9c0835803795f60de8171b4e78da8ce 100644
--- a/app.py
+++ b/app.py
@@ -439,51 +520,51 @@ class TenderCrawler:
                     count += 1
 
             app.logger.info(f"UNGM crawling completed: {count} tenders processed")
             return count
 
         except requests.HTTPError as exc:
             detail = ""
             if exc.response is not None:
                 detail = f" - {exc.response.text[:200]}"
             app.logger.error(f"UNGM crawling failed: {exc}{detail}")
             return 0
         except requests.RequestException as exc:
             app.logger.error(f"UNGM crawling failed: {exc}")
             return 0
         except Exception as exc:
             app.logger.error(f"UNGM crawling failed: {exc}")
             return 0
 
     def crawl_ted(self) -> int:
         """TED 사이트 크롤링"""
         app.logger.info("Starting TED crawling...")
 
         url = "https://api.ted.europa.eu/v3/notices/search"
 
         ted_settings = self.get_ted_settings()
-        search_query = ted_settings.get("query") or DEFAULT_TED_QUERY
+        search_query = ted_settings.get("query") or _default_ted_query()
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
 
         sort_order_value = str(sort_order).lower() if sort_order else "desc"
 
         field_tokens = [str(field).strip() for field in fields if str(field).strip()]
diff --git a/app.py b/app.py
index 20f3a7cb36574d8b012228ad05b2d2e5e150d2ea..f5bc3e9fc9c0835803795f60de8171b4e78da8ce 100644
--- a/app.py
+++ b/app.py
@@ -698,51 +779,51 @@ def get_tenders():
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
-        default_ted_from = (today - timedelta(days=30)).isoformat()
+        default_ted_from = (today - timedelta(days=DEFAULT_TED_LOOKBACK_DAYS)).isoformat()
 
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
diff --git a/app.py b/app.py
index 20f3a7cb36574d8b012228ad05b2d2e5e150d2ea..f5bc3e9fc9c0835803795f60de8171b4e78da8ce 100644
--- a/app.py
+++ b/app.py
@@ -785,81 +866,84 @@ def search_config():
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
 
-    ungm_keywords_value = ungm_config.query if ungm_config else ''
+    if ungm_config is None:
+        ungm_keywords_value = ", ".join(DEFAULT_UNGM_KEYWORDS)
+    else:
+        ungm_keywords_value = ungm_config.query or ''
     ungm_keyword_list = _split_tokens(ungm_keywords_value)
     ungm_preview_link = build_ungm_deeplink(keywords=ungm_keyword_list)
 
     ted_settings, ted_builder = _parse_ted_config(ted_config.query if ted_config else None)
 
     today = datetime.utcnow().date()
     default_ted_to = today.isoformat()
-    default_ted_from = (today - timedelta(days=30)).isoformat()
+    default_ted_from = (today - timedelta(days=DEFAULT_TED_LOOKBACK_DAYS)).isoformat()
 
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
-        ted_query_preview=ted_settings.get("query", DEFAULT_TED_QUERY),
+        ted_query_preview=ted_settings.get("query", _default_ted_query()),
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
