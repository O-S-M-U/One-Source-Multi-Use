"""seed → 후보 키워드 (네이버 자동완성 + 접미어 + 정규화 중복 제거)."""
from __future__ import annotations

import logging
from typing import List

log = logging.getLogger(__name__)

EXPANSION_SUFFIXES = [
    "추천", "방법", "비교", "후기", "가격",
    "순위", "장단점", "효과", "종류", "주의사항",
]
INTENT_MODIFIERS = ["추천", "비교", "방법", "순위", "후기", "가격", "리뷰", "best", "TOP5"]
SCOPE_MODIFIERS = ["직장인", "초보자", "2025", "단기간", "주말", "집에서", "20대", "30대"]
QUESTION_MODIFIERS = ["무엇", "어떻게", "왜", "차이"]


def fetch_naver_autocomplete(seed: str, limit: int = 10) -> List[str]:
    try:
        import requests
    except ImportError:
        return []
    url = "https://ac.search.naver.com/nx/ac"
    params = {"q": seed, "q_enc": "utf-8", "st": "111",
              "frm": "nv", "r_format": "json", "r_enc": "utf-8"}
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"),
        "Referer": "https://www.naver.com",
    }
    try:
        r = requests.get(url, params=params, headers=headers, timeout=8)
        r.raise_for_status()
        data = r.json()
        items = data.get("items", [[]])
        if not items or not items[0]:
            return []
        return [it[0] for it in items[0] if it and it[0].strip()][:limit]
    except Exception as e:
        log.info("[expander] 자동완성 실패 (정상 폴백): %s", e)
        return []


def _dedup_with_normalize(candidates):
    seen_surface, seen_norm, out = set(), set(), []
    for kw in candidates:
        kw = " ".join((kw or "").split())
        norm = kw.replace(" ", "").lower()
        if kw and kw not in seen_surface and norm not in seen_norm:
            seen_surface.add(kw)
            seen_norm.add(norm)
            out.append(kw)
    return out


def fetch_searchad_related(seed: str, limit: int = 30) -> List[str]:
    """score-4: Naver Search Ad API keywordstool 의 연관 키워드. 정확도 ⭐⭐⭐.

    키 없거나 호출 실패 시 빈 리스트.
    """
    try:
        from ..evaluator.naver_search_ad import related_keywords
        return related_keywords(seed, limit=limit) or []
    except Exception as e:
        log.warning("[expander] searchad related 실패: %s", e)
        return []


def expand(seed: str, limit: int = 10, *,
            use_autocomplete: bool = True,
            use_searchad: bool = True) -> List[str]:
    """씨드 → 후보 키워드.

    score-4 우선순위: searchad keywordstool > 자동완성 > 접미어 룰.
    """
    s = (seed or "").strip()
    if not s:
        return []
    searchad = fetch_searchad_related(s, limit=30) if use_searchad else []
    autocomplete = fetch_naver_autocomplete(s, limit=10) if use_autocomplete else []
    suffix_keywords = [f"{s} {sfx}" for sfx in EXPANSION_SUFFIXES]
    return _dedup_with_normalize(
        [s] + searchad + autocomplete + suffix_keywords
    )[:limit]
