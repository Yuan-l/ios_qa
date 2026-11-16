#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行入口。

    python3 -m ios_qa build [--full] [--check]
    python3 -m ios_qa info
    python3 -m ios_qa find "首页 基建"
    python3 -m ios_qa symbol TBMonitor
    python3 -m ios_qa trace TDMonitor
    python3 -m ios_qa methods "网络 采样"
    python3 -m ios_qa callers TBRequestManager
    python3 -m ios_qa impact TBRequestManager --depth 2
    python3 -m ios_qa read <file> --focus foo
    python3 -m ios_qa pages
    python3 -m ios_qa module TDMonitorKit
    python3 -m ios_qa path TBFinanceDetailViewController
    python3 -m ios_qa notifications --unbalanced
    python3 -m ios_qa similar QWeatherBaseButton.initWithFrame:
    python3 -m ios_qa duplicates --min-lines 8
    python3 -m ios_qa vectors
    python3 -m ios_qa semantic "行情页面怎么加载数据"
    python3 -m ios_qa qa save "问题" "答案"
    python3 -m ios_qa qa search "问题"
    python3 -m ios_qa eval
    python3 -m ios_qa mcp
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import SCHEMA_VERSION
from .build import build
from .config import ProjectProfile
from .eval import resolve_cases_path, run_eval
from .query import QueryService


def _common_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    # 默认值用 SUPPRESS：子命令解析器也继承了这些参数，若带默认值会把
    # 写在子命令之前的 ``--root/--db`` 覆盖回 None（argparse 的已知行为）。
    # 真正的默认值在 main() 里统一补齐。
    parser.add_argument(
        "--root", default=argparse.SUPPRESS,
        help="项目根目录（默认：环境变量 IOS_QA_ROOT，否则当前目录）",
    )
    parser.add_argument(
        "--db", default=argparse.SUPPRESS,
        help="SQLite 路径（默认 <root>/.ios_qa/ios_qa.db）",
    )
    parser.add_argument(
        "--profile", default=argparse.SUPPRESS,
        help="project.json 路径（默认 <root>/.ios_qa/project.json）",
    )
    parser.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="输出 JSON"
    )
    return parser



def _profile(args) -> ProjectProfile:
    return ProjectProfile.load(profile_path=args.profile, root=args.root)


def _service(args) -> QueryService:
    return QueryService(_profile(args), args.db)


def _dump(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _truncate(text: str, width: int = 120) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def _section(title: str, items: list[str], limit: int = 25) -> None:
    print(f"\n{title} ({len(items)})")
    for line in items[:limit]:
        print(f"  {line}")
    if len(items) > limit:
        print(f"  … 还有 {len(items) - limit} 条")


def _print_eval_report(report: dict, detailed: bool = True) -> None:
    for item in report["cases"]:
        if not detailed and item["ok"]:
            continue
        mark = "⏭" if item.get("skipped") else ("✅" if item["ok"] else "❌")
        reviewed = f"  [复核 {item['last_reviewed']}]" if item["last_reviewed"] else ""
        print(f"{mark} {item['id']}  ({item['type']}){reviewed}")
        if item["reason"]:
            print(f"   {item['reason']}")
        if item.get("skipped"):
            print(f"   - 跳过：{item.get('skip_reason', '')}")
        for check in item["checks"]:
            if not check["ok"] or not item["ok"]:
                print(f"   - {'ok' if check['ok'] else 'FAIL'} {check['kind']}: {check['detail']}")
    summary = f"\n通过 {report['passed']}/{report['total']}，失败 {report['failed']}"
    if report.get("skipped"):
        summary += f"，跳过 {report['skipped']}（需要向量索引）"
    print(summary)


def cmd_build(args) -> int:
    profile = _profile(args)
    report = build(
        profile,
        db_path=args.db,
        full=args.full,
        verbose=not args.quiet and not args.json,
    )

    check = None
    check_skipped = False
    if getattr(args, "check", False):
        # 构建后自检：只跑数据层 golden，不调用任何模型。
        # 只认当前工程的 .ios_qa/eval_cases.json；没有就跳过，不阻塞构建。
        cases_path = resolve_cases_path(profile)
        if cases_path.exists():
            check = run_eval(profile, report.db_path, cases_path)
        else:
            check_skipped = True

    if args.json:
        payload = report.to_dict()
        if check is not None:
            payload["check"] = check
        _dump(payload)
        if report.errors:
            return 1
        return 3 if check and check["failed"] else 0

    print(f"索引完成：{report.mode}  {report.duration_s:.1f}s")
    print(f"  数据库: {report.db_path}")
    print(f"  文件: {report.files_total} 总 / {report.files_parsed} 解析 / "
          f"{report.files_skipped} 跳过 / {report.files_deleted} 删除")
    print(f"  写入: symbols={report.symbols} methods={report.methods} "
          f"classes={report.classes} fingerprints={report.fingerprints}")
    print("  统计: " + "  ".join(f"{k}={v}" for k, v in report.counts.items()))
    if report.errors:
        print(f"\n解析失败 {len(report.errors)} 个：")
        for err in report.errors[:10]:
            print(f"  - {err['file']}: {err['error']}")
        if len(report.errors) > 10:
            print(f"  … 还有 {len(report.errors) - 10} 个")

    if check_skipped:
        print("\n跳过 eval 自检：未找到 eval_cases.json")
    elif check is not None:
        print("\n索引自检（eval）：")
        _print_eval_report(check, detailed=False)

    if report.errors:
        return 1
    return 3 if check and check["failed"] else 0


def cmd_info(args) -> int:
    with _service(args) as service:
        info = service.info()
    if args.json:
        _dump(info)
        return 0
    print(f"{info['project']}  索引信息")
    print(f"  数据库: {info['db_path']} ({info['db_size_mb']} MB)")
    print(f"  构建时间: {info['build_time']}  (距今 {info['index_age_days']} 天)")
    print(f"  分支: {info['git_branch']} @ {info['git_commit']}  脏文件 {info['git_dirty_files']}")
    print(f"  源码: {info['source_file_count']} 文件 / {info['source_loc']} 行")
    print("  表:", "  ".join(f"{k}={v}" for k, v in info["counts"].items()))
    print(f"  有方法体: {info['methods_with_body']}  "
          f"未配对通知: {info['unbalanced_notifications']}")
    for warning in info["warnings"]:
        print(f"  ⚠️  {warning}")
    return 0


def cmd_hybrid(args) -> int:
    with _service(args) as service:
        data = service.hybrid_find_feature(
            args.query,
            limit=args.limit,
            module=args.module,
            rerank=not args.no_rerank,
        )
    if args.json:
        _dump(data)
        return 0
    for i, item in enumerate(data["results"], 1):
        print(
            f"\n{i}. {item['class_name']}  [{item['module']}]  "
            f"score={item['score']}  sources={','.join(item['sources'])}"
        )
        print(f"   文件: {item['file_path']}")
        method = item.get("vector_method") or {}
        if method.get("method_name"):
            print(f"   语义最相近: {method['method_name']} ({method['score']})")
        methods = [m.get("method_name", "") for m in item.get("methods") or []][:5]
        if methods:
            print(f"   方法: {', '.join(methods)}")
    if not data["results"]:
        print(f"没有找到与「{args.query}」相关的功能。")
    elif not data.get("vector_available"):
        print(f"\n提示: {data.get('hint')}")
    return 0


def cmd_find(args) -> int:
    with _service(args) as service:
        data = service.find_feature(args.query, limit=args.limit)
    if args.json:
        _dump(data)
        return 0
    results = data["results"]
    if not results:
        print(f"没有找到与「{args.query}」相关的功能。")
        return 0
    for i, item in enumerate(results, 1):
        flags = []
        if item["is_view_controller"]:
            flags.append("VC")
        if item["is_app_entry"]:
            flags.append("App入口")
        print(f"\n{i}. {item['class_name']}  [{item['module']}]  "
              f"score={item['score']}  {', '.join(flags)}")
        print(f"   文件: {item['impl_path'] or item['file_path']}")
        print(f"   命中: {item['matched_on']}")
        if item.get("snippet"):
            print(f"   摘要: {_truncate(item['snippet'])}")
        if item.get("doc_comment"):
            print(f"   注释: {_truncate(item['doc_comment'])}")
        if item["methods"]:
            names = ", ".join(m["method_name"] for m in item["methods"][:6])
            print(f"   方法: {names}")
    return 0


def cmd_symbol(args) -> int:
    with _service(args) as service:
        data = service.search_symbol(args.name, limit=args.limit)
    if args.json:
        _dump(data)
        return 0
    if not data["results"]:
        print(f"没有找到符号「{args.name}」。")
        return 0
    for item in data["results"]:
        print(f"\n{item['class_name']}  ({item['kind']}/{item['language']}/{item['module']})")
        if item["superclass"]:
            print(f"  父类: {item['superclass']}")
        if item["protocols"]:
            print(f"  协议: {', '.join(item['protocols'])}")
        print(f"  文件: {item['impl_path'] or item['header_path'] or item['file_path']}")
        if item["doc_comment"]:
            print(f"  注释: {_truncate(item['doc_comment'], 160)}")
        if item["properties"]:
            props = ", ".join(f"{p.get('name')}:{p.get('type')}" for p in item["properties"][:12])
            print(f"  属性: {props}")
        if item["methods"]:
            print(f"  方法 ({len(item['methods'])}):")
            for method in item["methods"]:
                print(f"    {method['signature']}   [{method['file_path']}:{method['line_start']}]")
    return 0


def cmd_trace(args) -> int:
    with _service(args) as service:
        data = service.trace_class(
            args.class_name, limit=args.limit, module=getattr(args, "module", "")
        )
    if args.json:
        _dump(data)
        return 0
    if not data.get("found"):
        print(f"没有找到类「{args.class_name}」。")
        return 0
    print(f"{data['class_name']}  ({data['kind']}/{data['language']}/{data['module']})")
    if data.get("resolved_from"):
        print(f"  由 {data['resolved_from']} 解析而来")
    print(f"  文件: {data['impl_path'] or data['header_path'] or data['file_path']}")
    if data["superclass_chain"]:
        print(f"  继承链: {' -> '.join([data['class_name'], *data['superclass_chain']])}")
    if data["protocols"]:
        print(f"  协议: {', '.join(data['protocols'])}")
    if data["doc_comment"]:
        print(f"  注释: {_truncate(data['doc_comment'], 200)}")
    method_line = f"  方法: {data['method_count']} 个实现  扩展: {data['extension_count']}"
    if data.get("ambiguous"):
        method_line += f"（当前声明点 {data.get('scoped_method_count', 0)} 个）"
    print(method_line)
    if data.get("ambiguous"):
        selected = data.get("selected_module", "")
        print(f"  ⚠ 同名类出现在 {len(data['candidates'])} 个声明点，"
              f"当前只看 {selected}")
        for cand in data["candidates"]:
            mark = "*" if cand["module"] == selected else " "
            print(f"    {mark} {cand['module']:<20} {cand['kind']:<10} "
                  f"{cand['method_count']} 方法  "
                  f"[{cand['file_path']}:{cand['line_start']}]")
        print("    用 --module <模块> 切换；不指定时默认方法最多的那个")

    _section("方法", [
        f"{m['signature']}   [{m['file_path']}:{m['line_start']}-{m['line_end']}]"
        for m in data["methods"]
    ])
    _section("扩展/分类", [
        f"{e['kind']} {e['category_name'] or e['extension_of']}  "
        f"{e['module']}/{e['language']}  [{e['file_path']}:{e['line_start']}]"
        for e in data.get("extensions", [])
    ])
    _section("依赖", [
        f"{d['dep_class']}  ({d['dep_kind']})  [{d['file_path']}:{d['line']}]"
        for d in data["deps"]
    ])
    _section("被依赖", [
        f"{d['from_class']}  ({d['dep_kind']})  [{d['file_path']}:{d['line']}]"
        for d in data["dependents"]
    ])
    _section("API 调用", [
        f"{a['http_method'] or a['api_kind']} {a['url_pattern']}  "
        f"[{a['file_path']}:{a['line_number']}]"
        for a in data["api_calls"]
    ])
    _section("通知", [
        f"{'✅' if n['is_balanced'] == 1 else '⚠️' if n['is_balanced'] == 0 else '❔'} "
        f"{n['notification_name']}  ({n['register_method']}:{n['register_line']})"
        for n in data["notifications"]
    ])
    _section("调用方", [
        f"{c['caller_class']}.{c['caller_method']} -> {c['callee_name']}  "
        f"[{c['file_path']}:{c['line_number']}]"
        for c in data["callers"]
    ])
    _section("调用的符号", [
        f"{c['callee_class']}.{c['callee_name']}  [{c['file_path']}:{c['line_number']}]"
        for c in data["callees"]
    ])
    _section("页面入口", [
        f"{e['kind']} {e['class_name']} {e['identifier']}  [{e['file_path']}]"
        for e in data["entrypoints"]
    ])
    _section("跳转进入", [
        f"{n['from_class']} --{n['kind']}--> {n['to_class']}  [{n['file_path']}]"
        for n in data["navigation_in"]
    ])
    _section("跳转出去", [
        f"{n['from_class']} --{n['kind']}--> {n['to_class']}  [{n['file_path']}]"
        for n in data["navigation_out"]
    ])
    return 0


def cmd_methods(args) -> int:
    with _service(args) as service:
        data = service.search_methods(
            args.query or "", class_name=args.class_name or "",
            module=args.module or "", limit=args.limit,
        )
    if args.json:
        _dump(data)
        return 0
    if not data["results"]:
        print("没有找到方法。")
        return 0
    for item in data["results"]:
        print(f"\n{item['class_name']}.{item['method_name']}  "
              f"[{item['file_path']}:{item['line_start']}-{item['line_end']}]")
        if item["signature"]:
            print(f"  {item['signature']}")
        if item["doc_comment"]:
            print(f"  注释: {_truncate(item['doc_comment'], 160)}")
        if item["body_preview"]:
            print(f"  {_truncate(item['body_preview'], 180)}")
    return 0


def cmd_callers(args) -> int:
    with _service(args) as service:
        data = service.find_callers(args.target, limit=args.limit)
    if args.json:
        _dump(data)
        return 0
    print(f"{data['target']} 的调用方: {data['count']}")
    for c in data["callers"]:
        print(f"  {c['caller_class']}.{c['caller_method']} -> "
              f"{c['callee_class']}.{c['callee_name']}  "
              f"[{c['file_path']}:{c['line']}]  ({c['confidence']})")
    return 0


def cmd_impact(args) -> int:
    with _service(args) as service:
        data = service.impact_analysis(args.target, depth=args.depth, limit=args.limit)
    if args.json:
        _dump(data)
        return 0
    print(f"{data['target']} 的影响面: {data['affected_count']} 个类"
          f"{'（已截断）' if data.get('truncated') else ''}")
    for level in data["levels"]:
        print(f"\ndepth {level['depth']}  ({len(level['items'])})")
        for item in level["items"]:
            flag = " [VC]" if item["is_view_controller"] else ""
            print(f"  - {item['class_name']}{flag}  {item['module']}  "
                  f"via {item['via']}  {_truncate(item['detail'], 80)}")
            if item["file_path"]:
                print(f"      {item['file_path']}")
    return 0


def cmd_read(args) -> int:
    with _service(args) as service:
        data = service.read_source(args.file, focus=args.focus or "", context=args.context)
    if args.json:
        _dump(data)
        return 0
    if not data.get("found"):
        print(data.get("error", "读取失败"))
        return 1
    print(f"[{data['file']}]  lines {data['line_start']}-{data['line_end']} / {data['total_lines']}")
    if data.get("note"):
        print(data["note"])
    print()
    print(data["content"])
    return 0


def cmd_pages(args) -> int:
    with _service(args) as service:
        data = service.list_pages(module=args.module or "", limit=args.limit)
    if args.json:
        _dump(data)
        return 0
    print(f"页面 ({data['count']})")
    for page in data["pages"]:
        print(f"  {page['module']:<20} {page['class_name']:<45} "
              f"{page['vc_base'] or '?':<20} {page['file_path']}")
    return 0


def cmd_module(args) -> int:
    with _service(args) as service:
        data = service.list_module(args.module)
    if args.json:
        _dump(data)
        return 0
    print(f"模块 {data['module']}: {data['classes']} 类 / "
          f"{data['methods']} 方法 / {data['view_controllers']} 个页面")
    for f in data["files"]:
        print(f"  {f['language']}: {f['n']} 文件 / {f['loc'] or 0} 行")
    print("\n方法最多的类:")
    for item in data["top_classes"]:
        print(f"  {item['method_count']:>4}  {item['class_name']:<40} {item['impl_path']}")
    return 0


def cmd_path(args) -> int:
    with _service(args) as service:
        data = service.find_navigation(
            args.class_name,
            depth=args.depth,
            limit=args.limit,
            include_weak=not args.no_weak,
        )
    if args.json:
        _dump(data)
        return 0 if data["found"] else 1
    if not data["found"]:
        print(f"没有找到类「{args.class_name}」。")
        return 1
    print(f"{data['target']}  导航  ({data['module']})")
    if data["entrypoints"]:
        for entry in data["entrypoints"]:
            print(f"  入口: {entry['kind']} {entry['identifier']}  "
                  f"[{entry['file_path']}:{entry['line_number']}]")
    if not data["reachable"]:
        print(f"  ⚠️  {data['note']}")
    for index, path in enumerate(data["paths"], 1):
        print(f"\n  路径 {index}  (入口 {path['root']} / {path['hops']} 跳)")
        for hop in path["chain"]:
            mark = "" if hop["confidence"] == "high" else f"({hop['confidence']})"
            print(f"    {hop['from_class']} --{hop['kind']}{mark}--> {hop['to_class']}")
            print(f"      [{hop['file_path']}:{hop['line_number']}]")
    if data["incoming"]:
        print("\n  直接来源:")
        for edge in data["incoming"]:
            mark = "" if edge["confidence"] == "high" else f" ({edge['confidence']})"
            print(f"    {edge['from_class']}  ({edge['kind']}{mark})  "
                  f"[{edge['file_path']}:{edge['line_number']}]")
    if data["outgoing"]:
        print("\n  去向:")
        for edge in data["outgoing"]:
            print(f"    {edge['to_class']}  ({edge['kind']})  "
                  f"[{edge['file_path']}:{edge['line_number']}]")
    return 0


def cmd_notifications(args) -> int:
    balanced = 0 if args.unbalanced else (-1 if args.unknown else None)
    with _service(args) as service:
        data = service.find_notifications(
            name=args.name or "",
            module=args.module or "",
            balanced=balanced,
            limit=args.limit,
        )
    if args.json:
        _dump(data)
        return 0
    summary = data["summary"]
    print(f"通知: 匹配 {len(data['results'])} 条 / 共 {summary['total']} 条"
          f"（未配对 {summary['unbalanced']}，已配对 {summary['balanced']}，"
          f"无法判定 {summary['unknown']}）")
    for item in data["results"]:
        flag = {0: "未配对", 1: "已配对", -1: "无法判定"}.get(item["is_balanced"], "?")
        print(f"\n  [{flag}] {item['class_name']}  {item['notification_name']}")
        print(f"    注册: {item['register_method'] or '-'}  "
              f"[{item['file_path']}:{item['register_line']}]")
        if item["remove_line"]:
            print(f"    移除: {item['remove_method'] or '-'}  "
                  f"[{item['file_path']}:{item['remove_line']}]")
        else:
            print("    移除: (没有找到同名移除)")
    return 0


def cmd_similar(args) -> int:
    with _service(args) as service:
        data = service.find_similar_code(
            args.target,
            limit=args.limit,
            min_similarity=args.min_similarity,
            mode=args.mode,
        )
    if args.json:
        _dump(data)
        return 0
    if not data.get("found"):
        print(f"没有找到「{args.target}」的相似方法。")
        if data.get("hint"):
            print(f"  {data['hint']}")
        return 0
    if data.get("mode") == "semantic":
        seed = data.get("resolved") or {}
        print(f"{seed.get('class_name')}.{seed.get('method_name')}  "
              f"[语义近邻 / {data.get('model')}]")
        print(f"  {seed.get('file_path')}:{seed.get('line_start')}")
        if not data["results"]:
            print("  没有超过阈值的语义近邻")
            return 0
        print()
        for item in data["results"]:
            advice = item.get("advice") or {}
            print(f"  相似 {item['similarity']:.3f}  "
                  f"{item['class_name']}.{item['method_name']}  ({item['lines']} 行)")
            print(f"    {item['file_path']}:{item['line_start']}  "
                  f"{advice.get('label', '')}")
            for warning in advice.get("warnings") or []:
                print(f"    ⚠ {warning}")
        return 0
    seed = data["resolved"]
    print(f"{seed['class_name']}.{seed['method_name']}  ({seed['lines']} 行)"
          f"  {seed['file_path']}:{seed['line_start']}")
    if seed.get("matched_from"):
        print(f"  由「{seed['matched_from']}」匹配而来")
    print(f"  指纹: {seed['token_count']} token / {seed['shingle_count']} shingle")
    if not data["results"]:
        print(f"  没有相似度 ≥ {data['min_similarity']:.2f} 的方法")
        return 0
    print()
    for item in data["results"]:
        if item["exact"]:
            tag = "精确复制"
        else:
            tag = f"相似 {item['similarity']:.2f}"
        where = "同类" if item["same_class"] else (
            "同模块" if item["same_module"] else item["module"]
        )
        print(f"  {tag}  {item['class_name']}.{item['method_name']}"
              f"  ({item['lines']} 行)  [{where}]")
        print(f"    {item['file_path']}:{item['line_start']}")
    return 0


def cmd_duplicates(args) -> int:
    with _service(args) as service:
        data = service.find_duplicates(
            min_lines=args.min_lines,
            min_similarity=args.min_similarity,
            limit=args.limit,
            class_name=args.class_name or "",
        )
    if args.json:
        _dump(data)
        return 0
    summary = data["summary"]
    print(f"重复代码: {summary['clusters']} 组"
          f"（精确 {summary['exact']} / 近似 {summary['near']}）"
          f"  指纹 {summary['fingerprinted']} 个方法"
          f"  复核 {summary['pairs']} 对")
    if not data["clusters"]:
        print(f"  没有 ≥{data['min_similarity']:.2f} 且 ≥{data['min_lines']} 行的重复")
        return 0
    for index, cluster in enumerate(data["clusters"], 1):
        flag = "精确复制" if cluster["kind"] == "exact" else "近似复制"
        extra = "  ⚠ 跨模块" if cluster["cross_module"] else ""
        print(f"\n{index}. {flag}  x{cluster['size']}"
              f"  相似度 {cluster['similarity']:.2f}"
              f"  模块 {', '.join(cluster['modules'])}{extra}")
        for member in cluster["members"]:
            print(f"   {member['class_name']}.{member['method_name']}"
                  f"  ({member['lines']} 行)")
            print(f"     {member['file_path']}:{member['line_start']}")
    return 0


def cmd_semantic(args) -> int:
    with _service(args) as service:
        data = service.vector_search(
            args.query, limit=args.limit, module=args.module or ""
        )
    if args.json:
        _dump(data)
        return 0
    if not data.get("available"):
        print(f"语义检索不可用：{data.get('hint', '')}")
        return 0
    print(f"语义检索「{data['query']}」  {data.get('fusion')}  "
          f"索引 {data['summary']['indexed_methods']} 个方法")
    if not data["results"]:
        print("  没有命中")
        return 0
    for index, item in enumerate(data["results"], 1):
        print(f"{index}. {item['score']:.3f}  "
              f"{item['class_name']}.{item['method_name']}  [{item['module']}]")
        print(f"   {item['file_path']}:{item['line_start']}-{item['line_end']}")
        if item.get("body_preview"):
            print(f"   {item['body_preview'][:120]}")
    return 0


def cmd_similar_snippet(args) -> int:
    path = Path(args.file)
    if not path.exists():
        path = Path(args.root or ".") / args.file
    if not path.exists():
        print(f"文件不存在: {args.file}")
        return 1
    code = path.read_text(encoding="utf-8", errors="replace")
    with _service(args) as service:
        data = service.search_similar_snippet(
            code,
            limit=args.limit,
            module=args.module or "",
            min_similarity=args.min_similarity,
        )
    if args.json:
        _dump(data)
        return 0
    if not data.get("available"):
        print(f"语义检索不可用：{data.get('hint', '')}")
        return 0
    print(f"代码片段近邻（≥ {data['min_similarity']:.2f}）  "
          f"索引 {data['summary']['indexed_methods']} 个方法")
    if not data["results"]:
        print("  没有达到阈值的方法")
        return 0
    for item in data["results"]:
        advice = item.get("advice") or {}
        print(f"  相似 {item['similarity']:.3f}  "
              f"{item['class_name']}.{item['method_name']}  ({item['lines']} 行)  "
              f"{advice.get('label', '')}")
        print(f"    {item['file_path']}:{item['line_start']}")
    return 0


def cmd_vectors(args) -> int:
    from .vector_build import build_vectors

    profile = _profile(args)
    try:
        report = build_vectors(
            profile,
            db_path=args.db,
            full=args.full,
            verbose=not args.quiet and not args.json,
            limit=args.limit,
        )
    except Exception as exc:  # noqa: BLE001 - 依赖/模型异常统一提示
        print(f"构建向量索引失败：{exc}")
        return 1
    if args.json:
        _dump(report.to_dict())
        return 1 if report.errors else 0
    print(f"向量索引完成：{report.mode}  {report.elapsed_s:.1f}s")
    print(f"  目录: {report.vector_dir}  collection={report.collection}")
    print(f"  模型: {report.model} / {report.dim} 维"
          f"  稀疏: {report.sparse_model}")
    print(f"  方法: {report.methods_total} 个候选 / {report.methods_selected} 个可向量化")
    print(f"  写入: points={report.points_written}  "
          f"dense: embedded={report.embedded} cached={report.cached}  "
          f"sparse: embedded={report.sparse_embedded} cached={report.sparse_cached}")
    print(f"  文件: {report.files_changed} 变更 / {report.files_deleted} 删除")
    print(f"  索引总量: {report.collection_points} 个方法向量")
    if report.errors:
        for error in report.errors[:10]:
            print(f"  - {error}")
    return 1 if report.errors else 0


def cmd_qa(args) -> int:
    with _service(args) as service:
        if args.qa_action == "save":
            data = service.save_qa(
                args.question, args.answer,
                related_class=args.related_class or "",
                related_module=args.related_module or "",
                related_file=args.related_file or "",
                tags=args.tags or "",
                author=args.author or "",
            )
        elif args.qa_action == "search":
            data = service.search_qa(args.query, related_class=args.related_class or "",
                                     limit=args.limit)
        else:
            ids = [int(x) for x in (args.ids or [])]
            data = service.delete_qa(ids)
    if args.json:
        _dump(data)
        return 0
    if args.qa_action == "save":
        print(f"已保存 Q&A #{data.get('id')}")
    elif args.qa_action == "search":
        for item in data["results"]:
            print(f"\n#{item['id']}  {item['question']}")
            print(f"  {_truncate(item['answer'], 300)}")
            if item["related_class"]:
                print(f"  关联: {item['related_class']}")
    else:
        print(f"已删除 {data.get('deleted', 0)} 条")
    return 0


def cmd_mcp(args) -> int:
    from .mcp_server import run_mcp

    run_mcp(_profile(args), args.db)
    return 0


def cmd_eval(args) -> int:
    profile = _profile(args)
    db_path = Path(args.db) if args.db else profile.resolved_db_path
    if not db_path.exists():
        print(f"索引不存在: {db_path}\n先运行 python3 -m ios_qa build")
        return 2
    only = [part.strip() for part in args.only.split(",") if part.strip()] if args.only else None
    report = run_eval(profile, db_path, args.cases, only)
    if report.get("missing"):
        print(
            f"缺少 golden 用例: {report['cases_path']}\n"
            "在工程内创建 .ios_qa/eval_cases.json（可参考 ios_qa 仓库的 templates/）。"
        )
        return 2
    if args.json:
        _dump(report)
        return 0 if report["failed"] == 0 else 1

    _print_eval_report(report)
    return 0 if report["failed"] == 0 else 1


def cmd_mcp_smoke(args) -> int:
    from .mcp_smoke import run_acceptance

    profile = _profile(args)
    db_path = Path(args.db) if args.db else profile.resolved_db_path
    if not db_path.exists():
        print(f"索引不存在: {db_path}\n先运行 python3 -m ios_qa build")
        return 2
    report = run_acceptance(profile, db_path, clients=args.client, only=args.only)
    if args.json:
        _dump(report)
        return 0 if report["ok"] else 1

    print(f"MCP 验收：{report['project']}  ({report['db_path']})\n")
    print("协议层")
    for client in report["clients"]:
        print(f"  {'✅' if client['ok'] else '❌'} {client['name']}"
              f"  {client['tool_count']} 个工具"
              f"  服务端 {client['meta'].get('server', '-')}"
              f" / 协议 {client['meta'].get('protocol', '-')}")
        for check in client["checks"]:
            if not check["ok"]:
                print(f"     ❌ {check['name']}: {check['detail']}")
    if not report["tools"]["agree"]:
        print("  ❌ 两个客户端看到的工具集合不一致")
    print(f"  工具集合一致: {'是' if report['tools']['agree'] else '否'}"
          f"（{report['tools']['count']} 个）")

    print("\n数据层（客户端返回 vs QueryService 直接调用）")
    for item in report["data"]:
        if item["skipped"]:
            print(f"  ⏭  {item['tool']:<20} {item['detail']}")
        else:
            print(f"  {'✅' if item['ok'] else '❌'} {item['tool']:<20} "
                  f"{item['detail'] or '与查询层逐字段一致'}")

    summary = report["summary"]
    verdict = "通过" if report["ok"] else "失败"
    print(f"\n{verdict} {summary['passed']}/{summary['compared']} 个工具"
          f"（失败 {summary['failed']}）；协议层"
          f"{'通过' if summary['protocol_ok'] else '失败'}")
    return 0 if report["ok"] else 1


def cmd_doctor(args) -> int:
    profile = _profile(args)
    db_path = Path(args.db) if args.db else profile.resolved_db_path
    checks: list[tuple[str, bool, str]] = []
    checks.append(("数据库存在", db_path.exists(), str(db_path)))
    if db_path.exists():
        with QueryService(profile, db_path) as service:
            info = service.info()
            checks.append(
                ("schema", info["schema_version"] == str(SCHEMA_VERSION),
                 info["schema_version"])
            )
            checks.append(("classes > 0", info["counts"]["classes"] > 0,
                           str(info["counts"]["classes"])))
            checks.append(("methods > 0", info["counts"]["methods"] > 0,
                           str(info["counts"]["methods"])))
            probe = service.find_feature("首页 数据", limit=1)
            checks.append(("中文 FTS 可用", True,
                           probe["results"][0]["class_name"] if probe["results"] else "无命中（不阻塞）"))
    ok = all(passed for _, passed, _ in checks)
    if args.json:
        _dump([{"name": n, "ok": p, "detail": d} for n, p, d in checks])
    else:
        for name, passed, detail in checks:
            print(f"{'✅' if passed else '❌'} {name}: {detail}")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    common = _common_parser()
    parser = argparse.ArgumentParser(
        prog="ios_qa",
        description="iOS 静态代码解析 + 知识库（结构化索引 / 语义检索 / 重复代码 / MCP）",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("build", parents=[common], help="构建索引")
    p.add_argument("--full", action="store_true", help="忽略哈希，全量重建")
    p.add_argument("--quiet", action="store_true")
    p.add_argument(
        "--check",
        action="store_true",
        help="构建后跑 eval golden 用例；失败退出码 3（解析失败仍为 1）",
    )
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("info", parents=[common], help="索引健康信息")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("find", parents=[common], help="按功能描述找类/页面")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("hybrid", parents=[common], help="FTS + 向量混合找功能")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--module", default="")
    p.add_argument("--no-rerank", action="store_true", help="只做规则排序，不加载 Cross-Encoder")
    p.set_defaults(func=cmd_hybrid)

    p = sub.add_parser("symbol", parents=[common], help="查类/方法签名")
    p.add_argument("name")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_symbol)

    p = sub.add_parser("trace", parents=[common], help="查类数据流/调用链")
    p.add_argument("class_name")
    p.add_argument("--limit", type=int, default=80)
    p.add_argument("--module", default="", help="同名类按模块消歧")
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser("methods", parents=[common], help="全文检索方法体")
    p.add_argument("query", nargs="?", default="")
    p.add_argument("--class", dest="class_name", default="")
    p.add_argument("--module", default="")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_methods)

    p = sub.add_parser("callers", parents=[common], help="查谁调用了类/方法")
    p.add_argument("target")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_callers)

    p = sub.add_parser("impact", parents=[common], help="影响面分析")
    p.add_argument("target")
    p.add_argument("--depth", type=int, default=2)
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=cmd_impact)

    p = sub.add_parser("read", parents=[common], help="读源码（带行号）")
    p.add_argument("file")
    p.add_argument("--focus", default="")
    p.add_argument("--context", type=int, default=40)
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("pages", parents=[common], help="列出页面入口")
    p.add_argument("--module", default="")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_pages)

    p = sub.add_parser("module", parents=[common], help="模块概览")
    p.add_argument("module")
    p.set_defaults(func=cmd_module)

    p = sub.add_parser("path", parents=[common], help="从入口到页面的跳转链")
    p.add_argument("class_name")
    p.add_argument("--depth", type=int, default=6)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--no-weak", action="store_true",
                   help="不用 instantiate 这类低置信度边")
    p.set_defaults(func=cmd_path)

    p = sub.add_parser("notifications", parents=[common], help="通知注册/移除查询")
    p.add_argument("name", nargs="?", default="", help="通知名或类名（模糊匹配）")
    p.add_argument("--module", default="")
    p.add_argument("--unbalanced", action="store_true", help="只看未配对")
    p.add_argument("--unknown", action="store_true", help="只看无法判定")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_notifications)

    p = sub.add_parser("similar", parents=[common], help="找结构相近的方法（重复代码）")
    p.add_argument("target", help="Class.method 或方法名")
    p.add_argument("--min-similarity", dest="min_similarity", type=float, default=None)
    p.add_argument("--limit", type=int, default=10)
    p.add_argument(
        "--mode",
        choices=["lexical", "semantic"],
        default="lexical",
        help="lexical=词法指纹（默认）；semantic=Qdrant 语义近邻",
    )
    p.set_defaults(func=cmd_similar)

    p = sub.add_parser("semantic", parents=[common], help="自然语言语义搜索方法体")
    p.add_argument("query")
    p.add_argument("--module", default="")
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(func=cmd_semantic)

    p = sub.add_parser(
        "similar-snippet", parents=[common], help="任意代码片段找语义相近的方法"
    )
    p.add_argument("file", help="代码文件路径")
    p.add_argument("--module", default="")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument(
        "--min-similarity", dest="min_similarity", type=float, default=0.90
    )
    p.set_defaults(func=cmd_similar_snippet)

    p = sub.add_parser("vectors", parents=[common], help="构建 Qdrant 向量索引")
    p.add_argument("--full", action="store_true", help="重建 collection")
    p.add_argument("--limit", type=int, default=0, help="只向量化前 N 个方法（调试）")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_vectors)

    p = sub.add_parser("duplicates", parents=[common], help="重复代码聚类")
    p.add_argument("--min-lines", dest="min_lines", type=int, default=6)
    p.add_argument("--min-similarity", dest="min_similarity", type=float, default=0.8)
    p.add_argument("--class", dest="class_name", default="", help="只看某个类")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_duplicates)

    p = sub.add_parser("qa", parents=[common], help="历史问答")
    qa_sub = p.add_subparsers(dest="qa_action", required=True)
    qs = qa_sub.add_parser("save", parents=[common], help="保存问答")
    qs.add_argument("question")
    qs.add_argument("answer")
    qs.add_argument("--related-class", dest="related_class", default="")
    qs.add_argument("--related-module", dest="related_module", default="")
    qs.add_argument("--related-file", dest="related_file", default="")
    qs.add_argument("--tags", default="")
    qs.add_argument("--author", default="")
    qs.set_defaults(func=cmd_qa)
    qs = qa_sub.add_parser("search", parents=[common], help="搜索问答")
    qs.add_argument("query")
    qs.add_argument("--related-class", dest="related_class", default="")
    qs.add_argument("--limit", type=int, default=10)
    qs.set_defaults(func=cmd_qa)
    qs = qa_sub.add_parser("delete", parents=[common], help="按 id 删除")
    qs.add_argument("ids", nargs="+")
    qs.set_defaults(func=cmd_qa)

    p = sub.add_parser("mcp", parents=[common], help="启动 MCP stdio server")
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("eval", parents=[common], help="模型无关的 golden 验收")
    p.add_argument("--cases", default=None, help="用例 JSON（默认 <root>/.ios_qa/eval_cases.json）")
    p.add_argument("--only", default="", help="只跑指定 id，逗号分隔")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser(
        "mcp-smoke", parents=[common],
        help="MCP 双客户端验收（协议层 + 数据层）",
    )
    p.add_argument(
        "--client", choices=["both", "raw", "sdk"], default="both",
        help="both=stdlib JSON-RPC + 官方 SDK（默认）",
    )
    p.add_argument("--only", default="", help="只验指定工具")
    p.set_defaults(func=cmd_mcp_smoke)

    p = sub.add_parser("doctor", parents=[common], help="自检")
    p.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # --root/--db/--profile/--json 两处都能写（子命令前后），默认值在这里补。
    for name, default in (
        ("root", None), ("db", None), ("profile", None), ("json", False)
    ):
        if not hasattr(args, name):
            setattr(args, name, default)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
