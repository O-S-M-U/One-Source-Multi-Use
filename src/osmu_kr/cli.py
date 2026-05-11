"""CLI."""
from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import Config
from .researcher import KeywordResearcher


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="osmu-kr")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s_seed = sub.add_parser("seed")
    s_seed.add_argument("--seed", required=True)
    s_seed.add_argument("--limit", type=int, default=10)

    s_check = sub.add_parser("check")
    s_check.add_argument("--keyword", required=True)
    s_check.add_argument("--seed", default=None)

    s_rec = sub.add_parser("recommend")
    s_rec.add_argument("--top", type=int, default=5)

    s_sel = sub.add_parser("select")
    s_sel.add_argument("--id", required=True)
    s_sel.add_argument("--title", default="")
    s_sel.add_argument("--source", default="")

    sub.add_parser("prune")
    sub.add_parser("manage")    # 정기 점검 — cron 등 자동화용
    sub.add_parser("history")   # 분석 이력 조회
    sub.add_parser("show")
    sub.add_parser("config")        # legacy: cfg.summary() 출력 (호환)

    # ── v13: config 테이블 운영 명령 ──
    s_cfg_get = sub.add_parser("config-get",
                                 help="config 값 조회 (env > db > default)")
    s_cfg_get.add_argument("--key", required=True,
                              help="dot notation 키 (예: keyword.golden_threshold)")

    s_cfg_set = sub.add_parser("config-set",
                                 help="config DB 값 설정 (환경변수가 있으면 우선)")
    s_cfg_set.add_argument("--key", required=True)
    s_cfg_set.add_argument("--value", required=True)

    sub.add_parser("config-list",
                     help="모든 config 항목 + 출처(env/db/default) 표시")

    s_cfg_install = sub.add_parser("config-install-defaults",
                                      help="DEFAULTS 19개 항목을 DB 에 부트스트랩")
    s_cfg_install.add_argument("--overwrite", action="store_true")

    # ── v13-E: housekeeping 수동 트리거 ──
    sub.add_parser("housekeeping",
                     help="revival 재평가 + 풀삭제 정책 즉시 실행")

    # ── v13-F: accounts CRUD ──
    s_acc_add = sub.add_parser("account-add",
                                 help="Tistory 등 발행 계정 등록")
    s_acc_add.add_argument("--id", required=True)
    s_acc_add.add_argument("--blog-id", required=True)
    s_acc_add.add_argument("--platform", default="tistory")
    s_acc_add.add_argument("--login-id", default="")
    s_acc_add.add_argument("--cookie-path", default="")
    s_acc_add.add_argument("--note", default="")

    sub.add_parser("account-list", help="등록된 계정 목록")

    # ── 신규: 콘텐츠 생성 ──
    s_gen = sub.add_parser("generate", help="키워드 → Firecrawl 검색 → SEO HTML 생성")
    s_gen.add_argument("--keyword", required=True)
    s_gen.add_argument("--title", default="", help="(선택) 최종 제목")
    s_gen.add_argument("--no-save", action="store_true", help="content_db 저장 생략 (드라이런)")
    s_gen.add_argument("--no-fallback", action="store_true",
                        help="LLM 실패 시 휴리스틱 폴백 비활성")
    s_gen.add_argument("--require-real-images", action="store_true",
                        help="picsum 폴백 비활성 — Unsplash 이미지만 사용 (운영 권장)")

    s_del = sub.add_parser("delete-content",
                            help="content_db 에서 콘텐츠 1건 삭제 (id 기준)")
    s_del.add_argument("--id", required=True, help="삭제할 ContentRecord.id")
    s_del.add_argument("--yes", action="store_true",
                        help="확인 프롬프트 없이 즉시 삭제 (cron 등 자동화용)")

    s_re = sub.add_parser("regenerate",
                           help="기존 ContentRecord 의 keyword 로 글을 다시 생성 (in-place 갱신)")
    s_re.add_argument("--id", required=True, help="재생성할 ContentRecord.id")
    s_re.add_argument("--require-real-images", action="store_true",
                       help="picsum 폴백 비활성 — Unsplash 이미지만 사용")

    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    cfg = Config()
    rs = KeywordResearcher(cfg)

    if args.cmd == "config":
        print(cfg.summary()); return 0

    # ── v13: config 테이블 ──
    if args.cmd == "config-get":
        v = rs.config_mgr.get(args.key)
        src = rs.config_mgr.get_source(args.key)
        print(f"{args.key} = {v!r} (source={src})")
        return 0
    if args.cmd == "config-set":
        rs.config_mgr.set(args.key, args.value)
        print(f"✅ {args.key} = {args.value!r} (DB)")
        return 0
    if args.cmd == "config-list":
        for entry in rs.config_mgr.dump():
            print(f"  {entry['key']:50s} = {entry['value']!r:>10}  [{entry['source']}]")
        return 0
    if args.cmd == "config-install-defaults":
        n = rs.config_mgr.install_defaults(overwrite=args.overwrite)
        print(f"✅ DB 에 {n} 개 항목 적재"
              + (" (overwrite=True)" if args.overwrite else ""))
        return 0

    # ── v13-E: housekeeping ──
    if args.cmd == "housekeeping":
        from .researcher.housekeeping import Housekeeping
        hk = Housekeeping(rs.storage, evaluator=rs.evaluator,
                           config_mgr=rs.config_mgr)
        report = hk.run()
        print("=" * 60)
        print("  🧹 housekeeping 실행 결과")
        print("=" * 60)
        print(f"  {report.summary()}")
        return 0

    # ── v13-F: accounts ──
    if args.cmd == "account-add":
        from .models import Account
        rs.storage.upsert_account(Account(
            id=args.id, platform=args.platform, blog_id=args.blog_id,
            login_id=args.login_id, cookie_path=args.cookie_path,
            note=args.note,
        ))
        print(f"✅ 계정 등록: {args.id} ({args.platform}/{args.blog_id})")
        return 0
    if args.cmd == "account-list":
        accounts = rs.storage.list_accounts()
        if not accounts:
            print("(등록된 계정 없음)")
            return 0
        for a in accounts:
            mark = "✅" if a.is_active else "⏸"
            print(f"  {mark} {a.id} | {a.platform}/{a.blog_id} | "
                  f"login={a.login_id or '-'} | cookie={a.cookie_path or '-'}")
        return 0
    if args.cmd == "seed":
        rep = rs.run_seed(args.seed, expand_limit=args.limit)
        print(rep.summary()); return 0
    if args.cmd == "check":
        item = rs.check_keyword(args.keyword, seed=args.seed)
        print(json.dumps({"keyword_id": item.keyword_id, "keyword": item.keyword,
                          "score": item.score, "status": item.status},
                         ensure_ascii=False, indent=2)); return 0
    if args.cmd == "recommend":
        items = rs.recommend(top_n=args.top)
        for it in items:
            print(f"  {it.keyword_id} | {it.keyword} | {it.score} | seed={it.seed_keyword}")
        return 0
    if args.cmd == "select":
        rec = rs.select_for_content(args.id, title_final=args.title, original_source=args.source)
        print(f"created: id={rec.id} keyword='{rec.keyword}'"); return 0
    if args.cmd == "prune":
        pool, report = rs.prune()
        print(report.summary()); return 0
    if args.cmd == "manage":
        report = rs.manage()
        print("=" * 60)
        print("  🔧 키워드 풀 정기 점검 결과")
        print("=" * 60)
        print(f"  풀 크기: {report.pool_size_before} → {report.pool_size_after}")
        print(f"  활성(active) 황금 키워드: {report.active_count}")
        print(f"  {report.prune.summary()}")
        if report.top_recommendations:
            print("\n  현재 추천 TOP 5:")
            for i, it in enumerate(report.top_recommendations, 1):
                print(f"    {i}위 [{it.keyword}] {it.grade or '-'} {it.score:.0f}점")
        print("=" * 60)
        return 0
    if args.cmd == "history":
        history = rs.storage.list_history()
        print(f"\n[research_history] {len(history)} 건")
        for h in history[-30:]:
            mark = "🥇" if h.grade == "황금" else ("🥈" if h.grade == "좋은" else "  ")
            print(f"  {mark} {h.created_at[:16]} | {h.keyword[:30]:<30} "
                  f"{h.grade or '-':<3} {h.total_score:>5.1f}점 "
                  f"profile={h.profile}")
        return 0
    if args.cmd == "delete-content":
        target_id = args.id.strip()
        # 미리 정보 보여주기
        rec = next((r for r in rs.storage.list_content() if r.id == target_id), None)
        if not rec:
            print(f"❌ id={target_id} 콘텐츠를 찾지 못했어요.")
            return 1
        print("=" * 60)
        print(f"  🗑  삭제 대상")
        print("=" * 60)
        print(f"  id        : {rec.id}")
        print(f"  keyword   : {rec.keyword}")
        print(f"  title     : {rec.title_final or '(없음)'}")
        print(f"  status    : {rec.status}")
        print(f"  created_at: {rec.created_at}")
        print(f"  HTML 길이 : {len(rec.refined_post or '')} 자")
        print("=" * 60)
        if not args.yes:
            try:
                ans = input("정말 삭제할까요? [y/N]: ").strip().lower()
            except EOFError:
                ans = ""
            if ans not in ("y", "yes"):
                print("취소됨.")
                return 0
        ok = rs.storage.delete_content(target_id)
        if ok:
            print(f"✅  id={target_id} 삭제 완료")
            return 0
        print(f"❌  삭제 실패 (id 가 풀에 없거나 백엔드 오류)")
        return 1
    if args.cmd == "regenerate":
        from .content_generator import Generator
        from .content_generator.generator import GeneratorConfig
        target_id = args.id.strip()
        rec = next((r for r in rs.storage.list_content() if r.id == target_id), None)
        if not rec:
            print(f"❌ id={target_id} 콘텐츠를 찾지 못했어요.")
            return 1
        print("=" * 60)
        print(f"  🔁  재생성 대상")
        print("=" * 60)
        print(f"  id        : {rec.id}")
        print(f"  keyword   : {rec.keyword}")
        print(f"  status    : {rec.status}")
        print(f"  HTML 길이 : {len(rec.refined_post or '')} 자  → 새 결과로 덮어씁니다")
        print("=" * 60)
        gen = Generator(
            cfg=cfg, storage=rs.storage,
            config=GeneratorConfig(require_real_images=args.require_real_images),
        )
        try:
            result = gen.retry_record(target_id)
        except Exception as e:
            print(f"❌  재생성 실패: {e}")
            return 1
        print(f"✅  id={result.record_id} 재생성 완료")
        print(f"    {result.to_summary()}")
        if result.error_log:
            print(f"    로그: {result.error_log}")
        return 0
    if args.cmd == "generate":
        from .content_generator import Generator
        from .content_generator.generator import GeneratorConfig
        gen = Generator(
            cfg=cfg,
            config=GeneratorConfig(
                fallback_to_heuristic=not args.no_fallback,
                require_real_images=args.require_real_images,
            ),
        )
        result = gen.generate(args.keyword, save=not args.no_save,
                                title_final=args.title)
        print("=" * 60)
        print("  ✍️  콘텐츠 생성 결과")
        print("=" * 60)
        print(f"  {result.to_summary()}")
        if result.error_log:
            print(f"  ⚠️  로그: {result.error_log}")
        if result.original_source:
            print(f"  출처: {len(result.original_source)}개")
            for i, u in enumerate(result.original_source, 1):
                print(f"    {i}. {u}")
        if result.image_urls:
            print(f"  이미지: {len(result.image_urls)}개")
        print(f"\n  HTML 길이: {len(result.refined_post)}자")
        print(f"  HTML 미리보기 (앞 200자):\n  {result.refined_post[:200]}...")
        if result.record_id:
            print(f"\n  ✅ content_db 저장됨 — id={result.record_id}")
        return 0
    if args.cmd == "show":
        print(cfg.summary())
        for it in rs.storage.list_pool():
            print(f"  - {it.keyword_id} {it.keyword} score={it.score}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
