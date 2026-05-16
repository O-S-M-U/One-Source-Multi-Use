#!/usr/bin/env bash
set -e
echo "▶ [on-create] 의존성 설치"
python -m pip install --upgrade pip wheel setuptools
pip install -r requirements.txt
pip install -r ui/requirements.txt
pip install -e .
mkdir -p data credentials cookies

# infra-2: ko-sroberta-multitask 모델 prebuild — 매번 ~400MB 다운로드 회피
echo "▶ [on-create] sentence-transformers 모델 캐시 적재 (jhgan/ko-sroberta-multitask)"
python - <<'PY' || echo "⚠️  embedder prebuild 실패(무시) — 첫 실행 시 자동 다운로드됨"
import os, sys
try:
    from sentence_transformers import SentenceTransformer
    cache = os.environ.get("OSMU_EMBED_CACHE",
                            os.path.expanduser("~/.cache/osmu_kr/embed"))
    os.makedirs(cache, exist_ok=True)
    m = SentenceTransformer("jhgan/ko-sroberta-multitask", cache_folder=cache)
    # 한 번 encode 호출로 lazy 초기화 완전 종료
    m.encode(["워밍업"], normalize_embeddings=True)
    print(f"✅ 모델 캐시: {cache}")
except Exception as e:
    print(f"⚠️  prebuild skip: {e}", file=sys.stderr)
    sys.exit(0)
PY

echo "✅ [on-create] 완료"
