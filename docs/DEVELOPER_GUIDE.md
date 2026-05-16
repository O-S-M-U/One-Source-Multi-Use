# OSMU 개발자 가이드

새 백엔드(evaluator / storage / writer / publisher 등) 를 OSMU 위에 얹는 법.
모든 모듈은 **추상 클래스 1개 → 구현체 N개 + factory 1개** 패턴.

---

## 0. 공통 규칙

- 모든 새 모듈은 `OSMU_EMBEDDER=stub python tests/test_basic.py` 회귀에서 깨지지 않아야 한다 (118/118).
- 외부 API 키가 없을 때 **graceful fallback** — 앱이 절대 멈추지 않는다.
- 환경변수는 dot notation 으로 `OSMU_<카테고리>_<키>` 형식 + `config_manager.DEFAULTS` 에 등록.
- 직렬화는 항상 명시적 dataclass. dict 헷갈림 금지.

---

## 1. 새 Evaluator 추가

키워드 점수를 다른 방식(예: SEMrush, Ahrefs 등) 으로 계산하고 싶을 때.

### 1.1 인터페이스

```python
# src/osmu_kr/evaluator/base.py
class BaseEvaluator(ABC):
    name: str = "base"
    @abstractmethod
    def evaluate(self, keyword: str, *, seed: str = "") -> Evaluation: ...
```

### 1.2 구현 단계

1. `src/osmu_kr/evaluator/my_evaluator.py` 신규 — `BaseEvaluator` 상속.
2. `evaluate()` 가 `Evaluation(score=..., raw={...})` 반환. score 는 0~100.
3. 키 없을 때 `HeuristicEvaluator` 로 fallback (`naver_golden.py` 참조).
4. `factory.py` 에 등록:
   ```python
   if name == "my_evaluator":
       return MyEvaluator()
   ```
5. `config.OSMU_EVALUATOR=my_evaluator` 로 전환 가능.
6. 단위 테스트 — `tests/test_basic.py` 에 `test_my_evaluator_basic` 추가.

### 1.3 점수 모델 정렬

`Evaluation.raw` 에 다음 키를 채우면 `KeywordResearcher` 가 자동으로 등급/풀 관리에 사용:

```python
Evaluation(
    score=82.5,
    commercial_intent=0.85,
    raw={
        "evaluator": "my_evaluator",
        "components": {"axis_a": 30, "axis_b": 40, ...},
        "weights": {"axis_a": 40, "axis_b": 30, ...},
    },
)
```

---

## 2. 새 Storage 백엔드

DB / 클라우드 시트 / 파일 등 다른 영속화 백엔드.

### 2.1 인터페이스

```python
# src/osmu_kr/storage/base.py
class BaseStorage(ABC):
    name: str
    # keyword pool
    def list_pool() / get_pool() / upsert_pool() / delete_pool() / replace_pool()
    # content
    def list_content() / append_content() / update_content() / delete_content() / replace_content()
    # history (옵션)
    def list_history() / append_history()
    # v13: keyword_usages, accounts, config (in-memory fallback 제공)
    def list_usages() / get_active_usage() / upsert_usage() / list_usages_by_keyword()
    def list_accounts() / get_account() / upsert_account() / get_active_account()
    def get_config() / set_config() / delete_config() / list_config()
```

### 2.2 구현 단계

1. `src/osmu_kr/storage/my_backend.py` — `BaseStorage` 상속.
2. **필수 메서드 9개** 만 구현하면 기본 동작 (`replace_content`, `update_content`, `delete_content` 는 base 에 기본 구현).
3. v13 안전장치를 쓰려면 `list_usages` / `upsert_usage` / `get_active_usage` 도 override (안 하면 in-memory fallback — 휘발성).
4. `storage/factory.py` 의 `build_storage()` 에 분기 추가.
5. 테스트 — `tests/test_basic.py` 의 `test_sqlite_storage_*` 패턴 따라 라운드트립 검증.

### 2.3 예시 — `sqlite_local.py` vs `postgres.py` 차이점

- 컬럼 타입 정의 → `storage/{sqlite,postgres}_schema.py`
- 트랜잭션 / SQL 방언 / vector 컬럼 어댑터만 분기
- 같은 dataclass 를 같은 인터페이스로 입출력

---

## 3. 새 Writer

HTML 생성 백엔드 추가 (예: 다른 LLM, 로컬 SLM, 템플릿 기반 등).

### 3.1 인터페이스

```python
# src/osmu_kr/content_generator/interfaces.py
class BaseWriter(ABC):
    name: str = "base_writer"

    @abstractmethod
    def write(self, keyword, raw_content, *, sources=None, images=None, tone="전문적") -> str: ...

    # v13 권장 — 청사진 기반 (default 구현이 write() 에 위임)
    def write_from_blueprint(self, blueprint, normalized_sources=None,
                              *, images=None, tone="전문적") -> str: ...
```

### 3.2 구현 단계

1. `write()` 구현 — 실패 시 `RuntimeError` raise.
2. `write_from_blueprint()` 를 override 하면 v13 spec 정확히 정렬 (facts 만 인용, raw 비노출 등).
3. `Generator(writer=MyWriter())` 또는 default 등록.
4. 표절 방지 — `fact_based` 단락은 `normalized_sources` 의 facts 만 인용, 원본 raw 비노출.

---

## 4. 새 Publisher (발행 플랫폼)

Tistory 외 (Wordpress / 네이버 블로그 / Velog 등).

### 4.1 인터페이스

```python
# src/osmu_kr/publisher.py
class BasePublisher:
    name: str = "base"
    def publish(self, *, title, html, account, contents_id="") -> PublishResult: ...
```

### 4.2 구현 단계

1. `MyPlatformPublisher(BasePublisher)` 클래스.
2. `publish()` 에서 외부 API 호출 또는 Playwright. 실패 시 `PublishResult(success=False, error="...")`.
3. 인증 정보는 **accounts 테이블** 에 — `account.cookie_path`, `account.login_id` 등.
4. **`OSMU_PUBLISH_REAL=1` 가드 필수** — 의도치 않은 실 발행 차단.
5. `Publisher(storage, backend=MyPlatformPublisher())` 로 wrapper 가 게이트 4종(daily_limit / min_draft / similarity_cooldown / diversity) 자동 적용.

---

## 5. 새 Embedder

자기잠식 / 표절 검사용 임베딩 모델 교체.

### 5.1 인터페이스 (Protocol)

```python
# src/osmu_kr/content_generator/embedder.py
class BaseEmbedder(Protocol):
    name: str
    dim: int
    def encode(self, text: str) -> Optional[List[float]]: ...
```

### 5.2 가이드

- `dim` 일관성 유지 (다른 임베딩과 cosine 비교 가능해야 함). 기본 768 (`jhgan/ko-sroberta-multitask`).
- 모델 로드 실패 시 `StubEmbedder` 폴백 권장 (`KoSrobertaEmbedder` 패턴).
- `OSMU_EMBEDDER` 환경변수로 선택.

---

## 6. 새 Evaluator·Storage·Writer 패키지화 체크리스트

✅ `BaseXxx` 상속  
✅ 외부 키/의존성 없을 때 graceful fallback  
✅ `OSMU_EMBEDDER=stub python tests/test_basic.py` 회귀 0건  
✅ 신규 dataclass 는 `to_dict()` / `from_row()` 직렬화 메서드  
✅ 환경변수는 `OSMU_<카테고리>_<키>` + `config_manager.DEFAULTS` 에 등록  
✅ `factory.py` 분기 추가  
✅ 단위 테스트 + 회귀  
✅ README 의 “저장 모드 / 평가기 / 임베더” 표에 행 추가

---

## 7. 안전장치 계층과 통합

새 백엔드가 다음 시그널을 보내거나 받아야 한다:

| 시점 | 모듈 | 동작 |
|---|---|---|
| 키워드 선정 시 | `KeywordSafety.check_abuse_cooldown` | embedding 비교 후 cooldown 위반이면 `CooldownViolation` |
| 키워드 lock 시 | `SafetyLayer.start_lock` | `keyword_usages.status = in_progress` |
| 발행 완료 / 실패 시 | `SafetyLayer.mark_published / mark_failed` | `keyword_usages` 상태 전이 |
| Phase 1 후 | `Collector._self_cannibalization_warnings` | `raw_signals.self_cannibalization_warnings` 기록 |
| 발행 전 | `Publisher._check_*` 게이트 | 일/유사/다양성/min_draft 검사 |
| housekeeping | `Housekeeping.run()` | 매 `osmu-kr seed` 마다 timeout/eviction 자동 |

새 Publisher / Evaluator 는 이 흐름 안에 자연스럽게 통합돼야 한다.

---

## 8. 디버깅 / 로깅

- 모든 모듈은 `log = logging.getLogger(__name__)` 패턴.
- `osmu-kr -v <cmd>` 로 DEBUG 레벨.
- 외부 API 호출은 항상 try/except + 명확한 fallback 로깅.

---

## 9. 기여 워크플로우

1. issue 또는 작업 분할 (한 PR = 한 안전장치 또는 한 백엔드).
2. v13 spec 정렬 확인.
3. 회귀 통과 (118/118).
4. README + DEVELOPER_GUIDE 갱신.
5. PR 작성 — 변경 매트릭스 + 영향받는 안전장치 명시.

---

## 부록 — 자주 쓰는 헬퍼

```python
from osmu_kr.config_manager import ConfigManager
cm = ConfigManager(storage)
cm.get_int("publisher.daily_limit", 2)
cm.get_float("checker.plagiarism_overall_threshold", 0.15)
cm.set("custom.my_threshold", 0.7)

from osmu_kr.notifications import post_slack_message, notify_publish_done
post_slack_message("hello")

from osmu_kr.monitoring import UsageMonitor
um = UsageMonitor(storage)
um.record("anthropic", count=1)   # 임계 도달 시 Slack 자동 알림

from osmu_kr.researcher.safety import SafetyLayer
safety = SafetyLayer(storage)
safety.start_lock(kid, blog_id="x")
safety.mark_published(usage_id)
```
