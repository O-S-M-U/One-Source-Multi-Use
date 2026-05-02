"""홈 대시보드."""
from __future__ import annotations

import streamlit as st

from services import (
    get_researcher,
    settings_snapshot,
)

st.set_page_config(page_title="O.S.M.U Keyword Researcher",
                   page_icon="🌱", layout="wide", initial_sidebar_state="expanded")
st.title("🌱 O.S.M.U Keyword Researcher")
st.caption("수익형 블로그 콘텐츠 자동화의 첫 단계 — 씨앗 키워드 한 단어로 황금 키워드를 자동으로 찾아드려요.")

if "_seen_intro" not in st.session_state:
    st.info("**처음 사용하시나요?** 좌측 메뉴에서 **‘① 키워드 생성’** 을 눌러 씨앗 키워드 한 단어로 시작해보세요.")
    st.session_state["_seen_intro"] = True

try:
    rs = get_researcher()
    pool = rs.storage.list_pool()
    content = rs.storage.list_content()
    settings = settings_snapshot()
except Exception as e:
    st.error(f"초기 로딩 중 문제가 있었어요: {e}")
    st.stop()

c1, c2, c3, c4 = st.columns(4)
c1.metric("저장된 황금 키워드", f"{sum(1 for it in pool if it.status == 'golden')} 개")
c2.metric("전체 풀 크기", f"{len(pool)} 개", help=f"최대 {settings['POOL_MAX_SIZE']}개까지 보관")
c3.metric("작성한 글 수", f"{len(content)} 건")
c4.metric("저장 위치",
          {"sheets": "구글 시트", "mirror": "내 컴퓨터 + 시트(동기화)",
           "local": "내 컴퓨터", "auto": "자동"}.get(settings["storage_backend"], settings["storage_backend"]),
          help="‘설정’ 화면에서 변경 가능합니다.")

st.divider()
st.subheader("바로 시작하기")
b1, b2, b3 = st.columns(3)
with b1:
    if st.button("🌱  씨앗 키워드 입력하기", use_container_width=True, type="primary"):
        st.switch_page("pages/1_🌱_키워드_생성.py")
with b2:
    if st.button("⭐  추천 키워드 보기", use_container_width=True):
        st.switch_page("pages/3_⭐_추천_및_선택.py")
with b3:
    if st.button("📦  키워드 풀 관리", use_container_width=True):
        st.switch_page("pages/2_📦_키워드_풀.py")

st.subheader("⭐ 지금 추천드리는 키워드 TOP 5")
recs = rs.recommend(top_n=5)
if not recs:
    st.info("아직 추천 가능한 키워드가 없어요. ‘① 키워드 생성’ 에서 씨앗 키워드를 입력해 풀을 채워주세요.")
else:
    rec_rows = [{
        "순위": i + 1, "키워드": it.keyword,
        "점수": round(it.score, 1),
        "월 검색량": it.search_volume,
        "경쟁도": it.competition,
        "주제(seed)": it.seed_keyword,
    } for i, it in enumerate(recs)]
    st.dataframe(rec_rows, use_container_width=True, hide_index=True)

st.divider()
st.caption(
    "💡 **사용 흐름** &nbsp; ① 키워드 생성 → ② 풀에서 확인 → ③ 추천된 키워드 선택 → "
    "**자동으로 ‘작성 대기’ 상태로 기록됩니다.**"
)
