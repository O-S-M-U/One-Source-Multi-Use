"""Naver 검색광고 API Evaluator (stub) — 자격증명 없으면 휴리스틱 폴백."""
from __future__ import annotations

import os
from typing import Optional

from ..models import Evaluation
from .base import BaseEvaluator
from .heuristic import HeuristicEvaluator


class NaverAdsEvaluator(BaseEvaluator):
    name = "naver_ads"

    def __init__(self, api_key=None, secret=None, customer_id=None):
        self.api_key = api_key or os.getenv("NAVER_AD_API_KEY")
        self.secret = secret or os.getenv("NAVER_AD_SECRET")
        self.customer_id = customer_id or os.getenv("NAVER_AD_CUSTOMER_ID")
        self._fallback = HeuristicEvaluator()

    @property
    def has_credentials(self) -> bool:
        return all([self.api_key, self.secret, self.customer_id])

    def evaluate(self, keyword: str, *, seed: str = "") -> Evaluation:
        ev = self._fallback.evaluate(keyword, seed=seed)
        if not self.has_credentials:
            ev.raw = {**ev.raw, "evaluator": "naver_ads(fallback→heuristic)"}
        else:
            ev.raw = {**ev.raw, "evaluator": "naver_ads(stub)"}
        return ev
