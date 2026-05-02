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
    sub.add_parser("config")

    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    cfg = Config()
    rs = KeywordResearcher(cfg)

    if args.cmd == "config":
        print(cfg.summary()); return 0
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
    if args.cmd == "show":
        print(cfg.summary())
        for it in rs.storage.list_pool():
            print(f"  - {it.keyword_id} {it.keyword} score={it.score}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
