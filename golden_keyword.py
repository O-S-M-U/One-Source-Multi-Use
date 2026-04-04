#!/usr/bin/env python3
"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  🥇 황금 키워드 분석기 (Golden Keyword Analyzer)
  O.S.M.U 프로젝트 | 검색광고 API 없는 버전
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[ 황금 키워드 4가지 기준 & 가중치 ]
  1. DataLab 트렌드 점수  : 40점  ← 네이버 내 실제 검색 인기도 (주 지표)
  2. Naver Blog 경쟁도    : 30점  ← 블로그 결과수 역수 (KD proxy)
  3. 상업적 의도          : 20점  ← 키워드 텍스트 분석
  4. Google Trends        : 10점  ← 구글 트렌드 보조

[ 씨앗 키워드 확장 방식 ]
  - 네이버 자동완성 API   : 자동완성 후보 최대 10개 (인증 불필요)
  - 접미어 조합 확장      : 씨앗 + [추천/방법/비교/후기/가격/순위...] 최대 10개

[ 데이터 소스 ]
  - 네이버 DataLab API    : 트렌드 점수 (0~100), 상승/유지/하락 방향성
  - 네이버 블로그 검색 API: 결과 수 → 경쟁도 역산
  - Google Trends          : pytrends 비공식 라이브러리 (무료, API 키 불필요)

[ 사용법 ]
  # 씨앗 키워드 확장 모드 (권장)
  python golden_keyword.py --seed "다이어트"
  python golden_keyword.py --seed "재테크" --top 3

  # 단건/다건 직접 분석 모드
  python golden_keyword.py "다이어트 추천"
  python golden_keyword.py "키워드1,키워드2,키워드3"

  # JSON 출력 (파이프라인 연동)
  python golden_keyword.py --seed "다이어트" --json

[ 환경변수 (.env) ]
  NAVER_CLIENT_ID=xxxx        # 네이버 오픈 API (블로그 검색 + DataLab 공용)
  NAVER_CLIENT_SECRET=xxxx
  GOOGLE_SHEETS_ID=xxxx       # (선택) Google Sheets 파일 ID → 미설정 시 CSV 저장
"""

import os
import sys
import json
import time
import re
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

# ── 환경변수 로드 ──────────────────────────────────────────
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

NAVER_CLIENT_ID     = os.getenv("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "")

# DataLab은 같은 앱의 키 재사용 가능 (서비스만 추가 신청)
NAVER_DATALAB_CLIENT_ID     = os.getenv("NAVER_DATALAB_CLIENT_ID", "") or NAVER_CLIENT_ID
NAVER_DATALAB_CLIENT_SECRET = os.getenv("NAVER_DATALAB_CLIENT_SECRET", "") or NAVER_CLIENT_SECRET

# ── 황금 키워드 기준값 ──────────────────────────────────────
GOLDEN_CRITERIA = {
    # 상업적 의도 키워드 (키워드에 포함되면 수익화 가능성 높음)
    "commercial_words": [
        "추천", "비교", "방법", "가격", "후기", "리뷰", "구매", "순위",
        "장단점", "효과", "종류", "선택", "어떻게", "최고", "베스트",
    ],
    # 접미어 조합 확장용 (씨앗 + 이 단어들 조합)
    "expansion_suffixes": [
        "추천", "방법", "비교", "후기", "가격", "순위", "장단점",
        "효과", "종류", "주의사항",
    ],
    # 블로그 경쟁도 기준 (결과 수)
    "blog_competition": {
        "very_low" : 5_000,    # 매우 낮음: 30점
        "low"      : 30_000,   # 낮음: 20점
        "medium"   : 100_000,  # 보통: 10점
        "high"     : 500_000,  # 높음: 5점
                               # 초과: 0점
    },
}

# ── 일반 키워드 기본 가중치 ────────────────────────────────
DEFAULT_WEIGHTS = {
    "datalab"   : 40,   # DataLab 트렌드 점수 (네이버 내 인기도)
    "blog_comp" : 30,   # 블로그 결과수 역수 (경쟁도 proxy)
    "commercial": 20,   # 상업적 의도 (텍스트 분석)
    "gtrends"   : 10,   # Google Trends (보조)
}

# ── 롱테일 키워드 전용 가중치 ──────────────────────────────
# 롱테일은 트렌드 절대값이 낮아도 경쟁이 없으면 충분히 가치 있음
# → 경쟁도에 가중치를 몰아주고 트렌드 패널티를 줄임
LONGTAIL_WEIGHTS = {
    "datalab"   : 20,   # 낮춤: 롱테일은 절대 트렌드 낮아도 OK
    "blog_comp" : 45,   # 높임: 경쟁 없는 틈새 진입이 핵심 가치
    "commercial": 25,   # 소폭 상향: 구체적일수록 의도 명확해야
    "gtrends"   : 10,   # 유지
}

# ── 키워드 연금술 수식어 뱅크 ──────────────────────────────
# 각 카테고리에서 처방 타입에 맞는 수식어를 조합해 롱테일 키워드 생성
ALCHEMY_TEMPLATES = {

    # 상업의도 낮을 때 → 구매/비교/탐색 의도 단어 추가
    "상업의도": [
        "추천", "비교", "후기", "순위", "방법", "장단점",
        "리뷰", "효과", "선택 방법", "어떻게 하나요",
        "잘하는 법", "잘 고르는 법",
    ],

    # 경쟁도 높을 때 → 대상별 구체화 (롱테일화 핵심)
    "대상": [
        "대학생", "직장인", "사회초년생", "30대", "40대", "50대",
        "주부", "자취생", "초보자", "여성", "남성",
        "임산부", "중년 여성", "시니어",
    ],

    # 경쟁도 높을 때 → 상황별 구체화
    "상황": [
        "처음 시작하는", "혼자서", "집에서", "10분 만에",
        "간단하게", "입문", "바쁜 직장인을 위한",
        "운동 없이", "식단만으로", "꾸준히 하는",
        "주말에", "아침에",
    ],

    # 경쟁도 높을 때 → 가격/조건 구체화
    "가격": [
        "가성비", "저렴하게", "무료로", "저예산",
        "비용 없이", "돈 안 드는", "월 1만원으로",
    ],

    # 트렌드 낮을 때 → 시의성/목적 수식어 추가
    "목적": [
        "선물용", "내돈내산 후기", "실사용 후기",
        "2026년", "최신", "효과 좋은",
        "실패 없는", "검증된",
    ],

    # 기간 수식어 → 단기 결과 원하는 검색자 공략
    "기간": [
        "1주일", "한 달", "3개월 만에",
        "빠르게", "단기간에", "꾸준히",
    ],
}

# ── 키워드 풀 관리 설정 ────────────────────────────────────
POOL_MAX_SIZE = 200
REVIVAL_DAYS  = 30   # 이 기간(일)이 지난 키워드는 재평가 대상
GRADE_ORDER   = {"황금": 4, "좋은": 3, "보통": 2, "미달": 1}
POOL_CSV_PATH = os.path.join(os.path.dirname(__file__), "keyword_pool.csv")
POOL_COLUMNS  = [
    "keyword", "grade", "total_score", "profile",
    "datalab_score", "datalab_direction",
    "blog_results", "blog_competition", "commercial_hits", "gtrends_score",
    "weak_points", "is_alchemy", "original_keyword", "seed_keyword",
    "status",            # active / used / reviving / deprecated
    "last_evaluated_at",
    "revival_count",
    "created_at",
]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. 씨앗 키워드 확장
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fetch_naver_autocomplete(seed: str) -> list:
    """
    네이버 자동완성 API로 씨앗 키워드 연관어 조회 (비공식, 인증 불필요).
    반환: 자동완성 키워드 리스트 (최대 10개)

    [ 주니어 개발자에게 ]
    이 URL은 네이버 검색창의 자동완성 기능과 동일한 엔드포인트예요.
    공식 API가 아니므로 언제든 구조가 바뀔 수 있어요. 실패 시 graceful fallback.
    """
    url = "https://ac.search.naver.com/nx/ac"
    params = {
        "q"        : seed,
        "q_enc"    : "utf-8",
        "st"       : "111",
        "frm"      : "nv",
        "r_format" : "json",
        "r_enc"    : "utf-8",
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.naver.com",
    }

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=8)
        resp.raise_for_status()
        data = resp.json()

        # items 구조: [["키워드", "타입"], ...]
        items = data.get("items", [[]])
        if not items or not items[0]:
            return []

        results = [item[0] for item in items[0] if item and item[0].strip()]
        return results[:10]

    except Exception as e:
        print(f"  ⚠️  네이버 자동완성 조회 실패: {e}")
        return []


def expand_seed(seed: str) -> list:
    """
    씨앗 키워드 → 후보 키워드 목록 생성.

    [ 확장 전략 ]
    1. 네이버 자동완성 → 실제 사용자가 검색하는 연관어 (최대 10개)
    2. 접미어 조합 → 상업적 의도 키워드 조합 (최대 10개)
    3. 중복 제거 후 씨앗 키워드 자체도 포함
    """
    print(f"  ① 네이버 자동완성 조회...")
    autocomplete = fetch_naver_autocomplete(seed)
    print(f"     → 자동완성 {len(autocomplete)}개: {autocomplete[:5]}{'...' if len(autocomplete)>5 else ''}")

    print(f"  ② 접미어 조합 확장...")
    suffixes = GOLDEN_CRITERIA["expansion_suffixes"]
    suffix_keywords = [f"{seed} {s}" for s in suffixes]

    # 합치기: 자동완성 우선, 접미어 보완, 씨앗 자체 포함
    all_candidates = [seed] + autocomplete + suffix_keywords

    # 중복 제거 — 표면 중복 + 의미적 중복(띄어쓰기만 다른 경우) 모두 제거
    # 예: "다이어트방법" 과 "다이어트 방법" 은 같은 키워드로 처리
    seen_surface    = set()   # 표면 문자열 중복 방지
    seen_normalized = set()   # 공백 제거 후 소문자 비교 (의미적 중복)
    unique = []
    for kw in all_candidates:
        kw   = " ".join(kw.split())          # 연속 공백 → 단일 공백 정규화
        norm = kw.replace(" ", "").lower()   # 의미적 동일성 키
        if kw and kw not in seen_surface and norm not in seen_normalized:
            seen_surface.add(kw)
            seen_normalized.add(norm)
            unique.append(kw)

    print(f"  ✅ 총 {len(unique)}개 후보 확보 (띄어쓰기 정규화 + 중복 제거 후)")
    return unique


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. 네이버 DataLab API (트렌드 점수) ← 주 지표
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fetch_naver_datalab(keyword: str) -> dict:
    """
    네이버 DataLab 검색어 트렌드 API → 최근 3개월 상대 트렌드 조회.
    반환: { trend_score, trend_direction, recent_avg, source }

    [ 주니어 개발자에게 ]
    DataLab API는 https://developers.naver.com 에서 무료 발급.
    블로그 검색과 동일한 앱에 '데이터랩(검색어 트렌드)' 서비스 추가하면 됩니다.
    반환값은 절대 검색량이 아닌 상대 점수(0~100)예요.
    """
    if not NAVER_DATALAB_CLIENT_ID or not NAVER_DATALAB_CLIENT_SECRET:
        print("  ⚠️  DataLab 키 없음 → 스킵")
        return {"trend_score": 0, "trend_direction": "데이터없음", "source": "datalab(스킵)"}

    url = "https://openapi.naver.com/v1/datalab/search"
    headers = {
        "X-Naver-Client-Id"    : NAVER_DATALAB_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_DATALAB_CLIENT_SECRET,
        "Content-Type"         : "application/json",
    }
    end_date   = datetime.now()
    start_date = end_date - timedelta(days=90)

    body = {
        "startDate"    : start_date.strftime("%Y-%m-%d"),
        "endDate"      : end_date.strftime("%Y-%m-%d"),
        "timeUnit"     : "week",
        "keywordGroups": [{"groupName": keyword, "keywords": [keyword]}],
    }

    try:
        resp = requests.post(url, headers=headers, json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        periods = data.get("results", [{}])[0].get("data", [])
        if not periods:
            return {"trend_score": 0, "trend_direction": "데이터없음", "source": "naver_datalab"}

        scores     = [p.get("ratio", 0) for p in periods]
        avg        = round(sum(scores) / len(scores), 1) if scores else 0
        recent     = scores[-4:]
        older      = scores[:-4] if len(scores) > 4 else scores[:max(1, len(scores) // 2)]
        recent_avg = round(sum(recent) / len(recent), 1) if recent else 0
        older_avg  = sum(older) / len(older) if older else 0

        if   recent_avg > older_avg * 1.2: direction = "📈 상승중"
        elif recent_avg < older_avg * 0.8: direction = "📉 하락중"
        else:                              direction = "➡️  유지중"

        return {
            "trend_score"    : avg,
            "trend_direction": direction,
            "recent_avg"     : recent_avg,
            "source"         : "naver_datalab",
        }

    except Exception as e:
        print(f"  ⚠️  DataLab 실패 [{keyword}]: {e}")
        return {"trend_score": 0, "trend_direction": "조회실패", "source": "naver_datalab"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. 네이버 블로그 검색 API (경쟁도 proxy)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fetch_naver_blog(keyword: str) -> dict:
    """
    네이버 블로그 검색 API → 결과 수로 경쟁도 역산.
    반환: { total_results, competition_label, competition_score, top_posts, source }

    [ 주니어 개발자에게 ]
    result 수가 적을수록 → 경쟁이 적음 → 황금 키워드 가능성 UP.
    result 수 자체는 검색량의 완벽한 proxy가 아니에요.
    DataLab 트렌드 점수와 함께 봐야 정확도가 높아요.
    """
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        print("  ⚠️  Naver API 키 없음 → 블로그 검색 스킵")
        return {"total_results": None, "competition_score": 15, "source": "naver_blog(스킵)"}

    url     = "https://openapi.naver.com/v1/search/blog.json"
    headers = {
        "X-Naver-Client-Id"    : NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    params = {"query": keyword, "display": 5, "sort": "sim"}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data  = resp.json()
        total = data.get("total", 0)
        items = data.get("items", [])
        top_posts = [
            re.sub(r"<[^>]+>", "", item.get("title", ""))
            for item in items[:3]
        ]

        # 경쟁도 역산 점수 (결과수 ↓ → 경쟁 ↓ → 점수 ↑)
        bc = GOLDEN_CRITERIA["blog_competition"]
        if   total < bc["very_low"]: score, label = 30, "🟢 매우 낮음"
        elif total < bc["low"]     : score, label = 20, "🟡 낮음"
        elif total < bc["medium"]  : score, label = 10, "🟠 보통"
        elif total < bc["high"]    : score, label =  5, "🔴 높음"
        else                       : score, label =  0, "🔴 매우 높음 (레드오션)"

        return {
            "total_results"    : total,
            "competition_label": label,
            "competition_score": score,
            "top_posts"        : top_posts,
            "source"           : "naver_blog_search",
        }

    except Exception as e:
        print(f"  ⚠️  블로그 검색 실패 [{keyword}]: {e}")
        return {"total_results": None, "competition_score": 15, "source": "naver_blog(실패)"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  4. Google Trends (보조 지표)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fetch_google_trends(keyword: str) -> dict:
    """
    pytrends로 최근 3개월 구글 트렌드 조회 (무료, 비공식).
    반환: { trend_score, trend_direction, source }

    [ 주니어 개발자에게 ]
    과도한 요청 시 429 에러. 키워드 사이 sleep(1.2) 필수.
    한국 기준(geo='KR')으로 조회해도 네이버 트렌드와 다를 수 있어요.
    """
    try:
        from pytrends.request import TrendReq
        pytrends = TrendReq(hl="ko", tz=540, timeout=(10, 25))
        pytrends.build_payload([keyword], timeframe="today 3-m", geo="KR")
        df = pytrends.interest_over_time()

        if df.empty or keyword not in df.columns:
            return {"trend_score": 0, "trend_direction": "데이터없음", "source": "google_trends"}

        scores     = df[keyword].tolist()
        avg        = round(sum(scores) / len(scores), 1) if scores else 0
        recent     = scores[-4:]
        older      = scores[:-4] if len(scores) > 4 else scores[:max(1, len(scores) // 2)]
        recent_avg = sum(recent) / len(recent) if recent else 0
        older_avg  = sum(older)  / len(older)  if older  else 0

        if   recent_avg > older_avg * 1.2: direction = "📈 상승중"
        elif recent_avg < older_avg * 0.8: direction = "📉 하락중"
        else:                              direction = "➡️  유지중"

        return {"trend_score": avg, "trend_direction": direction, "source": "google_trends"}

    except Exception as e:
        print(f"  ⚠️  Google Trends 실패 [{keyword}]: {e}")
        return {"trend_score": 0, "trend_direction": "조회실패", "source": "google_trends"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  5. 황금 키워드 점수 계산
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def score_keyword(
    keyword : str,
    datalab : dict,
    blog    : dict,
    gtrends : dict,
    weights : dict = None,   # None → DEFAULT_WEIGHTS 사용, LONGTAIL_WEIGHTS 전달 시 롱테일 프로필
) -> dict:
    """
    4개 기준으로 황금 키워드 점수 계산 (총 100점).

    [ 가중치 설계 — weights 파라미터로 프로필 전환 ]
    일반 프로필 (DEFAULT_WEIGHTS):
      DataLab 트렌드  40점  Blog 경쟁도 30점  상업의도 20점  Google 10점
    롱테일 프로필 (LONGTAIL_WEIGHTS):
      DataLab 트렌드  20점  Blog 경쟁도 45점  상업의도 25점  Google 10점
      → 트렌드 절대값 낮아도 경쟁 없으면 충분히 가치 있는 롱테일 구조

    [ 주니어 개발자에게 ]
    run_alchemy()는 LONGTAIL_WEIGHTS를 전달해서 이 함수를 재사용해요.
    모든 점수는 weights 비율에 따라 자동으로 스케일링됩니다.
    """
    if weights is None:
        weights = DEFAULT_WEIGHTS
    result = {
        "keyword"        : keyword,
        "scores"         : {},
        "total_score"    : 0,
        "is_golden"      : False,
        "verdict"        : "",
        "fail_reasons"   : [],
        "recommendations": [],
    }

    w_dl   = weights["datalab"]
    w_blog = weights["blog_comp"]
    w_com  = weights["commercial"]
    w_gt   = weights["gtrends"]
    is_longtail = (weights != DEFAULT_WEIGHTS)

    # ── 기준 1: DataLab 트렌드 ──────────────────────────────
    dl_score_raw = datalab.get("trend_score", 0)
    dl_direction = datalab.get("trend_direction", "데이터없음")
    dl_source    = datalab.get("source", "")

    if "스킵" in dl_source or "실패" in dl_source or "데이터없음" in dl_direction:
        dl_score = int(w_dl * 0.38)   # 데이터 없을 때 중간값
        dl_label = "⚠️  DataLab 데이터 없음 (NAVER_CLIENT_ID 설정 확인)"
    else:
        # 0~40 기준 원점수 계산 후 가중치로 스케일
        if   dl_score_raw >= 70: base = 35
        elif dl_score_raw >= 50: base = 27
        elif dl_score_raw >= 30: base = 18
        elif dl_score_raw >= 10: base = 10
        else                   : base = 4
        adj  = 5 if "상승중" in dl_direction else (-5 if "하락중" in dl_direction else 0)
        raw  = max(0, min(40, base + adj))
        dl_score = max(0, min(w_dl, round(raw * w_dl / 40)))

        thresh_hi = w_dl * 0.62
        thresh_lo = w_dl * 0.30
        icon = "✅" if dl_score >= thresh_hi else ("⚠️ " if dl_score >= thresh_lo else "❌")
        dl_label = f"{icon} 트렌드 {dl_score_raw}/100  {dl_direction}"

        if "상승중" in dl_direction:
            result["recommendations"].append("📈 네이버 트렌드 상승 중! 지금이 포스팅 적기예요")
        elif "하락중" in dl_direction:
            result["recommendations"].append("트렌드 하락 중 — 시기 재검토 또는 연관 키워드 탐색 권장")

    result["scores"][f"DataLab트렌드({w_dl})"] = {"score": dl_score, "max": w_dl, "label": dl_label}

    # ── 기준 2: Blog 경쟁도 ─────────────────────────────────
    raw_blog_score = blog.get("competition_score", 15)   # fetch_naver_blog 기준: /30
    blog_total     = blog.get("total_results")
    blog_comp_label = blog.get("competition_label", "데이터없음")

    if blog_total is None:
        blog_score = int(w_blog * 0.50)
        blog_label = "⚠️  블로그 결과수 없음 (NAVER_CLIENT_ID 설정 확인)"
    else:
        # raw_blog_score는 /30 기준값 → w_blog 기준으로 스케일
        blog_score = max(0, min(w_blog, round(raw_blog_score * w_blog / 30)))
        thresh_hi  = w_blog * 0.65
        thresh_lo  = w_blog * 0.33
        icon = "✅" if blog_score >= thresh_hi else ("⚠️ " if blog_score >= thresh_lo else "❌")
        blog_label = f"{icon} {blog_total:,}개  {blog_comp_label}"
        if raw_blog_score == 0:
            result["fail_reasons"].append("경쟁 과포화 (레드오션)")
            if not is_longtail:
                result["recommendations"].append("경쟁 강함 — 🧪 키워드 연금술로 롱테일 변환을 시도해보세요")

    result["scores"][f"Blog경쟁도({w_blog})"] = {"score": blog_score, "max": w_blog, "label": blog_label}

    # ── 기준 3: 상업적 의도 ─────────────────────────────────
    hits = [w for w in GOLDEN_CRITERIA["commercial_words"] if w in keyword]

    if   len(hits) >= 2: intent_score = w_com;             intent_label = f"✅ 상업 의도 복수 감지: {', '.join(hits)}"
    elif len(hits) == 1: intent_score = round(w_com * 0.75); intent_label = f"✅ 상업 의도 감지: {hits[0]}"
    else:
        intent_score = round(w_com * 0.20)
        intent_label = "❌ 상업적 의도 키워드 없음"
        if not is_longtail:
            result["recommendations"].append(
                f"'{keyword} 추천', '{keyword} 비교' 키워드도 분석해보세요"
            )

    result["scores"][f"상업적의도({w_com})"] = {"score": intent_score, "max": w_com, "label": intent_label}

    # ── 기준 4: Google Trends ───────────────────────────────
    gt_score_raw = gtrends.get("trend_score", 0)
    gt_direction = gtrends.get("trend_direction", "데이터없음")
    gt_source    = gtrends.get("source", "")

    if "실패" in gt_source or "데이터없음" in gt_direction or "조회실패" in gt_direction:
        gt_score = int(w_gt * 0.50)
        gt_label = "⚠️  Google Trends 데이터 없음 (pytrends 설치 확인)"
    else:
        if   gt_score_raw >= 70: raw_gt = 10
        elif gt_score_raw >= 40: raw_gt = 7
        elif gt_score_raw >= 20: raw_gt = 4
        else                   : raw_gt = 2
        if "상승중" in gt_direction:
            raw_gt = min(10, raw_gt + 1)
            result["recommendations"].append("🌐 구글 트렌드도 상승 중 — 글로벌 관심 키워드")
        gt_score = max(0, min(w_gt, round(raw_gt * w_gt / 10)))
        icon = "✅" if gt_score >= w_gt * 0.7 else ("⚠️ " if gt_score >= w_gt * 0.4 else "❌")
        gt_label = f"{icon} 구글 트렌드 {gt_score_raw}/100  {gt_direction}"

    result["scores"][f"Google트렌드({w_gt})"] = {"score": gt_score, "max": w_gt, "label": gt_label}

    # ── 총점 & 판정 ──────────────────────────────────────────
    result["total_score"]  = sum(v["score"] for v in result["scores"].values())
    result["profile"]      = "롱테일" if is_longtail else "일반"

    if   result["total_score"] >= 80: result["is_golden"] = True;  result["verdict"] = "🥇 황금 키워드! 바로 포스팅 시작하세요"
    elif result["total_score"] >= 60: result["is_golden"] = True;  result["verdict"] = "🥈 좋은 키워드! 상업적 의도 강화 후 사용 권장"
    elif result["total_score"] >= 40: result["is_golden"] = False; result["verdict"] = "🥉 보통 키워드. 롱테일 확장 고려"
    else:                             result["is_golden"] = False; result["verdict"] = "❌ 기준 미달. 다른 키워드 탐색 권장"

    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  6. 키워드 연금술 (보통/좋은 → 황금 변환)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def diagnose_weakness(scored: dict) -> list:
    """
    점수 결과에서 낮은 항목을 진단해 처방 타입 반환.
    반환: ["상업의도_부족", "경쟁도_높음", "트렌드_낮음"] 중 해당하는 것들

    [ 처방 타입별 연금술 전략 ]
    상업의도_부족 → 상업 수식어 추가 ("추천", "비교", "후기"...)
    경쟁도_높음   → 대상/상황/가격 수식어로 롱테일화 ("직장인", "가성비"...)
    트렌드_낮음   → 목적/시의성 수식어 추가 ("2026년", "내돈내산"...)
    """
    weaknesses = []
    for key, val in scored.get("scores", {}).items():
        ratio = val["score"] / val.get("max", 1)
        if "상업"   in key and ratio < 0.50: weaknesses.append("상업의도_부족")
        if "Blog"   in key and ratio < 0.40: weaknesses.append("경쟁도_높음")
        if "DataLab" in key and ratio < 0.40: weaknesses.append("트렌드_낮음")
    return weaknesses if weaknesses else ["상업의도_부족", "경쟁도_높음"]


def generate_alchemy_candidates(keyword: str, weaknesses: list) -> list:
    """
    약점 원인에 맞는 변환 키워드 후보 생성 (최대 10개).

    [ 처방별 변환 예시 ]
    "다이어트" + 경쟁도_높음 → "직장인 다이어트", "가성비 다이어트", "다이어트 처음 시작하는"
    "노트북"   + 상업의도_부족 → "노트북 추천", "노트북 비교", "노트북 후기"
    """
    candidates = []

    if "상업의도_부족" in weaknesses:
        for mod in ALCHEMY_TEMPLATES["상업의도"][:6]:
            candidates.append(f"{keyword} {mod}")

    if "경쟁도_높음" in weaknesses:
        for mod in ALCHEMY_TEMPLATES["대상"][:5]:
            candidates.append(f"{mod} {keyword}")
        for mod in ALCHEMY_TEMPLATES["상황"][:4]:
            candidates.append(f"{keyword} {mod}")
        for mod in ALCHEMY_TEMPLATES["가격"][:3]:
            candidates.append(f"{mod} {keyword}")
        for mod in ALCHEMY_TEMPLATES["기간"][:2]:
            candidates.append(f"{keyword} {mod}")

    if "트렌드_낮음" in weaknesses:
        for mod in ALCHEMY_TEMPLATES["목적"][:4]:
            candidates.append(f"{keyword} {mod}")
        for mod in ALCHEMY_TEMPLATES["기간"][:2]:
            candidates.append(f"{keyword} {mod}")

    # 원본과 중복 제거 (의미적 중복 포함), 최대 10개
    orig_norm    = keyword.replace(" ", "").lower()
    seen_surface = {keyword}
    seen_norm    = {orig_norm}
    unique = []
    for c in candidates:
        c    = " ".join(c.split())            # 공백 정규화
        norm = c.replace(" ", "").lower()
        if c not in seen_surface and norm not in seen_norm:
            seen_surface.add(c)
            seen_norm.add(norm)
            unique.append(c)
    return unique[:10]


def run_alchemy(keyword: str, original_scored: dict) -> list:
    """
    보통/좋은 키워드 → 황금 키워드 변환 시도 (키워드 연금술 🧪).

    [ 핵심 설계 ]
    연금술 키워드는 LONGTAIL_WEIGHTS로 채점:
      DataLab 20점 (낮춤) / Blog경쟁도 45점 (높임) / 상업의도 25점 / Google 10점
    → 트렌드는 낮지만 경쟁이 없는 롱테일 키워드에게 공정한 평가 환경 제공

    [ 판정 기준 (동일하게 100점 만점) ]
    80점↑ 황금  /  60~79 좋은  /  40~59 보통  /  40↓ 미달
    """
    orig_total = original_scored.get("total_score", 0)
    grade      = "🥈 좋은" if orig_total >= 60 else "🥉 보통"

    print(f"\n  {'─'*58}")
    print(f"  🧪 키워드 연금술: [{keyword}] ({grade} → 황금 변환 시도)")

    weaknesses = diagnose_weakness(original_scored)
    print(f"  약점 진단: {', '.join(weaknesses)}")

    candidates = generate_alchemy_candidates(keyword, weaknesses)
    if not candidates:
        print("  변환 후보 없음")
        return []

    print(f"  변환 후보 {len(candidates)}개: {candidates}")

    # Google Trends는 연금술 상위 3개에만 호출 (429 방지)
    # 나머지는 중간값(5점) 사용 — LONGTAIL_WEIGHTS에서 Google Trends는 10점으로 영향 적음
    GT_ALCHEMY_LIMIT = 3

    alchemy_results = []
    for i, kw in enumerate(candidates):
        print(f"    [{i+1:2}/{len(candidates)}] {kw}")
        dl   = fetch_naver_datalab(kw)
        blog = fetch_naver_blog(kw)

        if i < GT_ALCHEMY_LIMIT:
            gt = fetch_google_trends(kw)
            time.sleep(2.5)   # 연금술 내 pytrends는 넉넉하게 대기
        else:
            # 429 방지: Google Trends 스킵, 중간값 적용
            gt = {"trend_score": 0, "trend_direction": "스킵(rate limit 방지)", "source": "skipped"}
            time.sleep(0.5)

        # 롱테일 전용 가중치로 재채점
        scored = score_keyword(kw, dl, blog, gt, weights=LONGTAIL_WEIGHTS)

        alchemy_results.append({
            "keyword"         : kw,
            "naver_datalab"   : dl,
            "naver_blog"      : blog,
            "google_trends"   : gt,
            "golden_score"    : scored,
            "is_alchemy"      : True,
            "original_keyword": keyword,
        })

    alchemy_results.sort(key=lambda x: x["golden_score"]["total_score"], reverse=True)

    # 결과 요약 출력
    golden_hit = [r for r in alchemy_results if r["golden_score"]["is_golden"]]
    improved   = [r for r in alchemy_results if r["golden_score"]["total_score"] > orig_total]

    print(f"\n  {'─'*58}")
    print(f"  🧪 연금술 결과 (롱테일 프로필 기준, /100점)")
    for r in alchemy_results[:5]:
        s    = r["golden_score"]
        star = "⭐" if s["is_golden"] else "  "
        dl_s = r["naver_datalab"].get("trend_score", 0)
        comp = r["naver_blog"].get("competition_label", "N/A")[:6]
        print(f"  {star} {r['keyword'][:28]:<28}  DataLab:{dl_s:4.0f}  {comp:<8}  {s['total_score']:>3}점  {s['verdict'][:14]}")

    if golden_hit:
        print(f"\n  ✨ 연금 성공! {len(golden_hit)}개 황금 키워드 도달:")
        for r in golden_hit:
            print(f"     ⭐ [{r['keyword']}]  {r['golden_score']['total_score']}점")
    elif improved:
        best = improved[0]
        print(f"\n  💡 최고 변환: [{best['keyword']}]  {orig_total}점 → {best['golden_score']['total_score']}점 (향상)")
    else:
        print(f"\n  ℹ️  변환 후에도 점수 향상 없음 — 씨앗 키워드 자체를 바꿔보세요")

    return alchemy_results


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  7. 결과 출력
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def print_report(keyword: str, dl: dict, blog: dict, gt: dict, scored: dict):
    line = "─" * 58
    print(f"\n{'═'*58}")
    print(f"  📊 황금 키워드 분석: [{keyword}]")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'═'*58}")

    print(f"\n【 Naver DataLab 】  ({dl.get('source', '')})  ★ 주 지표")
    print(f"  트렌드 점수   : {dl.get('trend_score', 0):.1f} / 100")
    print(f"  방향성        : {dl.get('trend_direction', 'N/A')}")

    print(f"\n【 Naver Blog 경쟁도 】  ({blog.get('source', '')})")
    if blog.get("total_results") is not None:
        print(f"  결과 수       : {blog.get('total_results', 0):,}개  {blog.get('competition_label', '')}")
        for i, t in enumerate(blog.get("top_posts", [])[:3], 1):
            print(f"    {i}. {t[:48]}...")
    else:
        print("  데이터 없음")

    print(f"\n【 Google Trends 】  ({gt.get('source', '')})  보조 지표")
    print(f"  트렌드 점수   : {gt.get('trend_score', 0):.1f} / 100")
    print(f"  방향성        : {gt.get('trend_direction', 'N/A')}")

    print(f"\n{line}")
    print(f"【 황금 키워드 판정 】")
    print(f"{line}")
    for criterion, val in scored["scores"].items():
        max_s   = val.get("max", 40)
        filled  = int(10 * val["score"] / max_s) if max_s else 0
        bar     = "█" * filled + "░" * (10 - filled)
        print(f"  {criterion:<20} [{bar}] {val['score']:>3}/{max_s}점")
        print(f"    └ {val['label']}")

    print(f"{line}")
    print(f"  총점  : {scored['total_score']} / 100점")
    print(f"  판정  : {scored['verdict']}")

    if scored["fail_reasons"]:
        print(f"\n  ⚠️  주의:")
        for r in scored["fail_reasons"]:
            print(f"    • {r}")

    if scored["recommendations"]:
        print(f"\n  💡 다음 액션:")
        for r in scored["recommendations"]:
            print(f"    → {r}")

    print(f"{'═'*58}\n")


def return_json(results: list) -> str:
    return json.dumps(results, ensure_ascii=False, indent=2)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  8. Google Sheets keyword_research 시트 저장
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _build_sheet_rows(all_results: list, seed: str = "") -> list:
    """
    분석 결과 리스트 → keyword_research 시트 행 포맷으로 변환.
    일반 결과 + 연금술 결과를 모두 포함.
    """
    rows = []
    now  = datetime.now().strftime("%Y-%m-%d %H:%M")

    for r in all_results:
        s    = r.get("golden_score", {})
        dl   = r.get("naver_datalab", {})
        blog = r.get("naver_blog", {})
        gt   = r.get("google_trends", {})
        hits = [w for w in GOLDEN_CRITERIA["commercial_words"] if w in r.get("keyword", "")]

        # grade 텍스트
        total = s.get("total_score", 0)
        if   total >= 80: grade = "황금"
        elif total >= 60: grade = "좋은"
        elif total >= 40: grade = "보통"
        else            : grade = "미달"

        # 약점 요약
        weaknesses = []
        for key, val in s.get("scores", {}).items():
            ratio = val["score"] / val.get("max", 1)
            if "상업" in key and ratio < 0.5:  weaknesses.append("상업의도부족")
            if "Blog" in key and ratio < 0.4:  weaknesses.append("경쟁도높음")
            if "DataLab" in key and ratio < 0.4: weaknesses.append("트렌드낮음")

        rows.append({
            "keyword"           : r.get("keyword", ""),
            "grade"             : grade,
            "total_score"       : total,
            "profile"           : s.get("profile", "일반"),
            "datalab_score"     : dl.get("trend_score", 0),
            "datalab_direction" : dl.get("trend_direction", ""),
            "blog_results"      : blog.get("total_results", ""),
            "blog_competition"  : blog.get("competition_label", ""),
            "commercial_hits"   : ", ".join(hits) if hits else "없음",
            "gtrends_score"     : gt.get("trend_score", 0),
            "weak_points"       : ", ".join(weaknesses) if weaknesses else "-",
            "is_alchemy"        : "Y" if r.get("is_alchemy") else "N",
            "original_keyword"  : r.get("original_keyword", ""),
            "seed_keyword"      : seed,
            "created_at"        : now,
        })
    return rows


def save_to_keyword_research_sheet(
    all_results : list,
    seed        : str = "",
    sheet_id    : str = "",   # Google Sheets 파일 ID (MCP 연동 시 사용)
    csv_fallback: bool = True,
) -> str:
    """
    keyword_research 시트에 분석 결과 저장.

    [ 저장 방식 우선순위 ]
    1. gspread (pip install gspread google-auth) → Google Sheets 직접 쓰기
    2. CSV 파일 저장 (fallback) → 수동으로 Sheets에 붙여넣기

    [ keyword_research 시트 컬럼 ]
    keyword | grade | total_score | profile | datalab_score | datalab_direction |
    blog_results | blog_competition | commercial_hits | gtrends_score |
    weak_points | is_alchemy | original_keyword | seed_keyword | created_at

    [ 주니어 개발자에게 ]
    Google Sheets MCP가 Claude Code에 연결되면, Claude가 직접 이 함수 결과를
    Sheets에 append할 수 있어요. 지금은 CSV로 저장 후 수동 업로드 방식입니다.
    """
    rows = _build_sheet_rows(all_results, seed)
    if not rows:
        print("  ⚠️  저장할 데이터 없음")
        return ""

    # ── 방법 1: gspread로 직접 저장 ──────────────────────────
    if sheet_id:
        try:
            import gspread
            from google.oauth2.service_account import Credentials

            creds_path = os.path.join(os.path.dirname(__file__), "credentials.json")
            scope = [
                "https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive",
            ]
            creds = Credentials.from_service_account_file(creds_path, scopes=scope)
            gc    = gspread.authorize(creds)
            sh    = gc.open_by_key(sheet_id)

            try:
                ws = sh.worksheet("keyword_research")
            except gspread.WorksheetNotFound:
                ws = sh.add_worksheet(title="keyword_research", rows=1000, cols=20)
                ws.append_row(list(rows[0].keys()))  # 헤더 추가

            for row in rows:
                ws.append_row(list(row.values()))

            print(f"  ✅ Google Sheets 저장 완료: {len(rows)}행 추가")
            return f"https://docs.google.com/spreadsheets/d/{sheet_id}"

        except ImportError:
            print("  ⚠️  gspread 미설치 → pip install gspread google-auth")
        except Exception as e:
            print(f"  ⚠️  Sheets 직접 저장 실패: {e}")

    # ── 방법 2: CSV 파일 저장 (fallback) ─────────────────────
    if csv_fallback:
        import csv
        ts       = datetime.now().strftime("%Y%m%d_%H%M")
        filename = f"keyword_research_{seed}_{ts}.csv" if seed else f"keyword_research_{ts}.csv"
        out_path = os.path.join(os.path.dirname(__file__), filename)

        with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

        print(f"  ✅ CSV 저장 완료: {out_path}")
        print(f"     → Google Sheets 'keyword_research' 시트에 수동 붙여넣기 하세요")
        return out_path

    return ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  9. 키워드 풀 관리 (최대 200개 유지, 생애주기 관리)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def load_keyword_pool(pool_path: str = None) -> list:
    """
    keyword_pool.csv 로드 → 딕셔너리 리스트 반환.
    파일 없으면 빈 리스트 반환 (첫 실행 시 자동 생성).
    """
    import csv
    path = pool_path or POOL_CSV_PATH
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def save_keyword_pool(pool: list, pool_path: str = None):
    """
    키워드 풀을 grade 내림차순 → total_score 내림차순으로 정렬 후 CSV 저장.
    최대 POOL_MAX_SIZE(200)개만 유지.

    [ status 값 ]
    active      → 활성 (포스팅 후보)
    used        → 글 작성 파이프라인으로 넘어간 항목 (다음 저장 시 제거)
    reviving    → 재평가 진행 중 (임시 상태)
    deprecated  → 재평가 후 미달 판정 → 다음 저장 시 제거
    """
    import csv
    path = pool_path or POOL_CSV_PATH

    # 정렬: 황금 > 좋은 > 보통 > 미달 → 같은 등급 내에서는 총점 내림차순
    pool_sorted = sorted(
        pool,
        key=lambda r: (
            -GRADE_ORDER.get(r.get("grade", "미달"), 0),
            -float(r.get("total_score", 0) or 0),
        ),
    )
    pool_sorted = pool_sorted[:POOL_MAX_SIZE]  # 최대 크기 보장

    if not pool_sorted:
        return

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=POOL_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in pool_sorted:
            for col in POOL_COLUMNS:   # 누락 컬럼 기본값 채우기
                row.setdefault(col, "")
            writer.writerow(row)

    print(f"  💾 키워드 풀 저장 완료: {len(pool_sorted)}개 → {path}")


def get_recommendations(pool: list, n: int = 3) -> list:
    """
    풀에서 status=active 인 키워드 중 상위 n개 추천.
    정렬 기준: grade 내림차순(황금 우선) → total_score 내림차순

    [ 사용 예 ]
    pool = load_keyword_pool()
    recs = get_recommendations(pool, n=3)
    → 오늘의 포스팅 후보 TOP 3 반환
    """
    active = [r for r in pool if r.get("status", "active") == "active"]
    ranked = sorted(
        active,
        key=lambda r: (
            -GRADE_ORDER.get(r.get("grade", "미달"), 0),
            -float(r.get("total_score", 0) or 0),
        ),
    )
    return ranked[:n]


def _re_evaluate_keyword(row: dict) -> tuple:
    """
    기존 풀 항목의 점수를 재조회·재계산 (DataLab + Blog + Google Trends 재호출).
    반환: (업데이트된 row dict, new_total_score)

    [ 주니어 개발자에게 ]
    이 함수는 --manage 모드에서만 호출돼요 (--seed 실행 시에는 호출 안 함).
    API를 재호출하므로 키워드 수가 많으면 시간이 오래 걸릴 수 있어요.
    """
    kw      = row.get("keyword", "")
    profile = row.get("profile", "일반")
    weights = LONGTAIL_WEIGHTS if profile == "롱테일" else DEFAULT_WEIGHTS

    print(f"    🔄 재평가: [{kw}]")
    dl   = fetch_naver_datalab(kw)
    blog = fetch_naver_blog(kw)
    gt   = fetch_google_trends(kw)
    time.sleep(2.0)

    scored = score_keyword(kw, dl, blog, gt, weights=weights)
    total  = scored.get("total_score", 0)

    if   total >= 80: grade = "황금"
    elif total >= 60: grade = "좋은"
    elif total >= 40: grade = "보통"
    else            : grade = "미달"

    hits = [w for w in GOLDEN_CRITERIA["commercial_words"] if w in kw]
    weaknesses = []
    for key, val in scored.get("scores", {}).items():
        ratio = val["score"] / val.get("max", 1)
        if "상업"    in key and ratio < 0.5: weaknesses.append("상업의도부족")
        if "Blog"    in key and ratio < 0.4: weaknesses.append("경쟁도높음")
        if "DataLab" in key and ratio < 0.4: weaknesses.append("트렌드낮음")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    row.update({
        "grade"             : grade,
        "total_score"       : total,
        "datalab_score"     : dl.get("trend_score", 0),
        "datalab_direction" : dl.get("trend_direction", ""),
        "blog_results"      : blog.get("total_results", ""),
        "blog_competition"  : blog.get("competition_label", ""),
        "commercial_hits"   : ", ".join(hits) if hits else "없음",
        "gtrends_score"     : gt.get("trend_score", 0),
        "weak_points"       : ", ".join(weaknesses) if weaknesses else "-",
        "last_evaluated_at" : now_str,
        "revival_count"     : str(int(row.get("revival_count", 0) or 0) + 1),
    })
    return row, total


def manage_pool(
    new_results : list,
    pool_path   : str  = None,
    max_size    : int  = POOL_MAX_SIZE,
    seed        : str  = "",
    run_revival : bool = False,
) -> list:
    """
    키워드 풀 병합·정리·저장.

    [ 흐름 ]
    1. keyword_pool.csv 로드
    2. 신규 결과 병합 (40점 미만 제외, 중복 키워드는 점수 업데이트)
    3. run_revival=True 일 때: 30일+ 경과 항목 API 재호출 재평가
       - 40점↑ → active 유지 / 미달 → deprecated 표시
    4. deprecated / used 항목 제거
    5. 200개 초과 시 삭제 우선순위: ①점수 낮은 것 → ②생성일 오래된 것
    6. grade 내림차순 → 총점 내림차순 정렬 후 저장

    [ run_revival 플래그 ]
    False (기본) : --seed 실행 시 빠른 병합만 수행
    True         : --manage 실행 시 전체 재평가 포함

    반환: 정리된 풀 (list of dict)
    """
    path    = pool_path or POOL_CSV_PATH
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── 1. 기존 풀 로드 ───────────────────────────────────
    pool     = load_keyword_pool(path)
    pool_map = {r["keyword"]: r for r in pool}
    print(f"\n  📦 키워드 풀 현황: {len(pool)}개 / 최대 {max_size}개")

    # ── 2. 신규 결과 병합 ─────────────────────────────────
    new_rows      = _build_sheet_rows(new_results, seed)
    added_count   = 0
    updated_count = 0

    for nr in new_rows:
        kw = nr.get("keyword", "")
        if not kw:
            continue
        total = float(nr.get("total_score", 0) or 0)
        if total < 40:   # 미달 키워드는 풀에 추가하지 않음
            continue

        if kw in pool_map:
            existing = pool_map[kw]
            # used 상태(파이프라인 진행 중)는 점수만 업데이트하고 status 유지
            nr["status"]            = existing.get("status", "active")
            nr["revival_count"]     = existing.get("revival_count", "0")
            nr["created_at"]        = existing.get("created_at", now_str)
            nr["last_evaluated_at"] = now_str
            pool_map[kw] = nr
            updated_count += 1
        else:
            nr["status"]            = "active"
            nr["last_evaluated_at"] = now_str
            nr["revival_count"]     = "0"
            nr.setdefault("created_at", now_str)
            pool_map[kw] = nr
            added_count += 1

    print(f"  ➕ 신규 추가: {added_count}개  🔄 업데이트: {updated_count}개")

    # ── 3. 부활 심사 (--manage 모드에서만) ────────────────
    revival_kept    = 0
    revival_dropped = 0

    if run_revival:
        now             = datetime.now()
        revival_targets = []

        for kw, row in pool_map.items():
            if row.get("status") != "active":
                continue
            le = row.get("last_evaluated_at") or row.get("created_at", "")
            try:
                le_dt    = datetime.strptime(le[:16], "%Y-%m-%d %H:%M")
                age_days = (now - le_dt).days
            except Exception:
                age_days = 0
            if age_days >= REVIVAL_DAYS:
                revival_targets.append(kw)

        if revival_targets:
            print(f"\n  🔁 부활 심사 대상: {len(revival_targets)}개 ({REVIVAL_DAYS}일+ 경과)")
            for kw in revival_targets:
                updated_row, new_total = _re_evaluate_keyword(pool_map[kw])
                if new_total >= 40:
                    updated_row["status"] = "active"
                    pool_map[kw] = updated_row
                    revival_kept += 1
                    print(f"    ✅ [{kw}] {new_total}점 → 부활!")
                else:
                    updated_row["status"] = "deprecated"
                    pool_map[kw] = updated_row
                    revival_dropped += 1
                    print(f"    🗑️  [{kw}] {new_total}점 → deprecated 처리")
        else:
            print(f"  ✅ 부활 심사 대상 없음 ({REVIVAL_DAYS}일 미만 항목만 있음)")

    # ── 4. deprecated / used 제거 ─────────────────────────
    before_clean = len(pool_map)
    pool_map = {
        kw: row for kw, row in pool_map.items()
        if row.get("status") not in ("deprecated", "used")
    }
    removed = before_clean - len(pool_map)
    if removed:
        print(f"  🗑️  deprecated/used 제거: {removed}개")

    # ── 5. 풀 크기 초과 시 삭제 ──────────────────────────
    pool_list = list(pool_map.values())
    if len(pool_list) > max_size:
        # 삭제 우선순위: ①총점 낮은 것 ②생성일 오래된 것 (문자열 오름차순 = 오래된 순)
        pool_list.sort(key=lambda r: (
            float(r.get("total_score", 0) or 0),
            r.get("created_at", "9999-99-99"),
        ))
        over       = len(pool_list) - max_size
        to_delete  = pool_list[:over]
        pool_list  = pool_list[over:]
        print(f"  ✂️  풀 크기 초과 → {over}개 삭제 (점수 낮은 순)")
        for r in to_delete[:5]:
            print(f"    - [{r['keyword']}] {r.get('grade','')} {r.get('total_score','')}점")
        if over > 5:
            print(f"    ... 외 {over-5}개")

    # ── 6. 정렬 후 저장 ───────────────────────────────────
    save_keyword_pool(pool_list, path)

    active_cnt = sum(1 for r in pool_list if r.get("status") == "active")
    print(f"  📊 풀 최종: {len(pool_list)}개 (active: {active_cnt}개)")
    if run_revival and (revival_kept or revival_dropped):
        print(f"  🔁 부활 결과: 유지 {revival_kept}개 / 삭제 {revival_dropped}개")

    return pool_list


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  10. 단건 분석 파이프라인
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def analyze(keyword: str) -> dict:
    """단일 키워드 전체 분석 파이프라인"""
    print(f"\n  🔍 [{keyword}] 분석 시작...")

    print("  ① DataLab 트렌드 조회...")
    dl = fetch_naver_datalab(keyword)

    print("  ② 블로그 경쟁도 조회...")
    blog = fetch_naver_blog(keyword)

    print("  ③ Google Trends 조회...")
    gt = fetch_google_trends(keyword)
    time.sleep(1.2)

    scored = score_keyword(keyword, dl, blog, gt)
    print_report(keyword, dl, blog, gt, scored)

    return {
        "keyword"      : keyword,
        "naver_datalab": dl,
        "naver_blog"   : blog,
        "google_trends": gt,
        "golden_score" : scored,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  10. 씨앗 키워드 → 황금 키워드 TOP N 파이프라인
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def quick_score_commercial(keyword: str) -> int:
    """상업적 의도만으로 빠른 사전 점수 (API 호출 없음). 1차 필터용."""
    hits = [w for w in GOLDEN_CRITERIA["commercial_words"] if w in keyword]
    return 20 if len(hits) >= 2 else 15 if len(hits) == 1 else 0


def analyze_seed(seed: str, top_n: int = 5) -> list:
    """
    씨앗 키워드 → 황금 키워드 TOP N 선정 전체 파이프라인.

    [ 흐름 ]
    Step 1. 자동완성 + 접미어 조합으로 후보 최대 20개 확보
    Step 2. 상업적 의도 사전 점수로 상위 15개로 좁히기 (API 호출 없음)
    Step 3. 15개에 DataLab + Blog API 호출 → 중간 점수 계산
    Step 4. 상위 top_n개에만 Google Trends 조회 (rate limit 고려)
    Step 5. 최종 점수 계산 및 랭킹 출력
    """
    SEP = "═" * 60
    print(f"\n{SEP}")
    print(f"  🌱 씨앗 키워드: [{seed}]  →  황금 키워드 TOP {top_n} 탐색")
    print(f"{SEP}")

    # ── Step 1: 씨앗 확장 ────────────────────────────────────
    print("\n  【 Step 1 】 씨앗 키워드 확장...")
    candidates = expand_seed(seed)

    # ── Step 2: 상업적 의도 사전 필터 (상위 15개) ────────────
    print(f"\n  【 Step 2 】 상업적 의도 사전 점수 계산 ({len(candidates)}개)...")
    for c in candidates:
        pass  # 이미 문자열 리스트

    # 상업적 의도 점수 기준 정렬, 씨앗 자체는 항상 포함
    scored_candidates = sorted(
        candidates,
        key=lambda kw: (0 if kw == seed else -quick_score_commercial(kw))
    )
    top15 = scored_candidates[:15]
    print(f"  ✅ 상위 15개 선별: {top15}")

    # ── Step 3: DataLab + Blog API 조회 (15개) ───────────────
    print(f"\n  【 Step 3 】 DataLab + Blog 경쟁도 조회 ({len(top15)}개)...")
    enriched = []
    for i, kw in enumerate(top15):
        print(f"    [{i+1:2}/{len(top15)}] {kw}")
        dl   = fetch_naver_datalab(kw)
        blog = fetch_naver_blog(kw)

        # 중간 점수 계산 (DataLab + Blog + 상업의도)
        dl_dir  = dl.get("trend_direction", "")
        dl_s    = dl.get("trend_score", 0)
        dl_base = 35 if dl_s >= 70 else 27 if dl_s >= 50 else 18 if dl_s >= 30 else 10 if dl_s >= 10 else 4
        dl_adj  = 5 if "상승중" in dl_dir else -5 if "하락중" in dl_dir else 0
        mid_score = (
            max(0, min(40, dl_base + dl_adj))
            + blog.get("competition_score", 15)
            + quick_score_commercial(kw)
        )

        enriched.append({
            "keyword": kw,
            "_dl"    : dl,
            "_blog"  : blog,
            "_mid"   : mid_score,
        })
        time.sleep(0.4)

    # 중간 점수로 정렬 → 상위 top_n 선별
    enriched.sort(key=lambda x: x["_mid"], reverse=True)
    top_final = enriched[:top_n]

    # ── Step 4: Google Trends (상위 top_n만) ─────────────────
    print(f"\n  【 Step 4 】 Google Trends 조회 (상위 {top_n}개)...")
    for i, c in enumerate(top_final):
        print(f"    [{i+1}/{top_n}] {c['keyword']}")
        c["_gt"] = fetch_google_trends(c["keyword"])
        time.sleep(2.0)   # 429 방지: 넉넉하게 대기

    # ── Step 5: 최종 점수 계산 ──────────────────────────────
    print(f"\n  【 Step 5 】 최종 점수 계산 및 랭킹...")
    final_results = []
    for c in top_final:
        scored = score_keyword(c["keyword"], c["_dl"], c["_blog"], c["_gt"])
        final_results.append({
            "keyword"      : c["keyword"],
            "naver_datalab": c["_dl"],
            "naver_blog"   : c["_blog"],
            "google_trends": c["_gt"],
            "golden_score" : scored,
        })

    final_results.sort(key=lambda x: x["golden_score"]["total_score"], reverse=True)

    # ── 최종 결과 출력 ───────────────────────────────────────
    print(f"\n{SEP}")
    print(f"  🏆 [{seed}] 황금 키워드 TOP {top_n} 결과  (일반 프로필 기준)")
    print(f"{SEP}")
    print(f"  {'순위':<4} {'키워드':<22} {'DataLab':>8} {'경쟁도':<10} {'총점':>6}  판정")
    print(f"  {'─'*4} {'─'*22} {'─'*8} {'─'*10} {'─'*6}  {'─'*16}")

    for i, r in enumerate(final_results, 1):
        s      = r["golden_score"]
        star   = "⭐" if s["is_golden"] else "  "
        dl_s   = r["naver_datalab"].get("trend_score", 0)
        blog_l = r["naver_blog"].get("competition_label", "N/A")[:6]
        kw     = r["keyword"][:22]
        print(
            f"  {i}위{star}  {kw:<22} {dl_s:>6.1f}/100  {blog_l:<10} "
            f"{s['total_score']:>5}점  {s['verdict'][:16]}"
        )

    print(f"{SEP}")
    golden = [r for r in final_results if r["golden_score"]["is_golden"]]
    print(f"\n  ✅ 황금 키워드 {len(golden)}개: {', '.join(r['keyword'] for r in golden) or '없음'}")

    if final_results:
        top = final_results[0]
        print(f"\n  💡 최우선 추천: [{top['keyword']}]  ({top['golden_score']['total_score']}점)")
        print(f"     → content_db 'keyword' 컬럼에 등록하세요")

    # ── Step 6: 키워드 연금술 (보통/좋은 키워드 황금 변환 시도) ─
    non_golden = [r for r in final_results if not r["golden_score"]["is_golden"]
                  and r["golden_score"]["total_score"] >= 40]

    alchemy_all = []
    if non_golden:
        print(f"\n{SEP}")
        print(f"  🧪 Step 6 — 키워드 연금술 ({len(non_golden)}개 보통/좋은 키워드 변환 시도)")
        print(f"{SEP}")
        for r in non_golden:
            alchemy_results = run_alchemy(r["keyword"], r["golden_score"])
            alchemy_all.extend(alchemy_results)
            time.sleep(0.5)
    else:
        print(f"\n  ℹ️  연금술 대상 없음 (황금 키워드만 나왔거나 모두 기준 미달)")

    # ── Step 7: keyword_research 시트 저장 + 풀 업데이트 ────
    print(f"\n{SEP}")
    print(f"  💾 Step 7 — keyword_research 시트 저장 + 키워드 풀 업데이트")
    print(f"{SEP}")

    # 미달 제외하고 황금/좋은/보통 + 연금술 결과 모두 저장
    save_targets = [
        r for r in final_results if r["golden_score"]["total_score"] >= 40
    ] + alchemy_all

    sheet_id = os.getenv("GOOGLE_SHEETS_ID", "")
    save_to_keyword_research_sheet(save_targets, seed=seed, sheet_id=sheet_id)

    # 키워드 풀 병합 (200개 한도, run_revival=False → 빠른 병합만)
    manage_pool(save_targets, seed=seed, run_revival=False)

    print()
    return final_results + alchemy_all


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  9. 메인
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    """
    사용 모드:

    1. 씨앗 키워드 확장 모드 (권장)
       python golden_keyword.py --seed "씨앗키워드"
       python golden_keyword.py --seed "씨앗키워드" --top 5

    2. 단건/다건 직접 분석 모드
       python golden_keyword.py "키워드"
       python golden_keyword.py "키워드1,키워드2,키워드3"

    3. 추천 모드 (풀에서 TOP 3 추천, seed 입력 없이 실행)
       python golden_keyword.py --recommend

    4. 풀 관리 모드 (30일+ 경과 항목 재평가 + 정리)
       python golden_keyword.py --manage

    공통 옵션: --json (JSON 출력)
    """
    if len(sys.argv) < 2:
        print(__doc__)
        print(main.__doc__)
        sys.exit(1)

    args     = sys.argv[1:]
    use_json = "--json" in args

    # ── 추천 모드 ─────────────────────────────────────────────
    if "--recommend" in args:
        pool = load_keyword_pool()
        recs = get_recommendations(pool, n=3)
        SEP  = "═" * 60
        print(f"\n{SEP}")
        print(f"  🌟 추천 키워드 TOP 3  (keyword_pool 기준, active 항목)")
        print(f"{SEP}")
        if not recs:
            print("\n  📭 추천 키워드 없음.")
            print("     --seed 로 새 키워드를 탐색하면 풀이 자동으로 채워져요.")
        else:
            print(f"  {'순위':<4} {'키워드':<24} {'등급':<6} {'총점':>5}  씨앗키워드")
            print(f"  {'─'*4} {'─'*24} {'─'*6} {'─'*5}  {'─'*12}")
            for i, r in enumerate(recs, 1):
                kw    = r.get("keyword", "")[:24]
                grade = r.get("grade", "")
                score = r.get("total_score", "")
                seed  = r.get("seed_keyword", "")[:12]
                star  = "⭐" if grade == "황금" else "🥈" if grade == "좋은" else "  "
                print(f"  {i}위{star} {kw:<24} {grade:<6} {score:>5}점  {seed}")
        print(f"{SEP}\n")
        if use_json:
            print(return_json(recs))
        return

    # ── 풀 관리 모드 ───────────────────────────────────────────
    if "--manage" in args:
        SEP = "═" * 60
        print(f"\n{SEP}")
        print(f"  🔧 키워드 풀 관리 모드")
        print(f"     30일+ 경과 항목 재평가 / deprecated·used 제거 / 크기 정리")
        print(f"{SEP}")
        pool = manage_pool([], run_revival=True)
        print(f"\n  ✅ 풀 관리 완료. keyword_pool.csv 를 확인하세요.")

        # 관리 후 추천 키워드 미리 보여주기
        recs = get_recommendations(pool, n=3)
        if recs:
            print(f"\n  현재 추천 키워드 TOP 3:")
            for i, r in enumerate(recs, 1):
                print(f"    {i}위 [{r.get('keyword','')}]  {r.get('grade','')}  {r.get('total_score','')}점")
        print(f"\n{SEP}\n")
        if use_json:
            print(return_json(pool))
        return

    # ── 씨앗 모드 ─────────────────────────────────────────────
    if "--seed" in args:
        idx = args.index("--seed")
        if idx + 1 >= len(args) or args[idx + 1].startswith("--"):
            print("❌ --seed 다음에 씨앗 키워드를 입력하세요")
            print("   예: python golden_keyword.py --seed \"다이어트\"")
            sys.exit(1)

        seed  = args[idx + 1]
        top_n = 5
        if "--top" in args:
            ti = args.index("--top")
            if ti + 1 < len(args):
                try:
                    top_n = int(args[ti + 1])
                except ValueError:
                    pass

        results = analyze_seed(seed, top_n=top_n)
        if use_json:
            print(return_json(results))
        return

    # ── 단건/다건 모드 ────────────────────────────────────────
    flag_set  = {"--seed", "--top", "--json", "--recommend", "--manage"}
    skip_next = False
    kw_args   = []
    for i, a in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if a in flag_set:
            skip_next = True
            continue
        kw_args.append(a)

    if not kw_args:
        print("❌ 키워드를 입력하세요")
        sys.exit(1)

    keywords    = [k.strip() for k in kw_args[0].split(",") if k.strip()]
    all_results = []
    for kw in keywords:
        result = analyze(kw)
        all_results.append(result)
        if len(keywords) > 1:
            time.sleep(2)

    if use_json:
        print(return_json(all_results))

    if len(all_results) > 1:
        ranked = sorted(all_results, key=lambda x: x["golden_score"]["total_score"], reverse=True)
        print("=" * 60)
        print("  🏆 키워드 최종 순위")
        print("=" * 60)
        for i, r in enumerate(ranked, 1):
            ks   = r["golden_score"]
            star = "⭐" if ks["is_golden"] else "  "
            print(f"  {i}위 {star} [{r['keyword']}]  {ks['total_score']}점  {ks['verdict']}")
        print("=" * 60)


if __name__ == "__main__":
    main()
