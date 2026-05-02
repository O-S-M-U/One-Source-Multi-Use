"""② 키워드 풀 관리 화면."""
from __future__ import annotations

import os
import streamlit as st
from services import (
    get_local_xlsx_path, get_pool_dataframe, get_researcher,
    humanize_error, is_mirror_backend, log_activity,
    pull_from_sheets, push_to_sheets, sync_status,
)

st.set_page_config(page_title="키워드 풀 관리", page_icon="📦", layout="wide")
st.title("📦 ② 키워드 풀 관리")
st.caption("저장된 키워드를 한 곳에서 정리하세요.")


def _refresh():
    for k in list(st.session_state.keys()):
        if k.startswith("_pool_editor"):
            del st.session_state[k]
    st.rerun()


# 엑셀 다운로드
xlsx_path = get_local_xlsx_path()
if xlsx_path:
    with st.container(border=True):
        col_l, col_r = st.columns([3, 1.4])
        with col_l:
            st.markdown(f"**📊 엑셀 파일로도 관리할 수 있어요**  &nbsp; "
                        f"`{os.path.basename(xlsx_path)}`")
        with col_r:
            try:
                with open(xlsx_path, "rb") as f:
                    st.download_button(
                        "📥  엑셀로 내려받기", data=f.read(),
                        file_name=os.path.basename(xlsx_path),
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True)
            except Exception as e:
                st.caption(f"⚠️ {e}")

# 양방향 동기화 패널
if is_mirror_backend():
    status = sync_status() or {}
    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([1.6, 1.6, 1.4, 1.6])
        c1.metric("Google Sheets 연동",
                  "🟢 켜짐" if status.get("sheets_enabled") else "🔴 미연결")
        pending = status.get("pending_writes", 0)
        c2.metric("쓰기 대기열", f"보류 변경 {pending}건" if pending else "동기화 ✓")
        last_pull = status.get("last_pull_at") or "—"
        c3.metric("마지막 새로고침", last_pull[-8:] if last_pull != "—" else "—",
                  help=last_pull)
        last_push = status.get("last_push_at") or "—"
        c4.metric("마지막 업로드", last_push[-8:] if last_push != "—" else "—",
                  help=last_push)
        b1, b2, b3 = st.columns([1, 1, 2])
        with b1:
            if st.button("☁️  시트에서 새로고침", use_container_width=True):
                with st.spinner("구글 시트에서 가져오는 중…"):
                    res = pull_from_sheets()
                if res.get("ok"):
                    st.success(f"가져오기 완료 — 키워드 {res['pool_count']}개")
                    log_activity("success", "동기화", "Sheets → 로컬 pull")
                    _refresh()
                else:
                    st.error(f"가져오기 실패: {res.get('reason')}")
        with b2:
            if st.button("📤  시트로 업로드", use_container_width=True):
                with st.spinner("업로드 중…"):
                    res = push_to_sheets()
                if res.get("ok"):
                    st.success(f"업로드 완료 — 키워드 {res['pool_count']}개")
                    log_activity("success", "동기화", "로컬 → Sheets push")
                else:
                    st.error(f"업로드 실패: {res.get('reason')}")
        with b3:
            if status.get("sheet_url"):
                st.markdown(f"<div style='padding-top:6px'>🔗 "
                            f"<a href='{status['sheet_url']}' target='_blank'>구글 시트 열기</a></div>",
                            unsafe_allow_html=True)

# prune
top_l, top_r = st.columns([3, 2])
with top_l:
    st.markdown("##### 풀 정리(prune)")
    st.caption("‘유효기간이 지난 키워드’를 자동으로 재평가/삭제하고, 풀 크기를 최대 한도로 맞춥니다.")
with top_r:
    if st.button("🧹  지금 정리 실행", use_container_width=True):
        try:
            with st.spinner("정리 중…"):
                pool, report = get_researcher().prune()
            st.success(f"정리 완료 — {report.summary()}")
            log_activity("success", "풀 정리", "prune 실행", report.summary())
            _refresh()
        except Exception as e:
            log_activity("error", "풀 정리", humanize_error(e), str(e))
            st.error(humanize_error(e))

st.divider()

df = get_pool_dataframe()
total = len(df)
if total == 0:
    st.info("아직 저장된 키워드가 없어요. ‘① 키워드 생성’ 에서 시작해보세요.")
    st.stop()

f1, f2, f3, f4 = st.columns([2, 2, 2, 2])
with f1:
    statuses = sorted(set(df["status"])) if hasattr(df, "columns") else []
    status_filter = st.multiselect("상태 필터", options=["전체"] + list(statuses), default=["전체"])
with f2:
    sort_by = st.selectbox("정렬 기준",
                           options=["점수(높은순)", "점수(낮은순)", "검색량(높은순)", "최근 평가일순"])
with f3:
    keyword_search = st.text_input("키워드 검색", placeholder="ex) 다이어트")
with f4:
    seed_search = st.text_input("주제 필터", placeholder="ex) AI ETF")

view = df.copy()
if hasattr(view, "query"):
    if "전체" not in status_filter and status_filter:
        view = view[view["status"].isin(status_filter)]
    if keyword_search:
        view = view[view["keyword"].str.contains(keyword_search, case=False, na=False)]
    if seed_search:
        view = view[view["seed_keyword"].str.contains(seed_search, case=False, na=False)]
    sort_map = {
        "점수(높은순)": ("score", False), "점수(낮은순)": ("score", True),
        "검색량(높은순)": ("search_volume", False), "최근 평가일순": ("updated_at", False),
    }
    col, asc = sort_map[sort_by]
    view = view.sort_values(by=col, ascending=asc)

st.markdown(f"##### 키워드 목록 ({len(view)} / {total} 건)")
display = view.copy()
display.insert(0, "선택", False)
RENAME = {
    "keyword_id": "ID", "seed_keyword": "주제", "keyword": "키워드",
    "score": "점수", "search_volume": "월검색량", "competition": "경쟁도",
    "cpc": "CPC(원)", "commercial_intent": "상업의도", "status": "상태",
    "source": "출처", "updated_at": "마지막 평가", "note": "메모",
}
display = display.rename(columns=RENAME)
edited = st.data_editor(
    display, use_container_width=True, hide_index=True,
    disabled=[c for c in display.columns if c != "선택"],
    key="_pool_editor",
    column_config={
        "선택": st.column_config.CheckboxColumn(width="small"),
        "점수": st.column_config.NumberColumn(format="%.1f"),
        "CPC(원)": st.column_config.NumberColumn(format="%.0f"),
    },
)
selected_ids = edited[edited["선택"]]["ID"].tolist() if "ID" in edited.columns else []

b1, b2, b3 = st.columns([1, 1, 4])
with b1:
    delete_clicked = st.button(f"🗑  선택 삭제 ({len(selected_ids)})",
                                disabled=len(selected_ids) == 0, use_container_width=True)
with b2:
    revaluate_clicked = st.button(f"🔄  선택 재평가 ({len(selected_ids)})",
                                   disabled=len(selected_ids) == 0, use_container_width=True)

rs = get_researcher()
if delete_clicked:
    try:
        for kid in selected_ids:
            rs.storage.delete_pool(kid)
        st.success(f"{len(selected_ids)}개 키워드를 삭제했어요.")
        log_activity("success", "풀 관리", f"{len(selected_ids)}개 삭제")
        _refresh()
    except Exception as e:
        log_activity("error", "풀 관리", humanize_error(e), str(e))
        st.error(humanize_error(e))

if revaluate_clicked:
    try:
        with st.spinner("재평가 중…"):
            cnt = 0
            for kid in selected_ids:
                item = rs.storage.get_pool(kid)
                if not item: continue
                rs.check_keyword(item.keyword, seed=item.seed_keyword)
                cnt += 1
        st.success(f"{cnt}개 키워드를 새로 평가했어요.")
        log_activity("success", "풀 관리", f"{cnt}개 재평가")
        _refresh()
    except Exception as e:
        log_activity("error", "풀 관리", humanize_error(e), str(e))
        st.error(humanize_error(e))
