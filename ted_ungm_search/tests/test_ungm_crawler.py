"""Tests for the UNGM crawling helpers in :mod:`app`."""
from __future__ import annotations

import json

from app import TenderCrawler


class _DummyResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.headers = {"Content-Type": "text/html"}
        self.text = json.dumps(payload).encode("utf-8")
        self.content = self.text
        self.status_code = 200
        self.ok = True
        self.url = "https://www.ungm.org/Public/Notice/Search"

    def json(self) -> dict:
        return self._payload


def test_crawl_ungm_accepts_byte_response_text(monkeypatch) -> None:
    """Ensure byte-valued response text is handled gracefully."""

    crawler = TenderCrawler()
    payload = {
        "items": [
            {
                "Title": "PCR Diagnostic Kits",
                "Reference": "REF-001",
                "Published": "2024-01-01T00:00:00Z",
                "Deadline": "2024-02-01T00:00:00Z",
                "Agency": "UNICEF",
                "NoticeType": "RFP",
                "Country": "Global",
                "DetailUrl": "/Notice/12345",
            }
        ]
    }
    response = _DummyResponse(payload)

    monkeypatch.setattr(
        crawler,
        "_bootstrap_ungm_tokens",
        lambda referer, url: ("token", "cookie"),
    )
    monkeypatch.setattr(
        crawler,
        "_post_ungm_search",
        lambda url, referer_url, request_payload, verification_token, verification_cookie: response,
    )
    monkeypatch.setattr(crawler, "get_search_config", lambda site: "PCR")

    saved_records: list[dict] = []

    def _fake_save(tender: dict) -> bool:
        saved_records.append(tender)
        return True

    monkeypatch.setattr(crawler, "save_to_db", _fake_save)

    try:
        count = crawler.crawl_ungm()
    finally:
        crawler.session.close()

    assert count == 1
    assert saved_records[0]["title"] == "PCR Diagnostic Kits"
    assert saved_records[0]["detail_url"] == "https://www.ungm.org/Notice/12345"
