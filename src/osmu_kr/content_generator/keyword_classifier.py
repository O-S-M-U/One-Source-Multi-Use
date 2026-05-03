"""키워드 도메인 분류기 — 검색 의도/섹션/이미지 검색어를 도메인별로 정의.

LLM 에 ‘이 키워드는 어떤 주제인지’ 명확히 알려주고, 도메인별 ‘수익형 콘텐츠
구조’ 를 강제하기 위한 모듈.

[ 지원 도메인 ]
  · GAME    — 비디오 게임
  · FINANCE — 재테크 / 주식 / ETF / 부동산
  · DIET    — 다이어트 / 식단 / 운동
  · IT      — 노트북 / 스마트폰 / AI / 챗GPT
  · BEAUTY  — 화장품 / 스킨케어
  · TRAVEL  — 여행지 / 호텔
  · FOOD    — 요리 / 레시피
  · GENERAL — 기본 (도메인 미식별)

도메인 분류는 단어 매칭 기반 — 정확히 같지 않아도 부분 매칭으로 잡는다.
실 운영에서는 LLM 호출로 더 정교하게 가능하지만, 이 모듈만으로도 90% 이상 커버.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List


class Domain(Enum):
    GAME = "game"
    FINANCE = "finance"
    DIET = "diet"
    IT = "it"
    BEAUTY = "beauty"
    TRAVEL = "travel"
    FOOD = "food"
    GENERAL = "general"


@dataclass
class DomainProfile:
    """도메인별 콘텐츠 생성 프로파일 — Writer/Image 가 함께 참조."""
    domain: Domain
    name_ko: str                        # "게임"
    description_ko: str                 # LLM 시스템 프롬프트에 삽입될 도메인 정의
    search_intents: List[str]           # 독자의 검색 의도 (LLM 안내용)
    section_titles: List[str]           # H2 섹션 강제 구조
    section_requirements: List[str]     # 각 섹션에 들어가야 할 ‘구체적 요소’
    image_query_templates: Dict[str, List[str]]  # role → 영어 검색어 후보
    extra_keywords: List[str] = field(default_factory=list)  # 본문에 자연스럽게 포함될 도메인 어휘


# ── 게임 ──
GAME_PROFILE = DomainProfile(
    domain=Domain.GAME,
    name_ko="게임",
    description_ko=(
        "이 키워드는 비디오 게임입니다. 독자는 단순 개념 설명이 아니라 "
        "실제 게임 플레이, 캐릭터/직업 정보, 시스템 이해, 초보자 가이드, "
        "전략·공략·팁, 추천 빌드/세팅을 찾고 있습니다. "
        "개념 일반론이 아니라 게임 내 구체적 요소를 다뤄야 합니다."
    ),
    search_intents=[
        "이 게임이 어떤 게임인지(장르/규칙/진영) 빠르게 파악",
        "처음 시작하는 초보자에게 필요한 기본 가이드",
        "추천 캐릭터/맵/모드/난이도",
        "실전에서 쓸 수 있는 핵심 팁과 전략",
        "초보가 자주 하는 실수와 회피법",
        "추천 세팅/빌드/입문 콘텐츠",
    ],
    section_titles=[
        "1. 게임 기본 개요 — 장르·규칙·진영",
        "2. 처음 시작하는 초보자 필수 가이드",
        "3. 핵심 플레이 팁과 전략",
        "4. 자주 하는 실수와 회피법",
        "5. 추천 세팅·빌드·다음 단계",
    ],
    section_requirements=[
        "장르(예: 비대칭 호러 PvP, MMORPG)·플레이어 수·기본 승리 조건·핵심 시스템 요소를 한 단락 안에 명시",
        "처음 시작할 때 캐릭터/모드 선택 기준 + 첫 매칭에서 해야 할 행동 2가지 + 익혀야 할 시스템 1~2개",
        "게임 내 구체 요소(맵 위치/스킬/타이밍/리소스 우선순위)를 활용한 실전 팁 최소 2개",
        "초보가 자주 하는 실수 2개 이상 + 각 실수의 ‘해결 동작’ 또는 ‘대안’",
        "입문 빌드/세팅 추천 + 다음에 시도할 만한 콘텐츠(상위 모드/콜라보/이벤트)",
    ],
    image_query_templates={
        "concept":    ["video game gameplay screen", "esports gaming"],
        "example":    ["gamer playing console", "game controller hands"],
        "comparison": ["gaming setup desk", "esports tournament arena"],
        "summary":    ["gaming community fans", "video game collection"],
    },
    extra_keywords=["게임 플레이", "캐릭터", "공략", "전략", "초보자 가이드", "팁", "빌드"],
)

# ── 금융/재테크 ──
FINANCE_PROFILE = DomainProfile(
    domain=Domain.FINANCE,
    name_ko="재테크/금융",
    description_ko=(
        "이 키워드는 재테크/투자/금융 상품입니다. 독자는 수익률·리스크·세금·"
        "포트폴리오 비교를 찾고 있습니다. 단순 정의가 아니라 실제 투자 의사결정에 "
        "도움이 되는 정보·예시·비교 기준을 제시해야 합니다."
    ),
    search_intents=[
        "이 상품/투자가 어떤 구조인지",
        "예상 수익률과 리스크",
        "비슷한 상품과의 비교",
        "초보 투자자가 시작하는 방법",
        "세금·수수료·거래 시간 등 실무",
        "추천 포트폴리오 또는 시작 금액",
    ],
    section_titles=[
        "1. 기본 개념과 구조",
        "2. 수익률·리스크 — 숫자로 보는 핵심",
        "3. 비슷한 대안과의 비교",
        "4. 초보 투자자가 시작하는 방법",
        "5. 자주 하는 실수와 체크리스트",
    ],
    section_requirements=[
        "상품/시장의 작동 구조(어디서 거래/누가 운용/기초자산) 한 단락 + 핵심 용어 정리",
        "최근 5년 수익률 또는 변동성 범위 + 가장 큰 리스크 1~2개",
        "비교 대상 1~2개와의 차이를 표 또는 단락으로 비교",
        "최소 시작 금액 + 계좌 종류(예: ISA/연금) + 첫 1년 운용 가이드",
        "실수 사례 2개 이상 + 각각의 회피 체크리스트",
    ],
    image_query_templates={
        "concept":    ["stock market chart", "financial graph trend"],
        "example":    ["investment portfolio analysis", "trading desk monitors"],
        "comparison": ["financial comparison chart", "calculator and documents"],
        "summary":    ["savings investment growth", "financial planning"],
    },
    extra_keywords=["수익률", "리스크", "세금", "수수료", "포트폴리오", "분산투자"],
)

# ── 다이어트 ──
DIET_PROFILE = DomainProfile(
    domain=Domain.DIET,
    name_ko="다이어트/건강",
    description_ko=(
        "이 키워드는 다이어트·건강·식단·운동 관련 주제입니다. 독자는 실제로 "
        "따라할 수 있는 구체적 방법(식단 예시, 운동 루틴, 생활 습관)을 찾고 있습니다. "
        "막연한 ‘건강하게 드세요’ 가 아니라 구체 수치/예시/대체식품을 제시해야 합니다."
    ),
    search_intents=[
        "이 방법이 어떤 원리인지",
        "구체적인 식단 또는 운동 예시",
        "대체 식품·실제 메뉴 추천",
        "기간/주기/강도 등 실행 조건",
        "주의사항·부작용·체질별 적합성",
        "지속 가능하게 만드는 팁",
    ],
    section_titles=[
        "1. 어떤 원리로 작동하는지",
        "2. 실제 식단/루틴 예시",
        "3. 대체 식품·메뉴 추천",
        "4. 주의사항·체질별 적합성",
        "5. 꾸준히 하기 위한 팁",
    ],
    section_requirements=[
        "원리(칼로리 적자/혈당/단백질 비율 등) 한 단락 + 일반적 효과 범위(예: 한 달 -2kg)",
        "하루 식단 1일치 또는 일주일 운동 루틴 — 시간·양·강도 명시",
        "흔한 식품 → 대체식품 표 또는 리스트 (3쌍 이상)",
        "주의가 필요한 체질·질환 + 부작용 가능 신호",
        "지속 가능 팁 3가지 + 1주차/4주차 체감 변화 가이드",
    ],
    image_query_templates={
        "concept":    ["healthy meal plate", "balanced diet food"],
        "example":    ["meal prep containers", "salad bowl ingredients"],
        "comparison": ["before after fitness", "healthy vs junk food"],
        "summary":    ["water bottle exercise", "yoga mat workout"],
    },
    extra_keywords=["식단", "칼로리", "단백질", "운동 루틴", "주차별 변화"],
)

# ── IT/디지털 ──
IT_PROFILE = DomainProfile(
    domain=Domain.IT,
    name_ko="IT/디지털",
    description_ko=(
        "이 키워드는 IT 제품·서비스·기술입니다(노트북/스마트폰/AI 도구 등). "
        "독자는 스펙·가격·실사용 후기·대안 비교·구매 가이드를 찾고 있습니다. "
        "단순 설명이 아니라 비교 가능한 구체 수치/모델명/사용 시나리오를 제시해야 합니다."
    ),
    search_intents=[
        "이 제품/서비스가 무엇인지",
        "주요 스펙·가격대·구매처",
        "비슷한 대안과의 비교",
        "실사용 시나리오·후기",
        "구매 시 체크포인트",
        "추천 모델/플랜",
    ],
    section_titles=[
        "1. 핵심 기능과 차별점",
        "2. 스펙·가격·라인업",
        "3. 실사용 시나리오 — 누구에게 적합한가",
        "4. 비슷한 대안과 비교",
        "5. 구매 전 체크포인트",
    ],
    section_requirements=[
        "핵심 기능 3가지 + 경쟁 제품 대비 차별점 1개",
        "주요 라인업 2~3개 가격대 + 출시 연도/제조사",
        "구체 사용자 페르소나 2명(예: 대학생/디자이너) 별 적합도",
        "비교 대상 1~2개 — 가격·성능·생태계 표",
        "구매 전 체크 7개 + 첫 1주일 세팅 가이드",
    ],
    image_query_templates={
        "concept":    ["modern laptop on desk", "smartphone in hand"],
        "example":    ["coding workstation setup", "tech gadgets flat lay"],
        "comparison": ["product comparison side by side", "tech specs"],
        "summary":    ["tech accessories desk", "minimalist tech setup"],
    },
    extra_keywords=["스펙", "가격", "라인업", "실사용", "비교"],
)

# ── 뷰티 ──
BEAUTY_PROFILE = DomainProfile(
    domain=Domain.BEAUTY,
    name_ko="뷰티/화장품",
    description_ko=(
        "이 키워드는 화장품·스킨케어·메이크업입니다. 독자는 성분·피부타입·"
        "사용 방법·실사용 후기를 찾고 있습니다. 일반론이 아니라 피부타입별·"
        "상황별 추천을 제시해야 합니다."
    ),
    search_intents=["성분과 작동", "피부타입별 적합성", "사용 순서", "추천 제품/브랜드", "주의사항"],
    section_titles=[
        "1. 핵심 성분과 작동 원리",
        "2. 피부타입별 적합성",
        "3. 사용 순서·바르는 법",
        "4. 추천 제품/브랜드",
        "5. 주의사항과 부작용",
    ],
    section_requirements=[
        "주요 성분 + 효과(보습/진정/미백 등) 명시",
        "건성/지성/복합/민감성 4타입별 적합 여부",
        "아침/저녁 루틴 안 사용 순서 + 양",
        "가격대별 추천 2~3개",
        "흔한 부작용 + 패치테스트 방법",
    ],
    image_query_templates={
        "concept":    ["skincare products flatlay", "cosmetic ingredients"],
        "example":    ["woman applying skincare", "morning routine"],
        "comparison": ["product packaging side by side", "before after skin"],
        "summary":    ["minimalist beauty shelf", "self care"],
    },
    extra_keywords=["성분", "피부타입", "루틴", "보습", "트러블"],
)

# ── 여행 ──
TRAVEL_PROFILE = DomainProfile(
    domain=Domain.TRAVEL,
    name_ko="여행",
    description_ko=(
        "이 키워드는 여행지·호텔·여행 코스입니다. 독자는 ‘갈만한지’, ‘어떤 코스/숙소’, "
        "‘예산·일정·계절’ 같은 실행 정보를 찾고 있습니다."
    ),
    search_intents=["가볼 만한지", "추천 코스/숙소", "예산·일정", "성수기/계절", "주의사항"],
    section_titles=[
        "1. 어떤 곳인지 한눈에",
        "2. 추천 코스 — 1박 2일 / 2박 3일",
        "3. 숙소·먹거리 추천",
        "4. 예산·시즌·교통",
        "5. 알아두면 좋은 팁",
    ],
    section_requirements=[
        "위치·분위기·대표 명소 3개",
        "일정 길이별 코스 + 시간 배분",
        "예산대별 숙소 2개 + 현지 음식 3가지",
        "1인 평균 예산 + 추천 시즌 + 교통편 정리",
        "현지 매너·환전·통신 등 실용 팁",
    ],
    image_query_templates={
        "concept":    ["travel landscape scenic", "tourist landmark"],
        "example":    ["travel packing flatlay", "couple traveling"],
        "comparison": ["map planning travel", "passport ticket"],
        "summary":    ["sunset beach travel", "scenic mountain"],
    },
    extra_keywords=["코스", "숙소", "예산", "시즌", "교통편"],
)

# ── 음식/요리 ──
FOOD_PROFILE = DomainProfile(
    domain=Domain.FOOD,
    name_ko="음식/요리",
    description_ko=(
        "이 키워드는 음식·요리·레시피입니다. 독자는 만드는 법·재료·시간·"
        "대체 재료·실패 회피 팁을 찾고 있습니다."
    ),
    search_intents=["기본 레시피", "재료/대체재", "조리 시간/난이도", "응용 메뉴", "보관/실수 회피"],
    section_titles=[
        "1. 어떤 음식인지·기원",
        "2. 기본 레시피 (재료·순서)",
        "3. 대체 재료와 응용 메뉴",
        "4. 자주 하는 실수와 회피법",
        "5. 보관·재가열·페어링 팁",
    ],
    section_requirements=[
        "음식 소개 + 인기 이유 1~2개",
        "정확한 재료 표 + 단계별 조리법(시간 명시)",
        "대체 재료 3쌍 + 응용 변형 1~2개",
        "흔한 실수 2개 + 회피 팁",
        "보관 기간 + 재가열 방법 + 어울리는 음식/술",
    ],
    image_query_templates={
        "concept":    ["delicious food plated", "korean food traditional"],
        "example":    ["cooking ingredients flatlay", "kitchen prep"],
        "comparison": ["recipe variations", "food side by side"],
        "summary":    ["dining table setting", "food photography"],
    },
    extra_keywords=["재료", "레시피", "조리 시간", "보관"],
)

# ── 일반 (기본 폴백) ──
GENERAL_PROFILE = DomainProfile(
    domain=Domain.GENERAL,
    name_ko="일반",
    description_ko=(
        "이 키워드의 도메인이 자동 분류되지 않았습니다. 독자가 무엇을 찾는지 "
        "raw_content 의 흐름과 키워드의 단어 자체에서 추론해, 정의·실제 활용·"
        "주의사항·요약 흐름으로 작성하세요."
    ),
    search_intents=[
        "이 주제가 무엇인지",
        "실제로 어떻게 쓰이는지",
        "선택/판단 시 주의할 점",
        "다음 단계로 무엇을 할지",
    ],
    section_titles=[
        "1. 기본 개념과 핵심",
        "2. 실제 활용 사례",
        "3. 선택·판단 시 주의사항",
        "4. 핵심 정리",
    ],
    section_requirements=[
        "정의 + 왜 중요한지 + 간단한 예시 1개",
        "구체 시나리오 2개 (누가/언제/어떻게/결과)",
        "흔한 실수 또는 오해 + 비교 기준",
        "핵심 포인트 3~5개 정리 + 다음 행동",
    ],
    image_query_templates={
        "concept":    ["abstract concept illustration", "minimalist workspace"],
        "example":    ["business presentation", "people collaboration"],
        "comparison": ["balance scale concept", "comparison chart"],
        "summary":    ["checklist clipboard", "summary notebook"],
    },
    extra_keywords=[],
)


# ── 분류용 키워드 사전 ────────────────────────────────
GAME_TERMS = (
    "데드바이데이라이트", "dead by daylight", "dbd",
    "롤", "리그오브레전드", "league of legends", "lol",
    "오버워치", "overwatch",
    "발로란트", "valorant",
    "배틀그라운드", "배그", "pubg",
    "마인크래프트", "minecraft",
    "엘든링", "elden ring",
    "원신", "genshin",
    "스타크래프트", "starcraft",
    "디아블로", "diablo",
    "포트나이트", "fortnite",
    "사이버펑크", "cyberpunk",
    "젤다", "zelda",
    "포켓몬", "pokemon",
    "철권", "tekken",
    "스트리트 파이터", "스파", "street fighter",
    "메이플", "메이플스토리",
    "리니지", "lineage",
    "로스트아크", "lost ark",
    "검은사막",
    "와우", "wow", "world of warcraft",
    "FPS", "MMORPG", "RPG",
    "게임", "공략", "캐릭터", "퍽", "빌드", "스킬",
)
FINANCE_TERMS = (
    "etf", "ETF", "주식", "재테크", "투자", "부동산",
    "펀드", "예금", "적금", "채권", "코인", "비트코인",
    "이더리움", "주가", "수익률", "배당",
    "ISA", "연금", "절세",
)
DIET_TERMS = (
    "다이어트", "식단", "건강", "운동", "헬스", "요가",
    "필라테스", "단백질", "칼로리", "키토", "간헐적 단식",
    "체중", "감량", "근력",
)
IT_TERMS = (
    "노트북", "맥북", "스마트폰", "아이폰", "갤럭시",
    "패드", "ipad", "태블릿",
    "AI", "ai", "GPT", "gpt", "ChatGPT", "챗GPT",
    "클로드", "claude", "Claude",
    "프로그래밍", "코딩", "파이썬", "자바스크립트",
    "코드", "개발자",
)
BEAUTY_TERMS = (
    "화장품", "스킨케어", "메이크업", "립스틱", "선크림",
    "토너", "에센스", "세럼", "크림", "마스크팩",
    "쿠션", "파운데이션",
)
TRAVEL_TERMS = (
    "여행", "호텔", "숙소", "리조트", "맛집",
    "코스", "관광", "항공권",
    "도쿄", "오사카", "후쿠오카", "교토", "오키나와",
    "방콕", "치앙마이", "파리", "런던", "뉴욕",
    "발리", "다낭",
)
FOOD_TERMS = (
    "레시피", "요리", "음식", "메뉴", "맛집",
    "파스타", "샐러드", "스프", "찌개", "구이",
    "디저트", "베이킹",
)


def _normalize(text: str) -> str:
    return (text or "").replace(" ", "").lower()


def classify(keyword: str) -> Domain:
    """키워드 → 도메인 분류. 매칭 실패 시 GENERAL."""
    if not keyword:
        return Domain.GENERAL
    norm = _normalize(keyword)
    raw = keyword.lower()

    # 게임이 가장 흔한 도메인이라 우선 매칭
    for term in GAME_TERMS:
        if _normalize(term) in norm or term.lower() in raw:
            return Domain.GAME
    for term in FINANCE_TERMS:
        if _normalize(term) in norm or term.lower() in raw:
            return Domain.FINANCE
    for term in DIET_TERMS:
        if _normalize(term) in norm:
            return Domain.DIET
    for term in IT_TERMS:
        if _normalize(term) in norm:
            return Domain.IT
    for term in BEAUTY_TERMS:
        if _normalize(term) in norm:
            return Domain.BEAUTY
    for term in TRAVEL_TERMS:
        if _normalize(term) in norm:
            return Domain.TRAVEL
    for term in FOOD_TERMS:
        if _normalize(term) in norm:
            return Domain.FOOD
    return Domain.GENERAL


_PROFILES: Dict[Domain, DomainProfile] = {
    Domain.GAME: GAME_PROFILE,
    Domain.FINANCE: FINANCE_PROFILE,
    Domain.DIET: DIET_PROFILE,
    Domain.IT: IT_PROFILE,
    Domain.BEAUTY: BEAUTY_PROFILE,
    Domain.TRAVEL: TRAVEL_PROFILE,
    Domain.FOOD: FOOD_PROFILE,
    Domain.GENERAL: GENERAL_PROFILE,
}


def profile_for(keyword: str) -> DomainProfile:
    """키워드 → 도메인 프로파일."""
    return _PROFILES[classify(keyword)]
