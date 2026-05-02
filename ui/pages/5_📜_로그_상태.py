"""⑤ 로그 / 상태."""
from __future__ import annotations

import streamlit as st
from services import (clear_activities, get_activities, is_mirror_backend,
                       settings_snapshot, sync_status)

st.set_page_config(page_title="로그 / 상태", page_icon="📜", layout="wide")
st.title("📜 ⑤ 로그 & 상태")

snap = settings_snapshot()
c1, c2, c3 = st.columns(3)
c1.metric("저장 위치",
          {"sheets": "구글 시트", "mirror": "동기화", "local": "내 컴퓨터", "auto": "자동"}.get(
              snap["storage_backend"], snap["storage_backend"]))
c2.metric("평가 방식", snap["evaluator"])
c3.metric("자격증명", "있음" if snap["has_credentials"] else "없음(폴백 동작)")

with st.expander("자세한 설정 보기"):
    st.json(snap)

if is_mirror_backend():
    st.divider()
    st.subheader("🔄 양방향 동기화 상태")
    s = sync_status() or {}
    cc1, cc2, cc3, cc4 = st.columns(4)
    cc1.metric("Sheets 연동", "🟢 켜짐" if s.get("sheets_enabled") else "🔴 미연결")
    cc2.metric("보류 변경", f"{s.get('pending_writes', 0)} 건")
    cc3.metric("마지막 새로고침", s.get("last_pull_at") or "—")
    cc4.metric("마지막 업로드", s.get("last_push_at") or "—")
    if s.get("sheet_url"):
        st.markdown(f"🔗 [구글 시트 열기]({s['sheet_url']})")
    if s.get("last_error"):
        st.warning(f"마지막 오류: {s['last_error']}")

st.divider()
st.subheader("최근 작업 로그")
logs = get_activities()
top1, top2 = st.columns([5, 1])
with top2:
    if st.button("🧹  로그 비우기", use_container_width=True):
        clear_activities(); st.rerun()
if not logs:
    st.info("아직 기록된 작업이 없어요.")
else:
    rows = [{"시각": r["ts"],
             "구분": f"{ {'success':'✅','info':'ℹ️','warning':'⚠️','error':'❌'}.get(r['level'],'•')} {r['where']}",
             "메시지": r["message"], "상세": r["detail"]} for r in logs]
    st.dataframe(rows, use_container_width=True, hide_index=True)
