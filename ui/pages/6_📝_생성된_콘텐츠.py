"""⑥ 생성된 콘텐츠 뷰어 — content_db 의 HTML 을 실제로 렌더링.

[ 핵심 정책 ]
  · ‘메모리 변수’ 가 아니라 반드시 storage(content_db) 에서 매번 조회한다 — 즉,
    Streamlit cache 무효화로 항상 최신 데이터를 본다.
  · refined_post 는 텍스트가 아니라 HTML — st.markdown(..., unsafe_allow_html=True) 로
    실제 DOM 으로 렌더링한다. <h1>, <img>, <p> 등이 그대로 보이는 일이 없도록.
  · image_urls 는 JSON 직렬화돼 있을 수 있으므로 안전하게 파싱.
  · status 는 내부 코드 그대로 노출하지 않고 ‘생성 완료/검토 중/발행 완료’ 같이 친화 라벨.
  · 자동 새로고침 옵션 — 생성 직후에도 즉시 반영.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import List

import streamlit as st

from services import (
    delete_content_record,
    get_content_dataframe,
    get_researcher,
    humanize_error,
    log_activity,
    retry_content_record,
    settings_snapshot,
)
from osmu_kr.models import ContentRecord

# ── 페이지 설정 ─────────────────────────────────────
st.set_page_config(
    page_title="생성된 콘텐츠",
    page_icon="📝",
    layout="wide",
)
st.title("📝 ⑥ 생성된 콘텐츠")
st.caption("content_db 에 저장된 글을 실제 HTML 그대로 미리보기합니다. 이미지·태그·구조 모두 그대로.")


# ── 친화 status 매핑 ────────────────────────────────
STATUS_LABEL = {
    "generated": "🟢 생성 완료",
    "대기중": "⏳ 작성 대기",
    "생성중": "✏️ 생성 중",
    "검토중": "👀 검토 중",
    "승인완료": "✅ 승인 완료",
    "발행완료": "🚀 발행 완료",
    "발행차단": "🛑 발행 차단",
    "실패": "❌ 실패",
}


def humanize_status(status: str) -> str:
    return STATUS_LABEL.get(status, f"• {status or '미정'}")


def parse_image_urls(raw: str) -> List[dict]:
    """image_urls 필드 파싱 — JSON 또는 콤마 구분 문자열 둘 다 지원."""
    if not raw:
        return []
    raw = raw.strip()
    # JSON 시도
    if raw.startswith("[") or raw.startswith("{"):
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [d if isinstance(d, dict) else {"url": str(d), "filename": "", "alt": ""} for d in data]
            if isinstance(data, dict):
                return [data]
        except Exception:
            pass
    # 콤마 구분 문자열 폴백 (구버전 데이터 호환)
    return [{"url": u.strip(), "filename": "", "alt": ""} for u in raw.split(",") if u.strip()]


def parse_sources(raw: str) -> List[str]:
    if not raw:
        return []
    return [u.strip() for u in raw.split(",") if u.strip()]


def parse_created_at(s: str) -> datetime:
    if not s:
        return datetime.min
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        try:
            return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return datetime.min


# ── 사이드바: 수동 새로고침만 ────────────────────────
# 자동 새로고침은 본문 미리보기 expander 가 강제로 접히는 문제가 있어 제거.
# 새 콘텐츠 확인은 아래 ‘지금 새로고침’ 버튼으로 직접 트리거.
with st.sidebar:
    st.subheader("📝 콘텐츠 보기 옵션")
    if st.button("🔄  지금 새로고침", use_container_width=True,
                  help="content_db 에서 최신 데이터를 다시 불러옵니다."):
        st.cache_data.clear()
        st.cache_resource.clear()
        st.rerun()
    st.caption(
        "💡 새 콘텐츠가 생성되면 위 버튼을 눌러 갱신하세요. "
        "본문을 읽는 도중 화면이 자동으로 접히지 않도록 자동 새로고침은 비활성 상태입니다."
    )


# ── 데이터 조회 — 항상 storage 에서 직접 (cache TTL=3초) ────
@st.cache_data(ttl=3, show_spinner=False)
def _load_content_records() -> List[dict]:
    """content_db 의 모든 ContentRecord 를 dict 리스트로 반환 (created_at DESC)."""
    rs = get_researcher()
    records: List[ContentRecord] = rs.storage.list_content()
    rows = []
    for r in records:
        rows.append({
            "id": r.id,
            "keyword": r.keyword,
            "seed_keyword": r.seed_keyword,
            "title_final": r.title_final,
            "status": r.status,
            "created_at": r.created_at,
            "platform_url": r.platform_url,
            "refined_post": r.refined_post or "",
            "image_urls_raw": r.image_urls or "",
            "original_source": r.original_source or "",
            "error_log": r.error_log or "",
            "note": r.note or "",
        })
    rows.sort(key=lambda x: parse_created_at(x["created_at"]), reverse=True)
    return rows


records = _load_content_records()

# ── 상단 메트릭 ───────────────────────────────────
total = len(records)
status_counts: dict[str, int] = {}
for r in records:
    status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1

c1, c2, c3, c4 = st.columns(4)
c1.metric("총 글 수", f"{total} 건")
c2.metric("생성 완료", f"{status_counts.get('generated', 0)} 건")
c3.metric("발행 완료", f"{status_counts.get('발행완료', 0)} 건")
c4.metric("실패", f"{status_counts.get('실패', 0)} 건")

st.divider()

# ── 필터 ─────────────────────────────────────────
if not records:
    st.info(
        "아직 생성된 콘텐츠가 없어요. ‘① 키워드 생성’ 또는 ‘③ 추천 및 선택’ 화면에서 "
        "키워드를 만들고, CLI `osmu-kr generate --keyword \"...\"` 또는 GUI 에서 글 작성을 시작해보세요."
    )
    st.stop()

f1, f2 = st.columns([2, 4])
with f1:
    statuses = sorted({r["status"] for r in records})
    status_filter = st.multiselect(
        "상태 필터",
        options=statuses,
        default=statuses,
        format_func=humanize_status,
    )
with f2:
    keyword_query = st.text_input(
        "키워드/제목 검색",
        placeholder="예: 다이어트, AI ETF",
    )

filtered = [
    r for r in records
    if r["status"] in status_filter
    and (not keyword_query
         or keyword_query.lower() in r["keyword"].lower()
         or keyword_query.lower() in (r["title_final"] or "").lower())
]

st.markdown(f"##### 콘텐츠 목록 ({len(filtered)} / {total} 건) — 최신순")

# ── 카드 형태 렌더링 ────────────────────────────────
for r in filtered:
    with st.container(border=True):
        # 헤더 — 키워드 / 상태 / 메타
        head_l, head_r = st.columns([5, 2])
        with head_l:
            title = r["title_final"] or r["keyword"]
            st.markdown(f"### {title}")
            meta_bits = [f"**키워드**: {r['keyword']}"]
            if r["seed_keyword"]:
                meta_bits.append(f"**주제(seed)**: {r['seed_keyword']}")
            if r["created_at"]:
                meta_bits.append(f"**생성일시**: {r['created_at'][:19]}")
            st.caption(" · ".join(meta_bits))
        with head_r:
            st.markdown(
                f"<div style='text-align:right; padding-top:8px;'>"
                f"<span style='font-size:14px; padding:6px 12px; "
                f"background:#f5f7ff; border-radius:14px;'>"
                f"{humanize_status(r['status'])}</span><br/>"
                f"<small style='color:#888;'>id={r['id']}</small></div>",
                unsafe_allow_html=True,
            )

        sources = parse_sources(r["original_source"])
        images = parse_image_urls(r["image_urls_raw"])

        # 본문 미리보기 + 펼치기
        if r["refined_post"]:
            preview_label = (
                f"📖 본문 미리보기 ({len(r['refined_post']):,}자, "
                f"이미지 {len(images)}장, 출처 {len(sources)}개)"
            )
            with st.expander(preview_label, expanded=False):
                # ★ 핵심: HTML 을 실제 DOM 으로 렌더링
                st.markdown(r["refined_post"], unsafe_allow_html=True)

                # 이미지가 본문에 안 들어갔거나 따로 보고싶을 때 — 별도 갤러리
                if images:
                    st.markdown("---")
                    st.markdown("**🖼 사용된 이미지**")
                    cols = st.columns(min(3, len(images)))
                    for idx, img in enumerate(images):
                        url = img.get("url") or ""
                        fname = img.get("filename") or f"image-{idx + 1}"
                        alt = img.get("alt") or fname
                        if url:
                            with cols[idx % len(cols)]:
                                st.image(url, caption=f"{fname}\n({alt})",
                                          use_container_width=True)

                # 출처
                if sources:
                    st.markdown("---")
                    st.markdown("**🔗 출처 (original_source)**")
                    for s in sources:
                        st.markdown(f"- [{s}]({s})")

                # 발행 URL
                if r["platform_url"]:
                    st.markdown(f"**🚀 발행 URL**: [{r['platform_url']}]({r['platform_url']})")

                # 에러 / 메모
                if r["error_log"]:
                    st.warning(f"⚠️ error_log: {r['error_log']}")
                if r["note"]:
                    st.caption(f"메모: {r['note']}")
        else:
            st.warning(
                "이 콘텐츠는 본문(refined_post) 이 비어 있어요. "
                "콘텐츠 생성이 실패했거나 작성 대기 상태일 수 있습니다."
            )
            if r["error_log"]:
                st.error(f"error_log: {r['error_log']}")

        # ── 하단 액션 (재시도 / 다운로드 / 본문 / 삭제) ──
        ba1, ba2, ba3, ba4, ba5 = st.columns([1.1, 1, 1, 1, 2])

        # ── 재시도 (대기중/실패는 즉시 / generated 는 1단계 확인) ──
        retry_confirm_key = f"_confirm_retry_{r['id']}"
        retry_confirming = st.session_state.get(retry_confirm_key, False)
        needs_confirm_retry = r["status"] == "generated"

        with ba1:
            if not retry_confirming:
                btn_label = "🔁  재시도"
                if st.button(btn_label, key=f"retry_{r['id']}",
                              use_container_width=True,
                              help="이 키워드로 글을 다시 생성합니다 (같은 글 ID 유지)."):
                    if needs_confirm_retry:
                        st.session_state[retry_confirm_key] = True
                        st.rerun()
                    else:
                        # 대기중/실패 — 즉시 실행
                        with st.spinner(f"id={r['id']} 재생성 중…"):
                            res = retry_content_record(r["id"])
                        if res.get("ok"):
                            st.toast(f"id={r['id']} 재생성 완료 ({res['html_len']}자)",
                                      icon="✨")
                            log_activity("success", "콘텐츠 재시도",
                                          f"id={r['id']} keyword='{r['keyword']}' "
                                          f"→ {res['html_len']}자",
                                          res.get("error_log") or "")
                            st.cache_data.clear()
                            st.rerun()
                        else:
                            st.error(f"재시도 실패: {res.get('reason')}")
                            log_activity("error", "콘텐츠 재시도",
                                          str(res.get("reason")), str(res))
            else:
                if st.button("✅  재생성 확정", key=f"retry_yes_{r['id']}",
                              type="primary", use_container_width=True):
                    with st.spinner(f"id={r['id']} 재생성 중…"):
                        res = retry_content_record(r["id"])
                    st.session_state.pop(retry_confirm_key, None)
                    if res.get("ok"):
                        st.toast(f"id={r['id']} 재생성 완료 ({res['html_len']}자)",
                                  icon="✨")
                        log_activity("success", "콘텐츠 재시도",
                                      f"id={r['id']} keyword='{r['keyword']}' "
                                      f"→ {res['html_len']}자",
                                      res.get("error_log") or "")
                        st.cache_data.clear()
                        st.rerun()
                    else:
                        st.error(f"재시도 실패: {res.get('reason')}")

        with ba2:
            if r["refined_post"]:
                st.download_button(
                    "💾  HTML",
                    data=r["refined_post"],
                    file_name=f"{r['id']}_{r['keyword'][:30]}.html".replace("/", "-"),
                    mime="text/html",
                    key=f"dl_{r['id']}",
                    use_container_width=True,
                )
        with ba3:
            if st.button("📋  본문 보기", key=f"raw_{r['id']}",
                          use_container_width=True):
                st.code(r["refined_post"][:2000] + ("…" if len(r["refined_post"]) > 2000 else ""),
                          language="html")

        # ── 삭제 (2단계 확인) ──
        confirm_key = f"_confirm_delete_{r['id']}"
        is_confirming = st.session_state.get(confirm_key, False)

        with ba4:
            if not is_confirming:
                if st.button("🗑  삭제", key=f"del_{r['id']}",
                              use_container_width=True,
                              help="이 콘텐츠를 content_db 에서 영구 삭제합니다."):
                    st.session_state[confirm_key] = True
                    st.rerun()
            else:
                if st.button("✅  삭제 확정", key=f"del_yes_{r['id']}",
                              type="primary", use_container_width=True):
                    try:
                        ok = delete_content_record(r["id"])
                        if ok:
                            st.toast(f"id={r['id']} 삭제 완료", icon="🗑")
                            log_activity("success", "콘텐츠 삭제",
                                          f"id={r['id']} keyword='{r['keyword']}'")
                            st.session_state.pop(confirm_key, None)
                            st.cache_data.clear()
                            st.rerun()
                        else:
                            st.error(f"id={r['id']} 를 찾지 못했어요.")
                            st.session_state.pop(confirm_key, None)
                    except Exception as e:
                        st.error(humanize_error(e))
                        log_activity("error", "콘텐츠 삭제", humanize_error(e), str(e))
                        st.session_state.pop(confirm_key, None)
        with ba5:
            if is_confirming:
                if st.button("↩️  삭제 취소", key=f"del_no_{r['id']}",
                              use_container_width=True):
                    st.session_state.pop(confirm_key, None)
                    st.rerun()
            elif retry_confirming:
                if st.button("↩️  재시도 취소", key=f"retry_no_{r['id']}",
                              use_container_width=True):
                    st.session_state.pop(retry_confirm_key, None)
                    st.rerun()
            else:
                if r["status"] == "대기중":
                    st.caption("⏳ ‘🔁 재시도’ 로 글 작성 시작")
                elif r["status"] == "실패":
                    st.caption("❌ ‘🔁 재시도’ 로 다시 시도")
                elif r["status"] == "generated":
                    st.caption("✅ Slack 검토 → 발행")

        if is_confirming:
            st.warning(
                f"⚠️ **id={r['id']}** ({r['keyword']}) 콘텐츠를 정말 삭제하시겠어요? "
                "이 작업은 되돌릴 수 없습니다.",
                icon="⚠️",
            )
        if retry_confirming:
            st.warning(
                f"🔁 **id={r['id']}** ({r['keyword']}) 콘텐츠를 다시 생성합니다. "
                "현재 본문(HTML)·이미지가 새로 생성된 결과로 **덮어써집니다**.",
                icon="🔁",
            )

# ── 푸터 — 디버그 정보 ──────────────────────────────
st.divider()
with st.expander("🔧 디버그 — 데이터 흐름 확인"):
    snap = settings_snapshot()
    st.json({
        "storage_backend": snap["storage_backend"],
        "local_format": snap["local_format"],
        "data_dir": snap["data_dir"],
        "records_in_db": total,
        "filtered_shown": len(filtered),
        "cache_ttl_sec": 3,
        "auto_refresh": "disabled (수동 버튼만)",
    })
    st.caption(
        "이 화면의 데이터는 storage(content_db) 에서 직접 읽습니다 (TTL=3초). "
        "사이드바의 ‘🔄 지금 새로고침’ 버튼을 누르면 즉시 다시 불러옵니다."
    )
