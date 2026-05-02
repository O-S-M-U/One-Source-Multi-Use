"""기술 용어 → 한국어 친화 메시지 번역."""
from __future__ import annotations


def humanize_error(e: BaseException) -> str:
    msg = str(e)
    name = type(e).__name__
    if isinstance(e, PermissionError):
        if "seed_cooldown" in msg or "cooldown" in msg:
            return ("🛑 최근에 비슷한 주제의 글을 작성하셔서 잠시 사용할 수 없어요. "
                    "‘설정’에서 재사용 간격(일)을 조정하거나, 다른 주제의 키워드를 선택해주세요.")
        return "🛑 이 작업은 지금 실행할 수 없어요. 잠시 후 다시 시도해주세요."
    if isinstance(e, KeyError):
        return "🔍 선택한 키워드를 더 이상 찾을 수 없어요. ‘새로고침’을 눌러주세요."
    if isinstance(e, FileNotFoundError):
        return "📁 필요한 파일을 찾지 못했어요. 설정 화면에서 자격증명/시트 정보를 확인해주세요."
    if isinstance(e, ValueError):
        return f"⚠️ 입력값을 확인해주세요. ({msg})"
    if isinstance(e, ConnectionError):
        return "🌐 외부 서비스에 연결할 수 없어요. 잠시 후 다시 시도해주세요."
    if isinstance(e, RuntimeError) and "gspread" in msg:
        return ("🔧 구글 시트 연동 패키지를 찾을 수 없어요. "
                "터미널에서 `pip install gspread google-auth` 를 실행하거나, "
                "설정에서 ‘로컬 파일’ 백엔드로 전환해주세요.")
    return f"⚠️ 예상치 못한 문제가 발생했어요. ‘로그/상태’ 화면을 확인하세요. (유형: {name})"
