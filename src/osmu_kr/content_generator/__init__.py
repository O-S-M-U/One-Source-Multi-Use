"""콘텐츠 생성 모듈 (신규).

Golden Keyword → Firecrawl 검색·크롤링 → raw_content → LLM SEO HTML → content_db 저장

기존 KeywordResearcher / evaluator / DB 스키마 / CLI 구조는 일절 수정하지 않는다 —
신규 모듈로 ‘덧붙이는’ 형태로만 동작한다.
"""
from .interfaces import (
    BaseCrawler, BaseWriter, BaseImageProvider,
    CrawledPage, GenerationResult,
)
from .firecrawl_client import FirecrawlClient
from .collector import Collector, RawContent
from .keyword_context import KeywordContext, infer_intent, rule_topic_summary
from .interpreter import interpret
from .blueprint import (
    BlueprintResult, CommercialElements, ParagraphBlock, TargetReader,
    generate_blueprint,
)
from .blueprint_validator import validate_blueprint
from .embedder import (
    BaseEmbedder, KoSrobertaEmbedder, StubEmbedder, ZeroEmbedder,
    build_embedder, cosine,
)
from .phase2 import FactItem, Phase2Collector, Phase2Config, Phase2Result
from .writer import AnthropicWriter, HeuristicWriter
from .images import UnsplashImageProvider, PicsumImageProvider
from .generator import Generator

__all__ = [
    "BaseCrawler", "BaseWriter", "BaseImageProvider",
    "CrawledPage", "GenerationResult",
    "FirecrawlClient", "Collector", "RawContent",
    "KeywordContext", "infer_intent", "rule_topic_summary", "interpret",
    "BlueprintResult", "CommercialElements", "ParagraphBlock", "TargetReader",
    "generate_blueprint", "validate_blueprint",
    "BaseEmbedder", "KoSrobertaEmbedder", "StubEmbedder", "ZeroEmbedder",
    "build_embedder", "cosine",
    "FactItem", "Phase2Collector", "Phase2Config", "Phase2Result",
    "AnthropicWriter", "HeuristicWriter",
    "UnsplashImageProvider", "PicsumImageProvider",
    "Generator",
]
