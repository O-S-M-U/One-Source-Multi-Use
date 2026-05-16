# O.S.M.U — One-Source-Multi-Use

한국어 수익형 블로그 운영을 자동화하는 5단 파이프라인 + 안전장치 계층. 키워드 발굴 → 청사진 + 사실(facts) 수집 → HTML 생성 → 자기잠식·표절 검사 → 발행까지를 한 줄기로 묶고, **자기잠식·어뷰징·deadlock 을 사전에 차단**한다. 단독·팀 운영 모두 지원하며, API 키 0개 환경에서도 룰 폴백으로 끝까지 돌아간다.

> 본 저장소는 **v13 spec** 기반. 현재 위치는 “contents_maker 단순화 완료. checker / publisher 진입 직전”.

---

## 🚀 빠른 시작

```bash
# 1) 가상환경 + 의존성
python3 -m venv .venv && source .venv/bin/activate     # Mac/Linux
# Windows: python -m venv .venv && .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r ui/requirements.txt
pip install -e .

# 2) 테스트 (67/67 통과 확인)
OSMU_EMBEDDER=stub PYTHONPATH=src python tests/test_basic.py

# 3) GUI 실행
python main.py
```

Mac은 `ui/run_mac.command`, Windows는 `ui/run_windows.bat` 더블클릭으로도 실행 가능.

---

## 🧭 v13 파이프라인 — 진행 상황

| 단계 | 모듈 | 산출물 | 상태 |
|---|---|---|---|
| 0 | `interpreter.interpret` | KeywordContext (domain · intent · topic_summary) | ✅ |
| 1 | `keyword_researcher` (+ housekeeping 내재화) | 황금 키워드 + 풀 + revival 재평가 + 풀삭제 2단계 | ✅ |
| 1-안 | `KeywordSafety` | 씨드 중복(0.93) + 어뷰징 쿨다운(0.85, 3일) | ✅ |
| 1-안 | `SafetyLayer` | keyword_usages 기반 lock + archive | ✅ |
| 2-1 | `Collector.phase1` | title · target_reader · paragraph_blueprint | ✅ |
| 2-1.5 | `embedder` (jhgan/ko-sroberta-multitask) | summary_embedding · keyword embedding | ✅ |
| 2-1.6 | `blueprint.commercial_elements` | 추천 · 비교 · CTA 후보 | ✅ |
| 2-2 | `phase2.Phase2Collector` | normalized_sources (단락별 facts) | ✅ |
| 2-안 | collector Phase 1 자기잠식 경고 | 0.75 이상 매치 시 경고 + raw_signals | ✅ |
| 3 | `contents_maker` (write_from_blueprint) | refined_post (HTML) — facts 만 인용, raw 비노출 | ✅ |
| 3-DB | SQLite + PostgreSQL/pgvector 영속화 | 5 테이블 + config + accounts | ✅ |
| 4 | `checker` Stage 1 | 결정적 검증 (글자수, 구조, alt, 키워드 등장, 링크 HEAD) | ✅ |
| 4-B | `checker` plagiarism | normalized_sources vs 본문 cosine (overall + max sentence) | ✅ |
| 4-안 | `checker` Stage 2 | Google CSE + 사람 승인 UI | 🔜 |
| 5 | `Publisher` | 발행 게이트 4종(daily_limit / min_draft / similarity_cooldown) + MockPublisher / TistoryPlaywrightPublisher | ✅ (Mock 완료, Playwright 인터페이스만) |

### 안전장치 계층 (v13)

| 안전장치 | 시점 | 효과 |
|---|---|---|
| `keyword_usages.in_progress` lock | select 시 | 같은 키워드 중복 진입 차단 (deadlock 방지: timeout 또는 mark_failed 로 자동 해제) |
| `KeywordSafety.find_seed_duplicates` | 씨드 입력 | 유사도 ≥ 0.93 인 기존 키워드 안내 |
| `KeywordSafety.check_abuse_cooldown` | 황금 선정 시 | keyword vs keyword embedding 0.85 이상 → 3일 cooldown |
| 180일 재사용 정책 | 같은 blog 발행 시 | `keyword_usages.published_at` 180일 미만이면 재사용 차단 |
| `Collector.phase1` 자기잠식 경고 | Phase 1 종료 후 | summary_embedding ≥ 0.75 매치 시 raw_signals 에 기록 |
| `archive` 영구 제외 | housekeeping | 저품질 재평가 → archived → 추천 영구 제외 |
| 풀 삭제 2단계 | pool_max_size 초과 | 1순위(eval≥3, avg<45) → 2순위(last_eval ASC, total_score ASC) |

---

## 🧱 핵심 데이터 흐름

```
사용자 입력 keyword (str)
    │
    ▼
[0] interpret(keyword)             ← 룰 + Anthropic Claude 보강 (옵션)
    │  └ KeywordContext { keyword, inferred_topic, intent_hint, domain, topic_summary, source }
    ▼
[1] keyword_researcher              ← golden keyword 평가 + pool 관리
    │
    ▼
[2-1] Collector.phase1(ctx)        ← 청사진 생성 + summary_embedding + commercial
    │  ├ generate_blueprint()                  (룰 또는 LLM)
    │  ├ validate_blueprint()                  (일반 템플릿 reject + commercial 검증)
    │  ├ embedder.encode(...)                  (jhgan/ko-sroberta-multitask)
    │  └ → BlueprintResult {
    │        title, target_reader, paragraphs[],
    │        intro, short_conclusion,
    │        summary_embedding, commercial_elements, source
    │      }
    ▼
[2-2] Phase2Collector(crawler).run(blueprint, domain=...)
    │  ├ fact_based 단락만 순회 → 단락별 검색·크롤링·dedup
    │  ├ 도메인 관련성 게이트 (game 글에 일반 비즈니스 facts 들어오면 reject)
    │  └ → Phase2Result {
    │        sources_by_section: { section_index → [FactItem] },
    │        total_facts, issues, meta
    │      }
    ▼
[3] contents_maker (기존 Writer)    ← refined_post (HTML)
    │
    ▼
content_db (xlsx / csv / sheets / mirror)
```

각 단계는 **str과 KeywordContext 둘 다 입력으로 받을 수 있어** 후방호환을 깨지 않는다.

---

## 🧠 0단계 — KeywordContext + interpret

키워드를 단순 str로 흘려보내지 않고 즉시 **{keyword + inferred_topic + intent_hint + topic_summary}** 묶음으로 정규화한다. 분류 사각지대(미등재 신생 키워드)는 Anthropic Claude로 보강해 “‘데드바이데이라이트 = 게임’이라는 정보가 다음 단계까지 살아있게” 만든다.

| 모드 | 트리거 | 동작 |
|---|---|---|
| 룰만 | 기본값 | `keyword_classifier` 8개 도메인 + 9개 intent 사전 매칭 |
| LLM 보강 | `OSMU_USE_LLM_INTERPRETER=1` 또는 `interpret(use_llm=True)` | Claude Haiku로 `domain / intent / topic_summary` 덮어쓰기 |
| 강제 차단 | `OSMU_DISABLE_LLM_INTERPRETER=1` | LLM 호출 금지 |

LLM 호출 실패 또는 키 없음 → 자동 룰 폴백, `source='llm_fallback_rule'` 로 기록.

---

## 🏗 2-1단계 — Phase 1 청사진

`Collector.phase1(ctx)` 가 v9 spec의 contents 레코드 4종을 한 번에 만든다.

```
title                — h1, Tistory 본문과 별도 입력
target_reader        — { persona, knowledge_level: 초보|중급|전문가, primary_intent }
paragraph_blueprint  — 단락별 { section_index, title, paragraph_type, description, facts_required }
                       · paragraph_type: fact_based | llm_generated
                       · 첫·마지막 단락은 항상 llm_generated
summary_embedding    — embed(title + intro + short_conclusion), 768-dim
commercial_elements  — { recommendations, comparison_points, cta_candidates }
```

**일반 템플릿 자동 reject** — `[개념 → 활용 → 결론]` 류 추상 H2만으로 구성된 청사진은 `blueprint_validator` 가 reject하고 룰 폴백. 키워드/의도 신호가 단락 어디에도 없어도 reject.

**Commercial auto-fix** — LLM이 commercial을 누락해도 단락 구조가 정상이면 단락은 LLM 결과를 유지하고 commercial만 도메인별 룰로 보강(`raw_signals.commercial_autofix` 기록).

**임베딩 운영**
- 기본 모델: `jhgan/ko-sroberta-multitask` (768-dim, 한국어 sentence embedding)
- 첫 호출 시 자동 다운로드(~400MB), 캐시는 `~/.cache/osmu_kr/embed`
- 모델 로드 실패 시 자동 `StubEmbedder` 폴백 — 앱은 멈추지 않음
- 테스트는 `OSMU_EMBEDDER=stub` 으로 강제 (다운로드 회피)

---

## 🔍 2-2단계 — Phase 2 fact 매핑

`Phase2Collector(crawler).run(blueprint, domain=...)` 는 fact_based 단락만 골라서 단락별로 facts_required 키워드별 검색·크롤링·dedup을 수행한다. 결과는 v9 spec의 `contents.normalized_sources` 와 동일한 구조.

**자동 게이트**
- `min_facts_per_section` (default 3) 미달 → `insufficient_facts:section=N`
- `min_total_facts` (default 6) 미달 → `total_facts_too_low`
- 도메인 관련성 비율 < 0.2 → `domain_mismatch:ratio=...`
  (게임 키워드인데 facts에 ‘게임/플레이/캐릭터’ 같은 도메인 마커가 거의 없으면 자동 차단)

---

## 🌱 1단계 — Keyword Researcher

`run_seed → expand → evaluate → alchemy → pool` 까지의 키워드 선정 흐름. **‘어떤 글을 쓸지’는 결정하지 않는다** — 좋은 키워드만 찾는 책임에 집중.

### 4축 점수 (`naver_golden` 평가기)

| 축 | DEFAULT | LONGTAIL | 데이터 소스 |
|---|---:|---:|---|
| 검색 트렌드 | 40 | 20 | 네이버 DataLab |
| 경쟁도 | 30 | 45 | 네이버 블로그 검색 결과 수 |
| 상업적 의도 | 20 | 25 | 키워드 텍스트 매칭 |
| 보조 트렌드 | 10 | 10 | Google Trends (pytrends) |

**등급** — 황금 80+ / 좋은 60+ / 보통 40~59 (알케미 대상) / 미달 <40 (제외)

### 처방형 키워드 알케미

약점 진단(`상업의도_부족 / 경쟁도_높음 / 트렌드_낮음`)에 맞춰 6개 카테고리에서 수식어를 처방.

```
"다이어트" + 경쟁도_높음   → "직장인 다이어트", "다이어트 처음 시작하는", "가성비 다이어트"
"노트북"   + 상업의도_부족 → "노트북 추천", "노트북 비교", "노트북 후기"
```

---

## 💾 저장 모드 6종

| 모드 | 동작 | 권장 상황 |
|---|---|---|
| `local` | 내 컴퓨터(.xlsx 또는 .csv) 단독 | 혼자 사용, 오프라인 |
| `sheets` | Google Sheets 단독 (실시간 호출) | 팀 협업 우선 |
| `mirror` | 로컬 + Sheets 양방향 동기화 | 단독·팀 둘 다 |
| `sqlite` | 단일 .db 파일 — v9 spec 5개 테이블 | 개발·단독 운영 |
| `postgres` 🆕 | PostgreSQL + pgvector — Neon 등 | **운영 권장** — pgvector 의미 검색 |
| `auto` | 자격증명 있으면 mirror, 없으면 local | 별 설정 없이 |

5개 테이블(`keywords / keyword_evaluations / keyword_usages / accounts / contents`)이 첫 호출 시 자동 생성된다. SQLite·PostgreSQL 모두 같은 `BaseStorage` 인터페이스 — 코드 변경 없이 백엔드 전환 가능.

### Neon (PostgreSQL + pgvector) 셋업 5분 가이드

1. [console.neon.tech](https://console.neon.tech) 가입 → New Project (region 은 가까운 곳).
2. 대시보드의 **Connection Details** 에서 “Connection string (psql / pooled)” 복사.
3. `.env` 에 붙여넣기:
   ```env
   OSMU_STORAGE_BACKEND=postgres
   OSMU_DATABASE_URL=postgresql://USER:PASS@ep-xxx-xxxxx-pooler.region.aws.neon.tech/dbname?sslmode=require
   ```
4. 의존성 설치 — `pip install 'psycopg[binary]>=3.1' pgvector`.
5. 처음 앱 실행 시 `CREATE EXTENSION vector` + DDL 자동 적용. Neon 은 pgvector 0.5+ 기본 제공이라 별도 활성 작업 없음.
6. 자기잠식 의미 검색은 `PostgresStorage.find_similar_contents(embedding, top_k=5)` 한 줄.

> pgvector 비활성 환경(자체호스팅 등)이면 `summary_embedding` 이 자동으로 text(JSON) 폴백으로 떨어진다. 자기잠식 ANN 만 일시 비활성. 다른 모든 기능은 동일.

---

## ⚙️ 환경변수

`.env.example` 을 `.env` 로 복사 후 필요한 값만 채운다.

### 저장 / 평가 (1단계)

| 변수 | 기본값 | 의미 |
|---|---|---|
| `OSMU_STORAGE_BACKEND` | `auto` | `auto` / `mirror` / `sheets` / `local` / `sqlite` / `postgres` |
| `OSMU_LOCAL_FORMAT` | `xlsx` | `xlsx` / `csv` |
| `OSMU_SQLITE_PATH` | `./osmu.db` | SQLite 파일 경로 (`backend=sqlite` 일 때) |
| `OSMU_DATABASE_URL` | — | PostgreSQL 연결 문자열 (`backend=postgres` 일 때, Neon 등) |
| `OSMU_DB_POOL_MIN` | `1` | psycopg 풀 최소 커넥션 |
| `OSMU_DB_POOL_MAX` | `4` | psycopg 풀 최대 커넥션 |
| `OSMU_EVALUATOR` | `heuristic` | `heuristic` / `naver_golden` / `naver_ads` |
| `OSMU_POOL_MAX_SIZE` | `200` | 풀 최대 크기 |
| `OSMU_REVIVAL_DAYS` | `30` | 재평가 주기(일) |
| `OSMU_SEED_COOLDOWN_DAYS` | `7` | 동일 주제 재사용 간격(일) |
| `OSMU_GOLDEN_THRESHOLD` | `70` | 황금 임계 점수 |

### 0~4단계 (content_generator)

| 변수 | 기본값 | 의미 |
|---|---|---|
| `OSMU_USE_LLM_INTERPRETER` | `0` | `1` 이면 0단계에서 Claude로 domain/intent/topic_summary 보강 |
| `OSMU_DISABLE_LLM_INTERPRETER` | — | `1` 이면 use_llm=True 라도 LLM 호출 차단 |
| `OSMU_USE_LLM_BLUEPRINT` | `0` | `1` 이면 Phase 1 청사진을 Claude로 생성 |
| `OSMU_DISABLE_LLM_BLUEPRINT` | — | `1` 이면 Blueprint LLM 호출 차단 |
| `OSMU_EMBEDDER` | `ko-sroberta` | `ko-sroberta` / `stub` / `disabled` |
| `OSMU_EMBED_CACHE` | `~/.cache/osmu_kr/embed` | 임베딩 모델 캐시 경로 |
| `ANTHROPIC_API_KEY` | — | interpret / blueprint / Writer 공용 |

### 외부 데이터 소스

| 변수 | 기본값 | 의미 |
|---|---|---|
| `NAVER_CLIENT_ID/SECRET` | — | naver_golden 평가기용 (없으면 휴리스틱 폴백) |
| `GOOGLE_APPLICATION_CREDENTIALS` | — | gspread 서비스 계정 키 경로 |
| `OSMU_SHEET_ID` | — | 구글 시트 ID |
| `FIRECRAWL_API_KEY` | — | Firecrawl 크롤링 (없으면 휴리스틱 폴백) |
| `UNSPLASH_ACCESS_KEY` | — | Unsplash 이미지 (없으면 picsum 폴백) |

---

## 📦 프로젝트 구조

```
osmu_keyword_researcher/
├── main.py                          ← Streamlit UI 런처
├── requirements.txt / pyproject.toml / .env.example / .gitignore
├── golden_keyword.py                ← 사용자 제공 황금 키워드 분석기 (참고용)
│
├── src/osmu_kr/                     ← 본체
│   ├── config.py / models.py / cli.py
│   │
│   ├── storage/                     키워드 풀·콘텐츠 영속화
│   │   ├── csv_local.py / xlsx_local.py / sheets.py / mirror.py
│   │   └── factory.py
│   │
│   ├── evaluator/                   1단계 — 키워드 평가기
│   │   ├── heuristic.py / naver_ads.py / naver_golden.py
│   │   └── factory.py
│   │
│   ├── researcher/                  1단계 — 키워드 연구
│   │   ├── researcher.py / expander.py / alchemist.py
│   │   ├── manager.py / recommender.py
│   │
│   └── content_generator/           0~4단계 — 콘텐츠 생성 파이프라인
│       ├── keyword_classifier.py        도메인 8종 + intent 사전
│       ├── keyword_context.py           1단계: KeywordContext 데이터 구조
│       ├── interpreter.py               0단계: interpret() — 룰 + Claude 보강
│       │
│       ├── blueprint.py                 2-1단계: BlueprintResult 생성
│       ├── blueprint_validator.py       2-1단계: 일반 템플릿 reject
│       ├── embedder.py                  2-1.5: jhgan/ko-sroberta-multitask
│       ├── phase2.py                    2-2단계: normalized_sources
│       │
│       ├── collector.py                 (raw_content + Phase 1 통합)
│       ├── firecrawl_client.py          Firecrawl REST + MCP
│       ├── images.py / unsplash_client.py / keyword_translator.py
│       ├── writer.py                    Anthropic + Heuristic Writer
│       └── generator.py                 전체 오케스트레이션
│
├── ui/                              Streamlit GUI
│   ├── app.py / pages/ (1~6) / services/
│   ├── run_mac.command / run_windows.bat
│   └── requirements.txt
│
├── tests/test_basic.py              ← 67개 회귀 테스트
├── .devcontainer/                   ← Codespace prebuild
└── .github/workflows/tests.yml      ← Python 3.10/3.11/3.12 + macOS 매트릭스
```

---

## 📟 CLI

```bash
# 1단계 — 키워드 연구
osmu-kr seed --seed "다이어트"
osmu-kr check --keyword "AI ETF 추천 2025"
osmu-kr recommend --top 5
osmu-kr select --id 0001 --title "직장인 다이어트 식단"
osmu-kr prune
osmu-kr manage
osmu-kr show
osmu-kr history

# 3단계 — 콘텐츠 생성 (기존 Writer, 재설계 예정)
osmu-kr generate --keyword "데드바이데이라이트 공략"
osmu-kr regenerate --id 042
osmu-kr delete-content --id 042
```

---

## ✅ 테스트 / CI

```bash
OSMU_EMBEDDER=stub PYTHONPATH=src python tests/test_basic.py
# result: 118/118 passed
```

GitHub Actions(`tests.yml`)는 push/PR마다 Python 3.10/3.11/3.12 + macOS-latest 매트릭스로 자동 실행. 테스트 환경에서는 `OSMU_EMBEDDER=stub` 강제로 ko-sroberta 다운로드를 회피.

---

## 🌐 GitHub Codespaces

저장소 페이지 → 초록색 `<> Code` → Codespaces → `Create codespace on main`. `devcontainer.json` 이 자동으로 의존성 설치, `.env` 자동 복사, 8501 포트 미리보기까지 처리한다. v2부터 sentence-transformers 모델 캐시 prebuild를 추가할 예정.

---

## 🛡 안전장치

| 시점 | 장치 | 효과 |
|---|---|---|
| 0단계 | `OSMU_DISABLE_LLM_INTERPRETER=1` | 토큰 소비 차단 (디버깅·CI) |
| 0단계 | LLM 호출 실패 자동 룰 폴백 | 외부 API 다운에도 파이프라인 멈추지 않음 |
| 2-1 | `blueprint_validator` 일반 템플릿 reject | 추상 단락(`개념·활용·결론`)만으로 구성된 글 차단 |
| 2-1 | commercial auto-fix | LLM이 추천·비교·CTA 누락해도 룰로 보강 |
| 2-1.5 | embedder 자동 stub 폴백 | sentence-transformers 환경 문제로 앱 멈춤 방지 |
| 2-2 | 도메인 관련성 게이트 | 게임 글에 일반 비즈니스 facts 들어오면 자동 차단 |
| 2-2 | 단락당/전체 최소 facts 강제 | 사실 부족한 ‘속 빈 글’ 차단 |
| 3 | Writer 폴백 + HTML 검증 | LLM 실패 시 휴리스틱 + 금지 표현 자동 제거 |

---

## 📖 사용자 매뉴얼 (PDF)

전체 설치/Git/Codespace/투 트랙 운영 흐름을 담은 한국어 PDF — `OSMU_사용자_매뉴얼.pdf` 별도 산출물로 제공.

---

## 🛣 로드맵

- [x] 1단계 — Keyword Researcher (4축 점수 + 알케미 + 풀 관리)
- [x] 0단계 — KeywordContext + interpret() (룰 + Claude 보강)
- [x] 2-1단계 — Phase 1 청사진 + summary_embedding + commercial_elements
- [x] 2-2단계 — Phase 2 fact 매핑 + 도메인 관련성 + 최소 facts 게이트
- [x] 5단계 — SQLite 영속화 (v13 spec 6 테이블 + config + accounts + JSON 컬럼)
- [x] 6단계 — PostgreSQL + pgvector (Neon) — keyword/content embedding ANN
- [x] 7단계 — v13 안전장치 (keyword_usages lock, KeywordSafety, SafetyLayer)
- [x] v13-D — config 테이블 + ConfigManager (env > db > default)
- [x] v13-E — housekeeping 내재화 + 풀삭제 2단계 정책
- [x] contents_maker 단순화 — `write_from_blueprint()` 가 facts 만 인용, raw 비노출
- [x] CLI 신규 — `config get/set/list/install-defaults`, `housekeeping`, `account add/list`
- [x] checker Stage 1 — Tier A 결정적 검증 + Tier B 임베딩 표절 검사
- [x] checker Stage 1 보완 — Google CSE 광역 표절 (OSMU_GOOGLE_CSE_KEY/CX)
- [x] checker Stage 2 진입점 — `submit_for_review()` + Slack 알림 (notifications.py)
- [x] publisher — 발행 게이트 4종 + MockPublisher
- [x] publisher — Tistory Playwright 실 자동화 흐름 (OSMU_PUBLISH_REAL=1 가드)
- [x] Tistory 쿠키 캡처 스크립트 — `scripts/capture_tistory_cookie.py`
- [x] Tistory DOM 셀렉터 config 분리 — `tistory.selector.title/body/publish`
- [x] CLI — `check-content`, `approve`, `reject`, `publish` 명령
- [x] Streamlit 검토 UI — `ui/pages/7_검토_승인.py`
- [x] keyword_usages timeout 자동 처리 — housekeeping 안에서 24h 초과 자동 failed
- [x] 점수 정확도 보완 (score-1 ~ score-7)
  - blog 경쟁도 sub-axis 분리 (총량 15 + 최근 14일 밀도 15)
  - DataLab slope 정량화 (% 기반)
  - Naver Search Ad API `monthlyPcQcCnt` 절대 월 검색량 (정확도 ⭐⭐⭐)
  - Naver Search Ad API `keywordstool` 연관 키워드 (자동완성 대체/보완)
  - Shopping API + 패턴 룰 하이브리드 (상업적 의도 정확도 ⭐⭐)
  - 다양성 회피 (v13 spec d): 최근 7일 5편 중 cosine ≥ 0.8 그룹 3편 이상 경고
  - publish_attempt_count 재시도 + 지수 백오프
- [x] CI/CD — GitHub Actions 에 실 Neon 통합 테스트 job 추가
- [x] Codespaces prebuild — sentence-transformers 모델 캐시 (다운로드 회피)
- [x] 비용/한도 모니터링 — `monitoring.py` 호출 카운터 + Slack 임계 알림
- [x] DB 백업 — `scripts/backup_db.py` (SQLite .backup API / pg_dump)
- [x] Anthropic 모델 선택 config 화 — `anthropic.model.{interpret,blueprint,writer}`
- [x] **문서화** — 외부 서비스 셋업 v2 PDF + 운영자 매뉴얼 v2 PDF + 개발자 가이드 MD
- [ ] **운영 진입** — 실 Tistory DOM 셀렉터 확정 (쿠키 캡처 후 첫 발행하면서)
- [ ] checker — 자기잠식·표절·구조 두 단계 게이트
- [ ] publisher — 티스토리 Playwright + 어뷰징 게이트
- [ ] PostgreSQL + pgvector — 임베딩 기반 자기잠식 검색을 DB 안에서

---

## 🔮 설계 원칙

1. **모든 단계는 룰 폴백을 갖는다.** API 키 0개로도 끝까지 돌아가야 한다.
2. **각 단계는 in-memory 산출물을 명시적 데이터 클래스로 반환한다.** 다음 단계가 dict 헷갈림 없이 받는다.
3. **str과 정규화된 컨텍스트 둘 다 받는다.** 후방호환을 절대 깨지 않는다.
4. **검증은 ‘구조 reject’와 ‘soft auto-fix’로 분리한다.** 수익 포인트(commercial) 누락은 자동 보강, 단락 구조 위반은 룰 폴백.
5. **단계 사이의 책임은 침범하지 않는다.** keyword_researcher는 좋은 키워드만, collector는 청사진·facts만, contents_maker는 HTML 변환만.
