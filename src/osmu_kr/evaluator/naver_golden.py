"""NaverGoldenEvaluator — 사용자 제공 황금 키워드 분석기 로직 통합.

[ 4축 점수 (총 100점) ]
  · DataLab 트렌드  : 40점 (DEFAULT) / 20점 (LONGTAIL)
  · Blog 경쟁도     : 30점 (DEFAULT) / 45점 (LONGTAIL)
  · 상업적 의도     : 20점 (DEFAULT) / 25점 (LONGTAIL)
  · Google Trends   : 10점

자격증명 없으면 HeuristicEvaluator 로 폴백.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional

from ..models import Evaluation
from .base import BaseEvaluator
from .heuristic import HeuristicEvaluator

log = logging.getLogger(__name__)

COMMERCIAL_WORDS = (
    "추천", "비교", "방법", "가격", "후기", "리뷰", "구매", "순위",
    "장단점", "효과", "종류", "선택", "어떻게", "최고", "베스트",
)
BLOG_COMP = {"very_low": 5_000, "low": 30_000, "medium": 100_000, "high": 500_000}
DEFAULT_WEIGHTS = {"datalab": 40, "blog_comp": 30, "commercial": 20, "gtrends": 10}
LONGTAIL_WEIGHTS = {"datalab": 20, "blog_comp": 45, "commercial": 25, "gtrends": 10}


def _fetch_datalab(keyword, client_id, client_secret):
    if not client_id or not client_secret:
        return {"trend_score": 0, "trend_direction": "데이터없음", "source": "datalab(스킵)"}
    try:
        import requests
    except ImportError:
        return {"trend_score": 0, "trend_direction": "데이터없음", "source": "datalab(requests 미설치)"}
    end = datetime.now()
    start = end - timedelta(days=90)
    body = {
        "startDate": start.strftime("%Y-%m-%d"),
        "endDate": end.strftime("%Y-%m-%d"),
        "timeUnit": "week",
        "keywordGroups": [{"groupName": keyword, "keywords": [keyword]}],
    }
    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
        "Content-Type": "application/json",
    }
    try:
        r = requests.post("https://openapi.naver.com/v1/datalab/search",
                          headers=headers, data=json.dumps(body), timeout=10)
        r.raise_for_status()
        data = r.json()
        periods = data.get("results", [{}])[0].get("data", [])
        if not periods:
            return {"trend_score": 0, "trend_direction": "데이터없음", "source": "naver_datalab"}
        scores = [p.get("ratio", 0) for p in periods]
        avg = round(sum(scores) / len(scores), 1)
        recent = scores[-4:]
        older = scores[:-4] if len(scores) > 4 else scores[:max(1, len(scores)//2)]
        ra = sum(recent) / max(1, len(recent))
        oa = sum(older) / max(1, len(older))
        # score-2: slope 정량화 — (recent_avg - older_avg) / older_avg * 100 (%)
        if oa > 0:
            slope_pct = round((ra - oa) / oa * 100.0, 1)
        else:
            slope_pct = 0.0
        if slope_pct >= 20.0:
            direction = "상승중"
        elif slope_pct <= -20.0:
            direction = "하락중"
        else:
            direction = "유지중"
        return {
            "trend_score": avg,
            "trend_direction": direction,
            "trend_slope_pct": slope_pct,
            "recent_avg": round(ra, 2),
            "older_avg": round(oa, 2),
            "source": "naver_datalab",
        }
    except Exception as e:
        log.warning("[naver_golden] DataLab 실패: %s", e)
        return {"trend_score": 0, "trend_direction": "조회실패", "source": "naver_datalab"}


def _fetch_shopping_signal(keyword, client_id, client_secret):
    """score-5: Naver Shopping Search API — 상품 카운트 → 상업적 의도 보강.

    상품이 많이 매치될수록 ‘상업적’ 신호. Total 0 이면 정보성 키워드 비중↑.
    키 없거나 실패 시 None 반환.
    """
    if not client_id or not client_secret:
        return None
    try:
        import requests
    except ImportError:
        return None
    headers = {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}
    try:
        r = requests.get("https://openapi.naver.com/v1/search/shop.json",
                          headers=headers,
                          params={"query": keyword, "display": 1},
                          timeout=8)
        if r.status_code != 200:
            return None
        total = int(r.json().get("total", 0))
        return total
    except Exception as e:
        log.warning("[naver_golden] Shopping 실패: %s", e)
        return None


def _fetch_blog(keyword, client_id, client_secret, *,
                  recent_window_days: int = 14):
    """Blog 경쟁도 — v13 spec 대로 두 sub-axis 로 분리.

    Returns:
      · total_results       : 누적 글 수
      · competition_score   : 0~15 (총량) — 기존 30점 기준을 절반으로 스케일
      · recent_count        : 최근 N일 발행 수 (Blog Search API 정렬=date)
      · recent_density_score: 0~15 (최근 발행 밀도)
      · combined_score      : 0~30 (두 sub-axis 합)
    """
    if not client_id or not client_secret:
        return {"total_results": None, "recent_count": None,
                "competition_label": "데이터없음",
                "competition_score": 7, "recent_density_score": 7,
                "combined_score": 15, "source": "blog(스킵)"}
    try:
        import requests
    except ImportError:
        return {"total_results": None, "recent_count": None,
                "competition_label": "requests 미설치",
                "competition_score": 7, "recent_density_score": 7,
                "combined_score": 15, "source": "blog(requests 미설치)"}
    headers = {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}

    # ── 1) 총량 ──
    try:
        r = requests.get("https://openapi.naver.com/v1/search/blog.json",
                         headers=headers,
                         params={"query": keyword, "display": 5, "sort": "sim"},
                         timeout=10)
        r.raise_for_status()
        data = r.json()
        total = data.get("total", 0)
        # 0~15 점수로 스케일 (기존 30점 → 절반)
        if total < BLOG_COMP["very_low"]:    total_score, label = 15, "매우 낮음"
        elif total < BLOG_COMP["low"]:        total_score, label = 11, "낮음"
        elif total < BLOG_COMP["medium"]:     total_score, label = 7, "보통"
        elif total < BLOG_COMP["high"]:       total_score, label = 3, "높음"
        else:                                  total_score, label = 0, "매우 높음(레드오션)"
    except Exception as e:
        log.warning("[naver_golden] Blog total 실패: %s", e)
        return {"total_results": None, "recent_count": None,
                "competition_label": "조회실패",
                "competition_score": 7, "recent_density_score": 7,
                "combined_score": 15, "source": "blog(실패)"}

    # ── 2) 최근 N일 발행 밀도 ──
    # Naver Blog Search 의 date 정렬 — 최근 100건 받아서 N일 이내 카운트.
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=recent_window_days)
    recent_count = 0
    try:
        r2 = requests.get("https://openapi.naver.com/v1/search/blog.json",
                            headers=headers,
                            params={"query": keyword, "display": 100, "sort": "date"},
                            timeout=10)
        if r2.status_code == 200:
            items = r2.json().get("items", []) or []
            for it in items:
                pd = it.get("postdate", "") or ""
                # postdate 형식: YYYYMMDD
                if len(pd) == 8 and pd.isdigit():
                    try:
                        dt = datetime(int(pd[:4]), int(pd[4:6]), int(pd[6:8]),
                                       tzinfo=timezone.utc)
                        if dt >= cutoff:
                            recent_count += 1
                    except Exception:
                        continue
    except Exception as e:
        log.warning("[naver_golden] Blog recent 실패: %s", e)
        recent_count = None

    # 0~15 점수 — 최근 발행 수가 적을수록 좋음 (블루오션)
    if recent_count is None:
        recent_score = 7   # 데이터없음 — 중립
    elif recent_count == 0:    recent_score = 15
    elif recent_count <= 3:    recent_score = 12
    elif recent_count <= 10:   recent_score = 8
    elif recent_count <= 30:   recent_score = 4
    else:                      recent_score = 0

    combined = total_score + recent_score
    return {
        "total_results": total,
        "recent_count": recent_count,
        "competition_label": label,
        "competition_score": total_score,        # 0~15
        "recent_density_score": recent_score,    # 0~15
        "combined_score": combined,              # 0~30
        "source": "naver_blog",
    }


def _fetch_trends(keyword, enabled=True):
    if not enabled:
        return {"trend_score": 0, "trend_direction": "스킵", "source": "skipped"}
    try:
        from pytrends.request import TrendReq
        if not hasattr(_fetch_trends, "_pt"):
            _fetch_trends._pt = TrendReq(hl="ko", tz=540, timeout=(10, 25),
                                          retries=1, backoff_factor=1.5)
        pt = _fetch_trends._pt
        pt.build_payload([keyword], timeframe="today 3-m", geo="KR")
        df = pt.interest_over_time()
        if df.empty or keyword not in df.columns:
            return {"trend_score": 0, "trend_direction": "데이터없음", "source": "google_trends"}
        scores = df[keyword].tolist()
        avg = round(sum(scores) / len(scores), 1)
        recent = scores[-4:]
        older = scores[:-4] if len(scores) > 4 else scores[:max(1, len(scores)//2)]
        ra = sum(recent) / max(1, len(recent))
        oa = sum(older) / max(1, len(older))
        direction = "상승중" if ra > oa * 1.2 else ("하락중" if ra < oa * 0.8 else "유지중")
        return {"trend_score": avg, "trend_direction": direction, "source": "google_trends"}
    except ImportError:
        return {"trend_score": 0, "trend_direction": "데이터없음", "source": "pytrends 미설치"}
    except Exception as e:
        log.warning("[naver_golden] Trends 실패: %s", e)
        return {"trend_score": 0, "trend_direction": "조회실패", "source": "google_trends"}


def _score(keyword, dl, blog, gt, weights):
    w_dl, w_bl, w_co, w_gt = weights["datalab"], weights["blog_comp"], weights["commercial"], weights["gtrends"]
    dl_raw = dl.get("trend_score", 0)
    dl_dir = dl.get("trend_direction", "")
    dl_src = dl.get("source", "")
    dl_slope = float(dl.get("trend_slope_pct", 0.0) or 0.0)
    if "스킵" in dl_src or "실패" in dl_src or "데이터없음" in dl_dir:
        dl_score = int(w_dl * 0.38)
    else:
        if dl_raw >= 70: base = 35
        elif dl_raw >= 50: base = 27
        elif dl_raw >= 30: base = 18
        elif dl_raw >= 10: base = 10
        else: base = 4
        # score-2: slope 정량화 — 라벨이 아니라 수치 기반 보정 (-7..+7)
        slope_adj = max(-7.0, min(7.0, dl_slope / 10.0))
        raw = max(0, min(40, base + slope_adj))
        dl_score = max(0, min(w_dl, round(raw * w_dl / 40)))

    # v13: 총량(0~15) + 최근 14일 밀도(0~15) = combined(0~30)
    raw_bl = blog.get("combined_score", blog.get("competition_score", 15))
    if blog.get("total_results") is None:
        bl_score = int(w_bl * 0.50)
    else:
        bl_score = max(0, min(w_bl, round(raw_bl * w_bl / 30)))

    # score-5: 텍스트 매칭 + Shopping API 카운트 하이브리드
    hits = [w for w in COMMERCIAL_WORDS if w in keyword]
    if len(hits) >= 2: com_base = w_co
    elif len(hits) == 1: com_base = round(w_co * 0.75)
    else: com_base = round(w_co * 0.20)

    # Shopping API total — None 이면 텍스트 매칭만 사용
    shop_total = dl.get("shopping_total") if isinstance(dl, dict) else None
    if shop_total is not None:
        if shop_total >= 50_000:    shop_adj = round(w_co * 0.20)
        elif shop_total >= 5_000:   shop_adj = round(w_co * 0.10)
        elif shop_total >= 500:     shop_adj = round(w_co * 0.05)
        elif shop_total > 0:        shop_adj = 0
        else:                       shop_adj = -round(w_co * 0.20)
        com_score = max(0, min(w_co, com_base + shop_adj))
    else:
        com_score = com_base

    gt_raw = gt.get("trend_score", 0)
    gt_dir = gt.get("trend_direction", "")
    gt_src = gt.get("source", "")
    if "실패" in gt_src or "데이터없음" in gt_dir or "스킵" in gt_dir or gt_src == "skipped":
        gt_score = int(w_gt * 0.50)
    else:
        if gt_raw >= 70: raw_gt = 10
        elif gt_raw >= 40: raw_gt = 7
        elif gt_raw >= 20: raw_gt = 4
        else: raw_gt = 2
        if "상승" in gt_dir: raw_gt = min(10, raw_gt + 1)
        gt_score = max(0, min(w_gt, round(raw_gt * w_gt / 10)))

    total = int(dl_score + bl_score + com_score + gt_score)
    return {"total_score": total, "dl_score": dl_score, "bl_score": bl_score,
            "com_score": com_score, "gt_score": gt_score,
            "weights": dict(weights), "commercial_hits": hits}


def _to_evaluation(keyword, dl, blog, gt, scored, profile):
    total = scored["total_score"]
    blog_total = blog.get("total_results")
    if blog_total is None:
        comp_kr = "낮음"
    elif blog_total < BLOG_COMP["low"]:
        comp_kr = "낮음"
    elif blog_total < BLOG_COMP["medium"]:
        comp_kr = "중간"
    else:
        comp_kr = "높음"
    # search_volume: Search Ad API 절대값이 있으면 우선, 없으면 DataLab 추정치
    if dl.get("abs_monthly_qc") is not None:
        sv_estimate = int(dl["abs_monthly_qc"])
    else:
        sv_estimate = int(round(float(dl.get("trend_score", 0) or 0) * 300))
    n_hits = len(scored.get("commercial_hits", []))
    commercial_intent = min(1.0, 0.2 + n_hits * 0.20)
    return Evaluation(
        search_volume=sv_estimate, competition=comp_kr, cpc=0.0,
        commercial_intent=round(commercial_intent, 3), score=float(total),
        raw={
            "evaluator": "naver_golden", "profile": profile,
            "datalab": dl, "blog": blog, "google_trends": gt,
            "components": {
                "datalab": scored["dl_score"], "blog": scored["bl_score"],
                "commercial": scored["com_score"], "gtrends": scored["gt_score"],
            },
            # score-1: blog sub-axis 가시화
            "blog_subaxes": {
                "total_score":         blog.get("competition_score"),
                "recent_density_score": blog.get("recent_density_score"),
                "recent_count":         blog.get("recent_count"),
                "window_days":          blog.get("window_days", 14),
            },
            "weights": scored["weights"], "commercial_hits": scored.get("commercial_hits", []),
        },
    )


class NaverGoldenEvaluator(BaseEvaluator):
    name = "naver_golden"

    def __init__(self, naver_client_id=None, naver_client_secret=None,
                 datalab_client_id=None, datalab_client_secret=None,
                 enable_google_trends=True, request_delay_sec=0.4):
        self.naver_id = naver_client_id or os.getenv("NAVER_CLIENT_ID", "")
        self.naver_secret = naver_client_secret or os.getenv("NAVER_CLIENT_SECRET", "")
        self.datalab_id = datalab_client_id or os.getenv("NAVER_DATALAB_CLIENT_ID", "") or self.naver_id
        self.datalab_secret = datalab_client_secret or os.getenv("NAVER_DATALAB_CLIENT_SECRET", "") or self.naver_secret
        self.enable_trends = enable_google_trends
        self.delay = request_delay_sec
        self._fallback = HeuristicEvaluator()
        self._gt_call_count = 0

    @property
    def has_naver_credentials(self):
        return bool(self.naver_id and self.naver_secret)

    def evaluate(self, keyword, *, seed=""):
        return self._with_profile(keyword, weights=DEFAULT_WEIGHTS, profile="일반", seed=seed)

    def evaluate_longtail(self, keyword, *, seed=""):
        return self._with_profile(keyword, weights=LONGTAIL_WEIGHTS, profile="롱테일", seed=seed)

    def _with_profile(self, keyword, *, weights, profile, seed):
        kw = (keyword or "").strip()
        if not kw:
            return Evaluation()
        if not self.has_naver_credentials:
            ev = self._fallback.evaluate(kw, seed=seed)
            ev.raw = {**ev.raw, "evaluator": "naver_golden(fallback→heuristic)",
                      "reason": "NAVER_CLIENT_ID/SECRET 미설정"}
            return ev
        dl = _fetch_datalab(kw, self.datalab_id, self.datalab_secret)
        if self.delay: time.sleep(self.delay)
        # score-3: Search Ad API 절대 월 검색량 (있으면 dl.trend_score 와 병행 기록)
        try:
            from .naver_search_ad import monthly_search_volume
            abs_vol = monthly_search_volume(kw)
        except Exception:
            abs_vol = None
        if abs_vol is not None:
            dl["abs_monthly_qc"] = abs_vol
            dl["source"] = (dl.get("source") or "") + "+searchad"
        # score-5: Shopping API total — 상업적 의도 보강
        shop_total = _fetch_shopping_signal(kw, self.naver_id, self.naver_secret)
        if shop_total is not None:
            dl["shopping_total"] = shop_total
        # score-1: 최근 N일 발행 밀도 window 는 config 에서 (default 14)
        recent_days = getattr(self, "recent_density_window_days", 14)
        blog = _fetch_blog(kw, self.naver_id, self.naver_secret,
                            recent_window_days=recent_days)
        if self.delay: time.sleep(self.delay)
        gt_enabled = self.enable_trends and self._gt_call_count < 5
        gt = _fetch_trends(kw, enabled=gt_enabled)
        if gt_enabled and "skipped" not in gt.get("source", ""):
            self._gt_call_count += 1
        scored = _score(kw, dl, blog, gt, weights)
        return _to_evaluation(kw, dl, blog, gt, scored, profile=profile)


def grade_of(score: float) -> str:
    if score >= 80: return "황금"
    if score >= 60: return "좋은"
    if score >= 40: return "보통"
    return "미달"


def diagnose_weakness(ev: Evaluation):
    raw = ev.raw or {}
    comp = raw.get("components") or {}
    weights = raw.get("weights") or {}
    weak = []
    if comp and weights:
        if (comp.get("commercial", 0) / max(1, weights.get("commercial", 20))) < 0.50:
            weak.append("상업의도_부족")
        if (comp.get("blog", 0) / max(1, weights.get("blog_comp", 30))) < 0.40:
            weak.append("경쟁도_높음")
        if (comp.get("datalab", 0) / max(1, weights.get("datalab", 40))) < 0.40:
            weak.append("트렌드_낮음")
    else:
        if ev.commercial_intent < 0.5:
            weak.append("상업의도_부족")
        if ev.competition in ("중간", "높음"):
            weak.append("경쟁도_높음")
    return weak or ["상업의도_부족", "경쟁도_높음"]
