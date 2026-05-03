"""③ 추천 & 선택."""
from __future__ import annotations

import streamlit as st
from services import (get_content_dataframe, get_researcher,
                       humanize_error, log_activity)

st.set_page_config(page_title="추천 & 선택", page_icon="⭐", layout="wide")
st.title("⭐ ③ 추천 & 선택")
st.caption("지금 가장 가치 있는 키워드를 추천드려요.")

rs = get_researcher()
top_n = st.slider("몇 개까지 보여드릴까요?", 3, 20, 5)
recs = rs.recommend(top_n=top_n)

if not recs:
    st.info("지금 추천드릴 키워드가 없어요. ‘① 키워드 생성’ 에서 새 주제를 추가하거나, "
            "‘설정’ 에서 재사용 간격을 줄여보세요.")
else:
    for i, it in enumerate(recs, 1):
        with st.container(border=True):
            head, action = st.columns([5, 1.4])
            with head:
                st.markdown(f"### {i}. {it.keyword}")
                st.caption(f"주제: {it.seed_keyword}  ·  ID: {it.keyword_id}")
                cols = st.columns(4)
                cols[0].metric("점수", f"{it.score:.1f}")
                cols[1].metric("월 검색량", f"{it.search_volume:,}")
                cols[2].metric("경쟁도", it.competition)
                cols[3].metric("CPC(원)", f"{it.cpc:,.0f}")
            with action:
                st.write("")
                if st.button("✍️  이 키워드로 글 쓰기",
                             key=f"pick_{it.keyword_id}",
                             type="primary", use_container_width=True):
                    try:
                        rec = rs.select_for_content(it.keyword_id, title_final=it.keyword)
                        st.success(f"✅  ‘{rec.keyword}’ 가 작성 대기에 등록됐어요. (글 ID: {rec.id})")
                        log_activity("success", "추천/선택",
                                     f"키워드 ‘{rec.keyword}’ 선택 → 글 ID {rec.id} 등록")
                        st.rerun()
                    except Exception as e:
                        msg = humanize_error(e)
                        st.error(msg)
                        log_activity("error", "추천/선택", msg, str(e))

st.divider()
st.subheader("📝 최근 작성 대기 / 작성한 글")
df = get_content_dataframe()
if hasattr(df, "empty") and df.empty:
    st.info("아직 작성한 글이 없어요. ‘① 키워드 생성’ 후 위에서 ‘이 키워드로 글 쓰기’를 눌러보세요.")
else:
    STATUS_LABEL = {
        "generated": "🟢 생성 완료", "대기중": "⏳ 작성 대기",
        "생성중": "✏️ 생성 중", "검토중": "👀 검토 중",
        "승인완료": "✅ 승인 완료", "발행완료": "🚀 발행 완료",
        "발행차단": "🛑 발행 차단", "실패": "❌ 실패",
    }
    show = df.copy() if hasattr(df, "copy") else df
    if hasattr(show, "rename"):
        show["상태"] = show["status"].map(lambda s: STATUS_LABEL.get(s, f"• {s}"))
        show = show.rename(columns={
            "id": "글 ID", "keyword": "키워드", "seed_keyword": "주제",
            "title_final": "제목", "created_at": "등록일시",
            "platform_url": "발행 URL",
        })
        cols = ["글 ID", "키워드", "주제", "상태", "제목", "등록일시", "발행 URL"]
        show = show[[c for c in cols if c in show.columns]]
        show = show.sort_values(by="등록일시", ascending=False)
    st.dataframe(show, use_container_width=True, hide_index=True)
    st.caption("💡 본문 HTML 을 그대로 보고 싶으면 좌측 메뉴의 **‘📝 ⑥ 생성된 콘텐츠’** 페이지로 이동하세요.")
