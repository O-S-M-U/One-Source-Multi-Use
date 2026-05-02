# One-Source-Multi-Use

We’re going to create documentation for multiple platforms from a single source.

---

## 🌱 O.S.M.U Keyword Researcher

수익형 블로그 콘텐츠 자동화의 첫 단계 — **씨앗 키워드 입력 → 황금 키워드 자동 발굴 → 풀 관리 → 추천 → 콘텐츠 작성 대기 등록** 까지 책임지는 모듈입니다.

CLI(`osmu-kr seed`, `recommend`, `select`, `prune`)와 비개발자용 Streamlit GUI 두 가지 인터페이스를 제공하며, **로컬 엑셀(.xlsx) ↔ Google Sheets 양방향 동기화**로 단독·팀 운영 둘 다 지원합니다.

---

## 🚀 빠른 시작 (3 단계)

```bash
# 1) 가상환경
python3 -m venv .venv && source .venv/bin/activate     # Mac/Linux
# Windows: python -m venv .venv && .\.venv\Scripts\Activate.ps1

# 2) 의존성 설치
pip install -r requirements.txt -r ui/requirements.txt
pip install -e .

# 3) GUI 실행 — 브라우저가 자동으로 열립니다
python main.py
```

또는 Mac에서 `ui/run_mac.command`, Windows에서 `ui/run_windows.bat` 더블클릭.

---

## 📦 프로젝트 구조

```
osmu_keyword_researcher/
├── main.py                          ← Streamlit UI 런처 (python main.py)
├── requirements.txt / pyproject.toml / .env.example / .gitignore
├── golden_keyword.py                ← 사용자 제공 황금 키워드 분석기 (참고용)
│
├── src/osmu_kr/                     ← 본체 (수정 금지)
│   ├── config.py / models.py / cli.py
│   ├── storage/   (csv / xlsx / sheets / mirror — BaseStorage 인터페이스)
│   ├── evaluator/ (heuristic / naver_ads / naver_golden — BaseEvaluator)
│   └── researcher/ (researcher / expander / alchemist / manager / recommender)
│
├── ui/                              ← Streamlit GUI (본체 wrapper)
│   ├── app.py                           홈 대시보드
│   ├── pages/  1️⃣ 생성 / 2️⃣ 풀 / 3️⃣ 추천·선택 / 4️⃣ 설정 / 5️⃣ 로그
│   ├── services/  싱글턴 + 에러 번역 + 활동 로그
│   ├── run_mac.command / run_windows.bat
│   └── requirements.txt
│
├── tests/test_basic.py              ← 11개 회귀 테스트
├── .devcontainer/                   ← Codespace prebuild + .env 자동 복사
└── .github/workflows/tests.yml      ← Python 3.10/3.11/3.12 + macOS 매트릭스
```

---

## 🎯 핵심 개념

### 4축 점수 시스템 (`naver_golden` 평가기)

| 축 | DEFAULT 가중치 | LONGTAIL 가중치 | 데이터 소스 |
|---|---:|---:|---|
| 검색 트렌드 | 40 | 20 | 네이버 DataLab |
| 경쟁도 | 30 | 45 | 네이버 블로그 검색 결과 수 |
| 상업적 의도 | 20 | 25 | 키워드 텍스트 매칭 |
| 보조 트렌드 | 10 | 10 | Google Trends (pytrends) |

**롱테일 변형(알케미)** 은 가중치를 자동 전환해 평가합니다 — 트렌드는 낮아도 경쟁이 없으면 가치 있다는 가정.

### 등급
**황금 80+** / **좋은 60+** / **보통 40~59 (알케미 대상)** / **미달 <40 (제외)**

### 처방형 키워드 연금술

약점 진단(`상업의도_부족 / 경쟁도_높음 / 트렌드_낮음`) 결과에 맞춰 6가지 카테고리(`상업의도 / 대상 / 상황 / 가격 / 목적 / 기간`)에서 수식어를 처방.

```
"다이어트" + 경쟁도_높음   → "직장인 다이어트", "다이어트 처음 시작하는", "가성비 다이어트"
"노트북"   + 상업의도_부족 → "노트북 추천", "노트북 비교", "노트북 후기"
```

---

## 💾 저장 모드 4종

| 모드 | 동작 | 권장 상황 |
|---|---|---|
| `local` | 내 컴퓨터(.xlsx 또는 .csv) 단독 | 혼자 사용, 오프라인 |
| `sheets` | Google Sheets 단독 (실시간 호출) | 팀 협업 우선 |
| `mirror` ⭐ | 로컬 + Sheets 양방향 동기화 | **권장** — 둘 다 활용 |
| `auto` | 자격증명 있으면 mirror, 없으면 local | 별 설정 없이 |

`설정` 화면에서 라디오 한 번이면 전환됩니다.

---

## 🛠 환경변수

`.env.example` 을 `.env` 로 복사 후 필요한 값만 채우세요.

| 변수 | 기본값 | 의미 |
|---|---|---|
| `OSMU_STORAGE_BACKEND` | `auto` | `auto` / `mirror` / `sheets` / `local` |
| `OSMU_LOCAL_FORMAT` | `xlsx` | `xlsx` / `csv` |
| `OSMU_EVALUATOR` | `heuristic` | `heuristic` / `naver_golden` / `naver_ads` |
| `OSMU_POOL_MAX_SIZE` | `200` | 풀 최대 크기 |
| `OSMU_REVIVAL_DAYS` | `30` | 재평가 주기(일) |
| `OSMU_SEED_COOLDOWN_DAYS` | `7` | 동일 주제 재사용 간격(일) |
| `OSMU_GOLDEN_THRESHOLD` | `70` | 황금 임계 점수 |
| `NAVER_CLIENT_ID/SECRET` | — | naver_golden 평가기용 (없으면 휴리스틱 폴백) |
| `GOOGLE_APPLICATION_CREDENTIALS` | — | gspread 서비스 계정 키 경로 |
| `OSMU_SHEET_ID` | — | 구글 시트 ID |

---

## 📟 CLI

```bash
osmu-kr seed --seed "다이어트"
osmu-kr check --keyword "AI ETF 추천 2025"
osmu-kr recommend --top 5
osmu-kr select --id 0001 --title "직장인 다이어트 식단"
osmu-kr prune
osmu-kr show
```

---

## ✅ 테스트 / CI

```bash
PYTHONPATH=src python tests/test_basic.py     # 11개 테스트 로컬 실행
```

GitHub Actions(`tests.yml`)는 push/PR 시 Python 3.10/3.11/3.12 + macOS-latest 매트릭스로 자동 실행됩니다.

---

## 🌐 GitHub Codespaces

저장소 페이지 → 초록색 `<> Code` → Codespaces → `Create codespace on main`. `devcontainer.json` 이 자동으로 의존성 설치(prebuild 친화), `.env` 자동 복사, 8501 포트 미리보기까지 처리합니다.

---

## 📖 사용자 매뉴얼 (PDF)

전체 설치/Git/Codespace/투 트랙 운영 흐름을 담은 한국어 PDF 매뉴얼이 별도 산출물로 제공됩니다 — `OSMU_사용자_매뉴얼.pdf`.

---

## 🔮 확장 방향

키워드 단계 → **글감 수집(OpenClaw)** → **이미지(Unsplash)** → **글 작성(Claude)** → **PM 검토(Slack)** → **자동 발행(티스토리)** 의 다음 모듈을 같은 `BaseStorage` 위에 이어 붙일 수 있도록 설계됐습니다.
