# -*- coding: utf-8 -*-
"""
kb 命令行入口 —— 离线数据工程的全部操作

运行方式（使用 rag 的虚拟环境，与在线服务共用依赖）:

    rag\\.venv\\Scripts\\python -m kb <子命令>

子命令:
    status                查看当前发布版本与语料变更概况
    scan                  只读扫描语料，输出与当前 manifest 的差异
    seed [--force]        把现有 v1 三件套登记为基线（只读 + 新建登记文件）
    build [--full]        构建新版本（默认增量，基线=当前发布版）
          [--base ID]       指定基线版本
    verify <build_id>     执行一致性验证门禁
    eval <build_id>       执行 golden 集评估
    promote <build_id>    发布（--force 跳过软门禁，硬门禁不可跳）
    rollback              回滚到上一发布版本
    gc [--keep N] [--apply]  清理旧构建目录（默认只列清单）

所有命令对现有 rag/ 三件套与运行时数据库均零写入。
"""

import argparse
import logging
import os
import sys

# 保证从任意工作目录运行都能导入 rag / kb 包
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

logger = logging.getLogger(__name__)


def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def cmd_status(args):
    from .release import current_build_id, resolve_index_dir, history_build_ids
    build_id = current_build_id()
    print("当前发布版本: %s" % (build_id or "（无，尚未登记）"))
    if build_id:
        index_dir, _ = resolve_index_dir()
        print("  索引目录   : %s" % (index_dir or "（解析失败）"))
        print("  历史版本   : %s" % " -> ".join(history_build_ids()))

    from .manifest import diff_manifests, load_manifest, scan_sources
    from .paths import WW_DIR
    if os.path.isdir(WW_DIR):
        current_files = load_manifest(build_id) if build_id else {}
        new_files = scan_sources(WW_DIR)
        if build_id:
            diff = diff_manifests(current_files, new_files)
            print("语料变更（相对 %s）:" % build_id)
            print("  新增 %d / 删除 %d / 修改 %d / 未变 %d"
                  % (len(diff["added"]), len(diff["removed"]),
                     len(diff["changed"]), len(diff["unchanged"])))
            for f in diff["added"][:10]:
                print("    + %s" % f)
            for f in diff["changed"][:10]:
                print("    ~ %s" % f)
            for f in diff["removed"][:10]:
                print("    - %s" % f)
        else:
            print("语料文件: %d 个（尚未登记基线）" % len(new_files))


def cmd_scan(args):
    from .manifest import scan_sources
    files = scan_sources()
    print("扫描语料: %d 个文件" % len(files))
    for name in sorted(files):
        print("  %s  %s" % (files[name]["sha256"][:12], name))


def cmd_seed(args):
    from .seed import seed
    result = seed(force=args.force)
    if result.get("ok"):
        print("登记完成: %s（文件 %s 个，现有文档 %s 条）"
              % (result["build_id"], result.get("files"),
                 result.get("documents")))


def cmd_build(args):
    from .builder import IndexBuilder
    from .verify import verify_build
    builder = IndexBuilder(full=args.full, base_build_id=args.base)
    meta = builder.build()
    build_id = meta["build_id"]
    print("构建完成: %s" % build_id)
    print("  chunk: 复制 %d + 新增 %d，嵌入 %d（缓存命中 %d），失败 %d"
          % (meta["counts"]["copied"], meta["counts"]["new_chunks"],
             meta["counts"]["embedded"],
             meta["counts"]["embed_cache_hits"],
             meta["counts"]["failed"]))
    if not args.skip_verify:
        result = verify_build(build_id)
        print("一致性验证: %s" % ("通过" if result["ok"] else "不通过"))
        if not result["ok"]:
            print("  %s" % result["checks"])
    print("提示: 验证通过后执行  kb promote %s  发布；"
          "发布后在线服务会热切换到新版本。" % build_id)


def cmd_verify(args):
    from .verify import verify_build
    result = verify_build(args.build_id)
    print("一致性验证: %s" % ("通过" if result["ok"] else "不通过"))
    for name, detail in result["checks"].items():
        print("  %-16s %s" % (name, detail))
    return 0 if result["ok"] else 1


def cmd_eval(args):
    from .builder import _load_build_json
    from .eval import evaluate
    meta = _load_build_json(args.build_id)
    if meta is None:
        print("版本不存在: %s" % args.build_id)
        return 1
    result = evaluate(args.build_id, meta["index_dir"])
    print("评估结果: %s" % result)
    return 0


def cmd_eval_gen(args):
    from .builder import _load_build_json
    from .eval import evaluate_generation
    meta = _load_build_json(args.build_id)
    if meta is None:
        print("版本不存在: %s" % args.build_id)
        return 1
    print("生成质量评估(消耗 API 额度,耗时较长)...")
    result = evaluate_generation(
        args.build_id, meta["index_dir"], judge_model=args.judge_model)
    print("覆盖率: %s" % result.get("coverage"))
    print("judge 均分: %s" % result.get("judge"))
    for p in result.get("per_question", []):
        print("  - %s (字数 %s, 引用行 %s, judge %s)"
              % (p.get("question", "?")[:30],
                 p.get("answer_len"), p.get("cite_lines"),
                 p.get("judge", {}).get("faithfulness")))
    return 0


def cmd_promote(args):
    from .release import promote
    result = promote(args.build_id, force=args.force)
    if result.get("ok"):
        print("已发布: %s（在线服务将热切换）" % result["current"])
        return 0
    print("发布被拒绝: %s" % result.get("reason"))
    return 1


def cmd_rollback(args):
    from .release import rollback
    result = rollback()
    if result.get("ok"):
        print("已回滚到: %s（在线服务将热切换）" % result["current"])
        return 0
    print("回滚失败: %s" % result.get("reason"))
    return 1


def cmd_gc(args):
    from .release import gc
    result = gc(keep=args.keep, dry_run=not args.apply)
    print("清理%s:" % ("预览" if result["dry_run"] else "完成"))
    print("  将删除: %s" % (result["removed"] or "（无）"))
    print("  保留  : %s" % result["kept"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kb",
        description="MarxLen 离线数据工程与知识库活水更新工具")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="查看当前发布版本与语料变更")
    sub.add_parser("scan", help="只读扫描语料")

    p = sub.add_parser("seed", help="把现有 v1 索引登记为基线")
    p.add_argument("--force", action="store_true", help="已登记时强制重建登记")

    p = sub.add_parser("build", help="构建新版本（默认增量）")
    p.add_argument("--full", action="store_true", help="全量构建")
    p.add_argument("--base", help="指定增量基线版本（默认当前发布版）")
    p.add_argument("--skip-verify", action="store_true",
                   help="构建后跳过一致性验证")

    p = sub.add_parser("verify", help="一致性验证门禁")
    p.add_argument("build_id")

    p = sub.add_parser("eval", help="golden 集评估")
    p.add_argument("build_id")

    p = sub.add_parser("eval-gen", help="生成质量评估(引用覆盖率 + LLM 评分,需 API)")
    p.add_argument("build_id")
    p.add_argument("--judge-model", default=None,
                   help="评审模型 ID(默认跟随对话模型)")

    p = sub.add_parser("promote", help="发布版本")
    p.add_argument("build_id")
    p.add_argument("--force", action="store_true", help="跳过评估对比软门禁")

    sub.add_parser("rollback", help="回滚到上一版本")

    p = sub.add_parser("gc", help="清理旧构建目录")
    p.add_argument("--keep", type=int, default=3, help="保留版本数")
    p.add_argument("--apply", action="store_true", help="实际删除（默认只预览）")

    return parser


def main(argv=None) -> int:
    _setup_logging()
    parser = build_parser()
    args = parser.parse_args(argv)

    handlers = {
        "status": cmd_status,
        "scan": cmd_scan,
        "seed": cmd_seed,
        "build": cmd_build,
        "verify": cmd_verify,
        "eval": cmd_eval,
        "eval-gen": cmd_eval_gen,
        "promote": cmd_promote,
        "rollback": cmd_rollback,
        "gc": cmd_gc,
    }
    handler = handlers[args.command]
    result = handler(args)
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    sys.exit(main())
