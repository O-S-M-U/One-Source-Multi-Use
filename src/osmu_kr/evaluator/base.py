"""Evaluator 인터페이스."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable, List

from ..models import Evaluation


class BaseEvaluator(ABC):
    name: str = "base"

    @abstractmethod
    def evaluate(self, keyword: str, *, seed: str = "") -> Evaluation: ...

    def evaluate_longtail(self, keyword: str, *, seed: str = "") -> Evaluation:
        """롱테일 변형 모드. 기본 구현은 evaluate() 와 동일."""
        return self.evaluate(keyword, seed=seed)

    def evaluate_many(self, keywords: Iterable[str], *, seed: str = "") -> List[Evaluation]:
        return [self.evaluate(k, seed=seed) for k in keywords]
