"""Naver Search Ad API 클라이언트 (score-3 / score-4).

[ 사용 ]
  · /keywordstool — 절대 월 검색량 (monthlyPcQcCnt + Mobile) + 연관 키워드
  · 가입: searchad.naver.com → 도구 > API 사용 관리
  · 인증: Customer ID + Access License + Secret Key 의 HMAC-SHA256 서명

[ env ]
  NAVER_SEARCHAD_API_KEY        — Access License
  NAVER_SEARCHAD_SECRET_KEY     — Secret Key (HMAC)
  NAVER_SEARCHAD_CUSTOMER_ID    — Customer ID

키 없거나 호출 실패 → 모든 함수가 None 또는 빈 결과 반환 (앱 멈춤 0).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
from dataclasses import dataclass
from typing import List, Optional

log = logging.getLogger(__name__)


_BASE_URL = "https://api.searchad.naver.com"


def _credentials():
    return (
        os.environ.get("NAVER_SEARCHAD_API_KEY", ""),
        os.environ.get("NAVER_SEARCHAD_SECRET_KEY", ""),
        os.environ.get("NAVER_SEARCHAD_CUSTOMER_ID", ""),
    )


def has_credentials() -> bool:
    a, s, c = _credentials()
    return bool(a and s and c)


def _signature(timestamp: str, method: str, uri: str, secret: str) -> str:
    msg = f"{timestamp}.{method}.{uri}"
    h = hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256)
    return base64.b64encode(h.digest()).decode("utf-8")


@dataclass
class KeywordToolItem:
    keyword: str
    monthly_pc_qc: int = 0
    monthly_mobile_qc: int = 0
    competition_label: str = ""   # 낮음/중간/높음 (Naver 자체 라벨)
    monthly_avg_pc_clk: float = 0.0
    monthly_avg_mobile_clk: float = 0.0
    pl_avg_depth: float = 0.0

    @property
    def monthly_total_qc(self) -> int:
        """절대 월 검색량 합산 (PC + Mobile)."""
        return int(self.monthly_pc_qc + self.monthly_mobile_qc)


def keywordstool(keyword: str, *, include_related: bool = False,
                   show_detail: bool = True, timeout: int = 10
                   ) -> List[KeywordToolItem]:
    """/keywordstool 호출. 키 없거나 실패 시 빈 리스트.

    Args:
      keyword         : 조회 키워드 (대표). 콤마로 5개까지 동시.
      include_related : True 면 연관 키워드 후보까지 함께 반환 (보통 100건)
      show_detail     : 상세 통계 포함 여부
    """
    if not has_credentials():
        return []
    try:
        import requests
    except ImportError:
        log.warning("[search_ad] requests 미설치")
        return []

    api_key, secret, customer_id = _credentials()
    uri = "/keywordstool"
    timestamp = str(int(time.time() * 1000))
    headers = {
        "X-Timestamp": timestamp,
        "X-API-KEY": api_key,
        "X-Customer": str(customer_id),
        "X-Signature": _signature(timestamp, "GET", uri, secret),
    }
    params = {
        "hintKeywords": keyword,
        "showDetail": "1" if show_detail else "0",
    }
    if include_related:
        params["event"] = "0"

    try:
        r = requests.get(_BASE_URL + uri, headers=headers, params=params, timeout=timeout)
        if r.status_code != 200:
            log.warning("[search_ad] HTTP %s: %s", r.status_code, r.text[:200])
            return []
        data = r.json()
    except Exception as e:
        log.warning("[search_ad] 호출 실패: %s", e)
        return []

    out: List[KeywordToolItem] = []
    for k in data.get("keywordList", []) or []:
        # API 가 '<10' 같이 문자열로 줄 수 있음 — 안전 변환
        def _toi(v):
            try:
                return int(str(v).replace("<", "").strip())
            except Exception:
                return 0
        def _tof(v):
            try:
                return float(v)
            except Exception:
                return 0.0
        out.append(KeywordToolItem(
            keyword=k.get("relKeyword", ""),
            monthly_pc_qc=_toi(k.get("monthlyPcQcCnt", 0)),
            monthly_mobile_qc=_toi(k.get("monthlyMobileQcCnt", 0)),
            competition_label=k.get("compIdx", "") or "",
            monthly_avg_pc_clk=_tof(k.get("monthlyAvePcClkCnt", 0)),
            monthly_avg_mobile_clk=_tof(k.get("monthlyAveMobileClkCnt", 0)),
            pl_avg_depth=_tof(k.get("plAvgDepth", 0)),
        ))
    return out


def monthly_search_volume(keyword: str) -> Optional[int]:
    """절대 월 검색량 (PC+Mobile) 단일 키워드 — 정확도 ⭐⭐⭐.

    Returns:
      검색량 (int) 또는 None (키 없음/실패).
    """
    items = keywordstool(keyword)
    if not items:
        return None
    # 대표 키워드 매치 우선, 못 찾으면 첫 결과
    for it in items:
        if it.keyword == keyword:
            return it.monthly_total_qc
    return items[0].monthly_total_qc


def related_keywords(keyword: str, *, limit: int = 30) -> List[str]:
    """연관 키워드 후보 — expander.py 에서 자동완성 대체/보완.

    Returns:
      관련 키워드 텍스트 리스트. 키 없으면 빈 리스트.
    """
    items = keywordstool(keyword, include_related=True)
    if not items:
        return []
    out = []
    seen = {keyword.strip().lower()}
    for it in items:
        kw = (it.keyword or "").strip()
        if not kw or kw.lower() in seen:
            continue
        seen.add(kw.lower())
        out.append(kw)
        if len(out) >= limit:
            break
    return out
