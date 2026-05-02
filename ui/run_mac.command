#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
PY="$(command -v python3 || command -v python || true)"
if [[ -z "$PY" ]]; then
  echo "❌ Python을 찾지 못했습니다. https://www.python.org 에서 Python 3.10+ 를 설치해주세요."
  read -n 1 -s
  exit 1
fi
if ! "$PY" -c "import streamlit" >/dev/null 2>&1; then
  echo "▶ 첫 실행 — 필요한 패키지를 설치합니다 (1~2분)…"
  "$PY" -m pip install --upgrade pip
  "$PY" -m pip install -r ui/requirements.txt
fi
"$PY" main.py
