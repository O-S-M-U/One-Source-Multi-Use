#!/usr/bin/env bash
set -e
echo "▶ [on-create] 의존성 설치"
python -m pip install --upgrade pip wheel setuptools
pip install -r requirements.txt
pip install -r ui/requirements.txt
pip install -e .
mkdir -p data credentials
echo "✅ [on-create] 완료"
