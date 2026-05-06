"""embedder — summary_embedding 생성기 (3단계).

[ v9 spec ]
  · summary_embedding = embed(title + intro + short_conclusion)
  · 모델: jhgan/ko-sroberta-multitask  (한국어 sentence embedding)
  · v1: 로컬 실행 (CPU 가능, 비용 0)
  · v2+: pgvector 확장으로 DB 내 벡터 검색

[ 구현 ]
  · BaseEmbedder        : 추상 (encode(text) -> List[float])
  · KoSrobertaEmbedder  : sentence-transformers 로 jhgan/ko-sroberta-multitask 로드.
                          첫 호출 시 ~400MB 모델 다운로드 — lazy.
  · StubEmbedder        : 결정적 해시 기반 — 테스트/오프라인 환경용. 차원 768 고정.
  · ZeroEmbedder        : None 반환. 모델 로드 실패 시 폴백.

[ 폴백 정책 ]
  · 환경변수 OSMU_EMBEDDER=stub   → StubEmbedder
  · OSMU_EMBEDDER=disabled        → ZeroEmbedder
  · OSMU_EMBEDDER=ko-sroberta(default) → KoSrobertaEmbedder.
    → import 실패 / 모델 다운로드 실패 시 자동으로 StubEmbedder 로 폴백
      (앱이 멈추면 안 됨. 자기잠식 체크는 일시 비활성으로만.)
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
from typing import List, Optional, Protocol

log = logging.getLogger(__name__)


_DEFAULT_DIM = 768  # ko-sroberta-multitask 의 출력 차원과 동일


class BaseEmbedder(Protocol):
    name: str
    dim: int

    def encode(self, text: str) -> Optional[List[float]]: ...


# ── 1) 결정적 stub — 테스트용 ──────────────────────────
class StubEmbedder:
    """SHA256 해시 기반 결정적 임베딩. 같은 문자열 → 같은 벡터. cosine 비교 가능.

    실 모델은 아니지만 ‘인터페이스 + 차원 + 결정성’ 만으로 통합 테스트 가능.
    """
    name = "stub"
    dim = _DEFAULT_DIM

    def encode(self, text: str) -> List[float]:
        text = (text or "").strip()
        if not text:
            return [0.0] * self.dim
        # 32바이트 해시를 반복해 dim 만큼 채움 → 표준화
        h = hashlib.sha256(text.encode("utf-8")).digest()
        raw = (h * ((self.dim // 32) + 2))[: self.dim]
        # -1..1 범위 float 으로 변환
        floats = [(b - 128) / 128.0 for b in raw]
        # L2 정규화 (cosine 비교에 유리)
        norm = math.sqrt(sum(x * x for x in floats)) or 1.0
        return [x / norm for x in floats]


# ── 2) 모델 비활성 ─────────────────────────────────────
class ZeroEmbedder:
    """임베딩을 생성하지 않음 (None 반환). 자기잠식 체크 일시 비활성."""
    name = "disabled"
    dim = 0

    def encode(self, text: str) -> Optional[List[float]]:
        return None


# ── 3) 실제 ko-sroberta-multitask ──────────────────────
class KoSrobertaEmbedder:
    """sentence-transformers 로 jhgan/ko-sroberta-multitask 사용.

    - 첫 encode() 호출 때 모델을 lazy load (import + 다운로드).
    - 다운로드/import 실패 시 StubEmbedder 로 자동 폴백.
    """
    name = "ko-sroberta-multitask"
    dim = _DEFAULT_DIM

    def __init__(self, model_id: str = "jhgan/ko-sroberta-multitask",
                 device: Optional[str] = None,
                 cache_folder: Optional[str] = None):
        self.model_id = model_id
        self.device = device
        self.cache_folder = cache_folder or os.getenv(
            "OSMU_EMBED_CACHE", os.path.expanduser("~/.cache/osmu_kr/embed"),
        )
        self._model = None
        self._fallback: Optional[StubEmbedder] = None

    def _ensure_loaded(self):
        if self._model is not None or self._fallback is not None:
            return
        try:
            os.makedirs(self.cache_folder, exist_ok=True)
            from sentence_transformers import SentenceTransformer  # type: ignore
            log.info("[embedder] %s 로드 시도 (cache=%s)", self.model_id, self.cache_folder)
            self._model = SentenceTransformer(
                self.model_id, cache_folder=self.cache_folder, device=self.device,
            )
            log.info("[embedder] %s 로드 성공", self.model_id)
        except Exception as e:
            log.warning(
                "[embedder] %s 로드 실패 → StubEmbedder 로 폴백: %s", self.model_id, e,
            )
            self._fallback = StubEmbedder()

    def encode(self, text: str) -> Optional[List[float]]:
        self._ensure_loaded()
        if self._fallback is not None:
            return self._fallback.encode(text)
        text = (text or "").strip()
        if not text:
            return [0.0] * self.dim
        try:
            vec = self._model.encode([text], normalize_embeddings=True)[0]
            return [float(x) for x in vec]
        except Exception as e:
            log.warning("[embedder] encode 실패 → stub 폴백: %s", e)
            self._fallback = StubEmbedder()
            return self._fallback.encode(text)


# ── 팩토리 ─────────────────────────────────────────────
def build_embedder() -> BaseEmbedder:
    """환경변수에 따라 적절한 embedder 인스턴스 반환."""
    choice = os.getenv("OSMU_EMBEDDER", "ko-sroberta").strip().lower()
    if choice in {"stub", "test", "fake"}:
        return StubEmbedder()
    if choice in {"disabled", "off", "none", "0"}:
        return ZeroEmbedder()
    return KoSrobertaEmbedder()


def cosine(a: List[float], b: List[float]) -> float:
    """L2 정규화된 두 벡터의 cosine. 정규화 안 됐어도 안전하게 계산."""
    if not a or not b or len(a) != len(b):
        return 0.0
    num = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return num / (na * nb)
