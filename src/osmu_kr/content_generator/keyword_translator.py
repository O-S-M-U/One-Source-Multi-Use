"""한글 키워드 → 영어 검색 키워드 + 파일명 슬러그.

[ 두 가지 함수 ]
  · translate_to_english_queries(keyword)  → list[str]
       Unsplash 등 영어 검색 엔진용 키워드 후보 생성.
       예) "직장인 다이어트 식단" → ["diet meal", "healthy meal", "office diet food"]
  · keyword_to_slug(keyword)               → str
       파일명 prefix 슬러그. 영어 소문자 + 하이픈.
       예) "직장인 다이어트 식단" → "office-diet-meal"

[ 확장 포인트 ]
  · KEYWORD_MAP 사전이 핵심. 새 도메인(가전/패션/금융) 키워드를 추가하면 즉시 반영.
  · 향후 LLM 기반 번역기로 교체 가능 — translate_to_english_queries 한 함수만 swap.
"""
from __future__ import annotations

import re
import unicodedata
from typing import List


# ── 도메인별 키워드 맵 (자주 쓰는 한국어 → 영어) ─────────
# 짧고 일반적인 단어 위주. 매칭 안 되면 keyword 자체를 그대로 사용.
KEYWORD_MAP: dict[str, str] = {
    # 일반
    "추천": "best", "비교": "comparison", "방법": "how to",
    "후기": "review", "가격": "price", "리뷰": "review",
    "순위": "ranking", "TOP5": "top 5", "최고": "best",

    # 라이프스타일 / 다이어트
    "다이어트": "diet", "식단": "meal", "건강": "healthy",
    "운동": "exercise workout", "헬스": "fitness", "요가": "yoga",
    "수면": "sleep", "스트레스": "stress relief",

    # 대상
    "직장인": "office worker", "여성": "women", "남성": "men",
    "20대": "20s young adult", "30대": "30s adult", "40대": "40s",
    "50대": "50s middle aged", "초보자": "beginner",
    "주부": "homemaker", "임산부": "pregnant",

    # 시간/상황
    "단기간": "quick short term", "장기": "long term",
    "주말": "weekend", "아침": "morning", "저녁": "evening",
    "집에서": "at home indoor", "야외": "outdoor",

    # 금융/재테크
    "재테크": "investment", "주식": "stock market",
    "부동산": "real estate", "ETF": "etf investment",
    "AI ETF": "ai stock etf",
    # 'AI' 약어는 영어 토큰 그대로 사용 (KEYWORD_MAP 에서 제외)
    "투자": "investment", "수익": "profit return",

    # 음식
    "요리": "cooking", "레시피": "recipe", "샐러드": "salad",
    "식품": "food", "간식": "snack",

    # IT
    "노트북": "laptop", "스마트폰": "smartphone phone",
    "챗GPT": "chatgpt ai", "GPT": "gpt ai chatbot",
}
# AI 같은 약어는 영어 토큰으로 그대로 인식되므로 KEYWORD_MAP 에 둘 필요 없다.
# 'AI' 토큰은 _split_tokens 후 isalpha 검사에서 그대로 'ai' 소문자로 사용됨.

# 슬러그 변환에 쓰는 짧은 키 (위 사전의 첫 단어만 추출하기 위한 보조)
SLUG_FIRST_WORD = {
    k: v.split()[0] for k, v in KEYWORD_MAP.items()
}


def _normalize(text: str) -> str:
    """공백 정규화 + 양 끝 특수문자 정리."""
    t = unicodedata.normalize("NFKC", text or "").strip()
    t = re.sub(r"\s+", " ", t)
    return t


def _split_tokens(keyword: str) -> List[str]:
    """공백 + 흔한 구분자 기준 분할."""
    parts = re.split(r"[\s/,·\-_]+", _normalize(keyword))
    return [p for p in parts if p]


def translate_to_english_queries(keyword: str, *, max_queries: int = 3) -> List[str]:
    """한글 키워드 → 영어 검색 키워드 후보 N개.

    매칭되는 단어가 없으면 원본 그대로(영문 키워드 가정)를 첫 후보로 둔다.
    """
    kw = _normalize(keyword)
    if not kw:
        return []

    tokens = _split_tokens(kw)
    en_tokens: List[str] = []
    untranslated: List[str] = []
    for t in tokens:
        en = KEYWORD_MAP.get(t)
        if en:
            en_tokens.append(en)
        else:
            # 영문/숫자만으로 구성됐으면 그대로 사용
            if re.fullmatch(r"[A-Za-z0-9]+", t):
                en_tokens.append(t.lower())
            else:
                untranslated.append(t)

    if not en_tokens:
        # 번역 매칭이 전혀 없으면 원본 키워드 그대로 (영문일 가능성)
        return [kw]

    base = " ".join(dict.fromkeys(en_tokens))   # dedup
    queries = [base]

    # 변형 후보 — 핵심 단어 2개로 좁힌 버전
    short = " ".join(dict.fromkeys(en_tokens))
    short_words = short.split()
    if len(short_words) > 2:
        queries.append(" ".join(short_words[-2:]))   # 마지막 두 단어
        queries.append(" ".join(short_words[:2]))    # 처음 두 단어
    elif len(short_words) == 2:
        # 'healthy' 강조 변형
        queries.append(f"healthy {short_words[1]}")

    # dedup + 최대 N개
    seen, out = set(), []
    for q in queries:
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            out.append(q)
        if len(out) >= max_queries:
            break
    return out


def keyword_to_slug(keyword: str, *, max_words: int = 4) -> str:
    """한글/혼합 키워드 → 파일명 slug (영어 소문자 + 하이픈).

    예시:
      "직장인 다이어트 식단"   → "office-diet-meal"
      "AI ETF 추천 2025"       → "ai-etf-best-2025"
      "diet recipe"            → "diet-recipe"
    """
    kw = _normalize(keyword)
    if not kw:
        return "image"

    tokens = _split_tokens(kw)
    slug_parts: List[str] = []
    for t in tokens:
        first = SLUG_FIRST_WORD.get(t)
        if first:
            slug_parts.append(first.lower())
        elif re.fullmatch(r"[A-Za-z0-9]+", t):
            slug_parts.append(t.lower())
        # 모르는 한글 단어는 슬러그에서 제외 (의미 깨질 위험)

    if not slug_parts:
        # 완전히 매칭이 없으면 영문 fallback — ascii 만 추출
        ascii_only = re.sub(r"[^a-zA-Z0-9]+", "-", kw)
        ascii_only = re.sub(r"-+", "-", ascii_only).strip("-").lower()
        return ascii_only or "image"

    # dedup 유지 (순서 보존)
    seen = set()
    deduped = []
    for p in slug_parts:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    return "-".join(deduped[:max_words])


def make_filename(slug: str, index: int, ext: str = "jpg") -> str:
    """${slug}-${index}.${ext} 형태의 파일명."""
    safe_slug = slug or "image"
    safe_ext = (ext or "jpg").lstrip(".").lower()
    return f"{safe_slug}-{index}.{safe_ext}"


def make_alt_text(keyword: str, index: int, role: str = "") -> str:
    """SEO 친화 alt 텍스트 — 한글 키워드 + 역할이 있으면 역할까지 포함."""
    kw = (keyword or "").strip()
    role_label = ROLE_KO.get(role, "")
    if kw and role_label:
        return f"{kw} — {role_label}"
    if kw:
        return f"{kw} 관련 이미지 {index}"
    return f"본문 이미지 {index}"


# 이미지 역할 — 콘텐츠 섹션과 매핑
IMAGE_ROLES = ("concept", "example", "comparison", "summary")
ROLE_KO = {
    "concept":    "개념 설명",
    "example":    "실제 활용 사례",
    "comparison": "비교 및 주의사항",
    "summary":    "핵심 요약",
}


def role_for_index(index_one_based: int) -> str:
    """1번부터 IMAGE_ROLES 순서대로 매핑. 4번 초과 시 마지막 role(summary) 반복."""
    if index_one_based <= 0:
        return ""
    if index_one_based > len(IMAGE_ROLES):
        return IMAGE_ROLES[-1]
    return IMAGE_ROLES[index_one_based - 1]


def caption_for_role(keyword: str, role: str) -> str:
    """figcaption 용 짧은 설명. 본문에 자연스럽게 녹아들 수 있게 한국어로."""
    kw = (keyword or "").strip()
    if role == "concept":
        return f"{kw}의 기본 개념을 시각적으로 보여주는 장면"
    if role == "example":
        return f"{kw}을(를) 실제로 활용하는 모습"
    if role == "comparison":
        return f"{kw} 선택 시 주의 깊게 살펴봐야 할 부분"
    if role == "summary":
        return f"{kw} 핵심 요약을 떠올리게 하는 이미지"
    return f"{kw} 관련 이미지"
