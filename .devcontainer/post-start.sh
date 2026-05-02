#!/usr/bin/env bash
set -e
if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
  echo "📝 [post-start] .env.example을 .env로 복사했어요."
fi
if [ -n "${GOOGLE_APPLICATION_CREDENTIALS_JSON:-}" ]; then
  mkdir -p credentials
  CRED_PATH="credentials/service_account.json"
  printf '%s' "$GOOGLE_APPLICATION_CREDENTIALS_JSON" > "$CRED_PATH"
  chmod 600 "$CRED_PATH"
  if [ -f .env ]; then
    if grep -q "^GOOGLE_APPLICATION_CREDENTIALS=" .env; then
      sed -i.bak "s|^GOOGLE_APPLICATION_CREDENTIALS=.*|GOOGLE_APPLICATION_CREDENTIALS=$CRED_PATH|" .env
      rm -f .env.bak
    else
      echo "GOOGLE_APPLICATION_CREDENTIALS=$CRED_PATH" >> .env
    fi
  fi
  echo "🔐 [post-start] Codespace Secret 으로 자격증명 파일 생성"
fi
echo "✅ [post-start] 'python main.py' 로 GUI를 시작하세요."
