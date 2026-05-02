"""휴리스틱 평가기."""
from __future__ import annotations

import hashlib
from typing import Tuple

from ..models import Evaluation
from .base import BaseEvaluator

COMMERCIAL_KEYWORDS = (
    "추천", "비교", "방법", "순위", "리뷰", "후기", "가격", "가성비",
    "best", "top", "vs", "차이",
)
COMPETITIONS = ("낮음", "중간", "높음")


class HeuristicEvaluator(BaseEvaluator):
    name = "heuristic"

    @staticmethod
    def _seeded_floats(keyword: str) -> Tuple[float, float, float]:
        h = hashlib.md5(keyword.encode("utf-8")).hexdigest()
        a = int(h[0:8], 16) / 0xFFFFFFFF
        b = int(h[8:16], 16) / 0xFFFFFFFF
        c = int(h[16:24], 16) / 0xFFFFFFFF
        return a, b, c

    def evaluate(self, keyword: str, *, seed: str = "") -> Evaluation:
        kw = (keyword or "").strip()
        if not kw:
            return Evaluation()
        a, b, c = self._seeded_floats(kw)
        token_len = len(kw.replace(" ", ""))
        length_factor = max(0.3, 1.0 - (token_len / 30.0))
        search_volume = int(200 + a * 49_800 * length_factor)

        if token_len <= 4:
            comp_idx = 2 if b > 0.4 else 1
        elif token_len <= 8:
            comp_idx = 1 if b > 0.5 else 0
        else:
            comp_idx = 0 if b > 0.3 else 1
        competition = COMPETITIONS[comp_idx]

        cpc = round(200 + c * 1800, -1)

        commercial_intent = sum(1 for w in COMMERCIAL_KEYWORDS if w in kw.lower())
        commercial_intent = min(1.0, 0.2 + commercial_intent * 0.25)

        if 1000 <= search_volume <= 30_000:
            sv_score = 30.0
        elif search_volume < 1000:
            sv_score = max(0.0, 30.0 * (search_volume / 1000.0))
        else:
            over = (search_volume - 30_000) / 20_000.0
            sv_score = max(10.0, 30.0 - over * 15.0)

        comp_score = {"낮음": 30.0, "중간": 15.0, "높음": 5.0}[competition]
        cpc_score = min(20.0, max(0.0, (cpc - 200) / 1800 * 20.0))
        if cpc >= 500:
            cpc_score = max(cpc_score, 12.0)
        intent_score = commercial_intent * 20.0
        score = round(sv_score + comp_score + cpc_score + intent_score, 2)

        return Evaluation(
            search_volume=search_volume, competition=competition, cpc=cpc,
            commercial_intent=commercial_intent, score=score,
            raw={"evaluator": self.name, "seed": seed},
        )
