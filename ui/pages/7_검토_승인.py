"""7. 사람 검토 / 승인 — checker Stage 2 UI (ops-4).

[ 흐름 ]
  status=사용자검토중 콘텐츠 목록 → 미리보기 → ✅승인 / ❌거절 / 🚀발행 / 🔍재검증
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from osmu_kr import Config, KeywordResearcher
from osmu_kr.checker import Checker
from osmu_kr.notifications import submit_for_review, notify_publish_done
from osmu_kr.publisher import MockPublisher, Publisher, TistoryPlaywrightPublisher
from osmu_kr.researcher.safety import SafetyLayer


st.set_page_config(page_title="검토 / 승인", page_icon="📝", layout="wide")
st.title("📝 사람 검토 / 승인")
st.caption("checker Stage 2 — 사용자검토중 콘텐츠를 검토하고 승인/거절/발행")


@st.cache_resource
def _get_researcher() -> KeywordResearcher:
    return KeywordResearcher(Config())


rs = _get_researcher()

# ── 상단 액션 ─────────────────────────────────────────
col_a, col_b = st.columns([3, 1])
with col_b:
    if st.button("🔄 새로고침", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

contents = rs.storage.list_content()
in_review = [c for c in contents if c.status in {"사용자검토중", "시스템검증대기"}]

st.markdown(f"### 📋 검토 대기: **{len(in_review)}건**")

if not in_review:
    st.info("검토할 콘텐츠가 없습니다. `osmu-kr generate` 또는 `osmu-kr check-content --submit` 으로 진입시키세요.")
    st.stop()

# ── 필터 ───────────────────────────────────────────────
status_filter = st.multiselect(
    "status 필터",
    options=["사용자검토중", "시스템검증대기", "승인완료", "발행완료", "실패", "발행차단"],
    default=["사용자검토중", "시스템검증대기"],
)
display = [c for c in contents if c.status in status_filter]

# ── 목록 + 상세 ────────────────────────────────────────
selected_id = st.selectbox(
    "검토할 글 선택",
    options=[c.id for c in display],
    format_func=lambda i: f"{i} | {next((c.keyword for c in display if c.id == i), '?')} "
                           f"[{next((c.status for c in display if c.id == i), '?')}]",
)

if not selected_id:
    st.stop()

rec = next(c for c in display if c.id == selected_id)

col_meta, col_preview = st.columns([1, 2])
with col_meta:
    st.subheader("메타")
    st.write(f"**id**: `{rec.id}`")
    st.write(f"**keyword**: {rec.keyword}")
    st.write(f"**title**: {rec.title or rec.title_final or '(없음)'}")
    st.write(f"**status**: `{rec.status}`")
    st.write(f"**created_at**: {rec.created_at}")
    if rec.platform_url:
        st.write(f"**platform_url**: {rec.platform_url}")
    if rec.error_log:
        st.error(rec.error_log[:300])

with col_preview:
    st.subheader("본문 미리보기")
    if rec.refined_post:
        st.components.v1.html(rec.refined_post, height=500, scrolling=True)
    else:
        st.warning("refined_post 가 비어있음")

# ── 액션 버튼 ──────────────────────────────────────────
st.markdown("---")
st.subheader("🛠 액션")
c1, c2, c3, c4 = st.columns(4)

with c1:
    if st.button("🔍 시스템 재검증", use_container_width=True):
        chk = Checker(config_mgr=rs.config_mgr)
        result = chk.run(rec.refined_post)
        st.write(result.summary())
        if result.issues:
            for i in result.issues:
                st.error(i)
        if result.warnings:
            for w in result.warnings:
                st.warning(w)
        if result.passed and rec.status != "사용자검토중":
            rs.storage.update_content(rec.id, status="사용자검토중")
            sub_res = submit_for_review(
                content_id=rec.id,
                title=rec.title or rec.title_final or rec.keyword,
                check_result=result,
            )
            st.success(f"검토 요청 전송: sent={sub_res.sent}")
        st.rerun()

with c2:
    if st.button("✅ 승인", use_container_width=True, type="primary"):
        rs.storage.update_content(rec.id, status="승인완료")
        st.success("승인완료. 발행 버튼 또는 CLI 로 `osmu-kr publish` 실행하세요.")
        st.rerun()

with c3:
    reason = st.text_input("거절 사유", placeholder="(선택)")
    if st.button("❌ 거절", use_container_width=True):
        rs.storage.update_content(
            rec.id, status="실패",
            error_log=(rec.error_log + f" | rejected: {reason}").strip(" |"),
        )
        safety = SafetyLayer(rs.storage)
        for u in rs.storage.list_usages():
            if u.contents_id == rec.id and u.status == "in_progress":
                safety.mark_failed(u.id, note=f"ui_rejected: {reason}")
                break
        st.warning(f"거절됨. 키워드 잠금 해제됨.")
        st.rerun()

with c4:
    use_mock = st.checkbox("Mock 발행 (드라이런)", value=True)
    if st.button("🚀 발행", use_container_width=True, disabled=(rec.status != "승인완료")):
        backend = MockPublisher() if use_mock else TistoryPlaywrightPublisher(
            config_mgr=rs.config_mgr,
        )
        pub = Publisher(rs.storage, backend=backend, config_mgr=rs.config_mgr)
        result = pub.publish(rec.id)
        if result.success:
            st.success(f"발행 완료: {result.platform_url}")
            notify_publish_done(
                content_id=rec.id,
                title=rec.title or rec.keyword,
                platform_url=result.platform_url,
            )
        else:
            st.error(result.summary())
        st.rerun()
