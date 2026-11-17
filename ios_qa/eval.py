#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模型无关的 golden 验收。

不调用任何 LLM：直接跑查询层，对「类名 / 文件 / 行号 / 接口路径」做断言。
任何 MCP 客户端（OpenCode、Cursor、自研 Agent Loom…）拿到的都是同一份数据，
所以验收不绑定具体模型或 Agent。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR_NAME, ProjectProfile
from .query import QueryService
from .vector_build import read_meta

CASES_FILENAME = "eval_cases.json"


def resolve_cases_path(
    profile: ProjectProfile, cases_path: str | Path | None = None
) -> Path:
    """定位 golden 用例文件。

    显式 ``cases_path`` > ``<root>/.ios_qa/eval_cases.json`` >
    ``<root>/ios_qa/eval_cases.json``（旧布局）。
    """
    if cases_path:
        path = Path(cases_path).expanduser()
        if not path.is_absolute():
            path = profile.root / path
        return path
    for candidate in (
        profile.root / CONFIG_DIR_NAME / CASES_FILENAME,
        profile.root / "ios_qa" / CASES_FILENAME,
    ):
        if candidate.exists():
            return candidate
    return profile.root / CONFIG_DIR_NAME / CASES_FILENAME


def _call(service: QueryService, case: dict) -> dict:
    kind = case["type"]
    args = case.get("args") or {}
    if kind == "info":
        return service.info()
    if kind == "find_feature":
        return service.find_feature(args["query"], limit=args.get("limit", 10))
    if kind == "hybrid_find_feature":
        return service.hybrid_find_feature(
            args["query"],
            limit=args.get("limit", 5),
            module=args.get("module", ""),
        )
    if kind == "search_symbol":
        return service.search_symbol(args["name"], limit=args.get("limit", 10))
    if kind == "trace_class":
        return service.trace_class(
            args["class_name"],
            limit=args.get("limit", 80),
            module=args.get("module", ""),
        )
    if kind == "search_methods":
        return service.search_methods(
            args.get("query", ""),
            class_name=args.get("class_name", ""),
            module=args.get("module", ""),
            limit=args.get("limit", 20),
        )
    if kind == "find_callers":
        return service.find_callers(args["target"], limit=args.get("limit", 50))
    if kind == "impact_analysis":
        return service.impact_analysis(
            args["target"], depth=args.get("depth", 2), limit=args.get("limit", 200)
        )
    if kind == "find_navigation":
        return service.find_navigation(
            args["target"],
            depth=args.get("depth", 6),
            limit=args.get("limit", 20),
            include_weak=args.get("include_weak", True),
        )
    if kind == "find_similar_code":
        return service.find_similar_code(
            args["target"],
            limit=args.get("limit", 10),
            min_similarity=args.get("min_similarity", 0.3),
        )
    if kind == "find_duplicates":
        return service.find_duplicates(
            min_lines=args.get("min_lines", 6),
            min_similarity=args.get("min_similarity", 0.8),
            limit=args.get("limit", 20),
            class_name=args.get("class_name", ""),
        )
    if kind == "vector_search":
        return service.vector_search(
            args["query"], limit=args.get("limit", 5), module=args.get("module", "")
        )
    if kind == "search_similar_snippet":
        code = args.get("code", "")
        if not code and args.get("method_name"):
            row = service.conn.execute(
                "SELECT body FROM methods WHERE class_name=? AND method_name=? "
                "ORDER BY line_start LIMIT 1",
                (args.get("class_name", ""), args["method_name"]),
            ).fetchone()
            code = row["body"] if row else ""
        return service.search_similar_snippet(
            code,
            limit=args.get("limit", 5),
            module=args.get("module", ""),
            min_similarity=args.get("min_similarity", 0.75),
        )
    if kind == "find_notifications":
        return service.find_notifications(
            name=args.get("name", ""),
            module=args.get("module", ""),
            balanced=args.get("balanced"),
            limit=args.get("limit", 100),
        )
    if kind == "read_source":
        return service.read_source(
            args["file_path"],
            focus=args.get("focus", ""),
            context=args.get("context", 40),
        )
    if kind == "list_pages":
        return service.list_pages(module=args.get("module", ""), limit=args.get("limit", 100))
    if kind == "list_module":
        return service.list_module(args["module"])
    raise ValueError(f"未知用例类型: {kind}")


def _dig(data: Any, path: str) -> Any:
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _class_names(data: dict) -> list[str]:
    rows = data.get("results") or data.get("pages") or []
    return [row.get("class_name", "") for row in rows if row.get("class_name")]


def _rank_check(
    data: dict, spec: dict, field: str, top_only: bool
) -> tuple[bool, str]:
    """排序类断言：命中位置 + 可选的最低分 + 可选的「与第二名的间隔」。

    绝对分数是模型/模板相关的（换模型会整体平移），所以「能不能区分」
    用 ``min_margin`` 断言更稳：只要求 top1 比 top2 高出一定间隔。
    """
    rows = data.get("results") or []
    threshold_key = "min_similarity" if field == "similarity" else "min_score"
    threshold = float(spec.get(threshold_key, 0.0))
    margin_need = spec.get("min_margin")
    view = rows[:1] if top_only else rows
    seen = [
        f"{row.get('class_name')}.{row.get('method_name')}={row.get(field)}"
        for row in view
    ]
    ok = any(
        f"{row.get('class_name')}.{row.get('method_name')}" == spec["value"]
        and float(row.get(field) or 0) >= threshold
        for row in view
    )
    margin: float | None
    if len(rows) >= 2:
        margin = float(rows[0].get(field) or 0) - float(rows[1].get(field) or 0)
    elif rows:
        margin = float(rows[0].get(field) or 0)
    else:
        margin = None
    if margin_need is not None:
        need = float(margin_need)
        ok = ok and margin is not None and margin >= need
    detail = f"{threshold_key}={threshold}"
    if margin_need is not None:
        detail += f" min_margin={margin_need}"
    if margin is not None:
        detail += f" margin={margin:.4f}"
    return ok, f"{detail} seen={seen}"


def _check(data: dict, spec: dict) -> tuple[bool, str]:
    kind = spec["kind"]

    if kind == "class_in_results":
        names = _class_names(data)
        ok = spec["value"] in names
        return ok, f"{spec['value']} in {names[:8]}"

    if kind == "feature_rank":
        results = data.get("results") or []
        max_rank = int(spec.get("max_rank", 1))
        wanted = set(spec.get("values") or [spec.get("value")])
        for index, row in enumerate(results[:max_rank], 1):
            if row.get("class_name") in wanted:
                return True, (
                    f"{row.get('class_name')} rank={index} score={row.get('score')} "
                    f"sources={row.get('sources')}"
                )
        seen = [
            f"{row.get('class_name')}={row.get('score')}"
            for row in results[:max_rank]
        ]
        return False, f"{sorted(wanted)} not in top {max_rank}: {seen}"

    if kind == "min_results":
        rows = (
            data.get("results")
            or data.get("pages")
            or data.get("callers")
            or data.get("methods")
            or []
        )
        ok = len(rows) >= int(spec["value"])
        return ok, f"count={len(rows)} >= {spec['value']}"

    if kind == "superclass_equals":
        rows = data.get("results") or []
        actual = rows[0].get("superclass", "") if rows else ""
        return actual == spec["value"], f"superclass={actual!r}"

    if kind == "method_at_line":
        rows = data.get("results") or []
        methods = rows[0].get("methods", []) if rows else []
        for method in methods:
            if (
                method.get("method_name") == spec["method_name"]
                and method.get("file_path", "").endswith(spec["file_suffix"])
                and int(method.get("line_start", 0)) == int(spec["line"])
            ):
                return True, f"{spec['method_name']}@{spec['file_suffix']}:{spec['line']}"
        actual = [
            m.get("method_name") for m in methods
            if m.get("file_path", "").endswith(spec["file_suffix"])
        ]
        return False, f"missing {spec['method_name']}@line {spec['line']}; got {actual[:6]}"

    if kind == "trace_found":
        return bool(data.get("found")), f"found={data.get('found')}"

    if kind == "vector_available":
        return bool(data.get("available")), f"available={data.get('available')} hint={data.get('hint', '')}"

    if kind in {"vector_top", "vector_contains"}:
        return _rank_check(data, spec, field="score", top_only=kind == "vector_top")

    if kind == "similar_found":
        return bool(data.get("found")), (
            f"found={data.get('found')} hint={data.get('hint', '')}"
        )

    if kind in {"similar_top", "similar_contains"}:
        return _rank_check(
            data, spec, field="similarity", top_only=kind == "similar_top"
        )

    if kind == "duplicate_group":
        wanted = set(spec["members"])
        threshold = float(spec.get("min_similarity", 0.0))
        for cluster in data.get("clusters") or []:
            members = {
                f"{row.get('class_name')}.{row.get('method_name')}"
                for row in cluster.get("members") or []
            }
            if wanted <= members and float(cluster.get("similarity") or 0) >= threshold:
                return True, (
                    f"cluster kind={cluster.get('kind')} size={cluster.get('size')} "
                    f"similarity={cluster.get('similarity')} members={sorted(members)}"
                )
        clusters = [
            [f"{row.get('class_name')}.{row.get('method_name')}"
             for row in cluster.get("members") or []]
            for cluster in data.get("clusters") or []
        ]
        return False, f"missing {sorted(wanted)} in clusters={clusters}"

    if kind == "file_line_present":
        rows = data.get(spec.get("list", ""), [])
        for row in rows:
            line = row.get("line", row.get("line_number", row.get("line_start", 0)))
            if (
                row.get("file_path", "").endswith(spec["file_suffix"])
                and int(line or 0) == int(spec["line"])
            ):
                return True, f"{spec['file_suffix']}:{spec['line']}"
        actual = sorted({
            f"{Path(row.get('file_path', '')).name}:"
            f"{row.get('line', row.get('line_number', row.get('line_start', 0)))}"
            for row in rows
        })
        return False, f"missing {spec['file_suffix']}:{spec['line']}; got {actual[:6]}"

    if kind == "api_url_contains":
        urls = [row.get("url_pattern", "") for row in data.get("api_calls", [])]
        ok = any(spec["value"] in url for url in urls)
        return ok, f"{spec['value']} in {urls[:8]}"

    if kind == "read_focus_matched":
        return bool(data.get("focus_matched")), f"focus_matched={data.get('focus_matched')}"

    if kind == "read_content_contains":
        ok = spec["value"] in (data.get("content") or "")
        return ok, f"content contains {spec['value']!r}"

    if kind == "field_gte":
        actual = _dig(data, spec["field"])
        ok = actual is not None and actual >= spec["value"]
        return ok, f"{spec['field']}={actual} >= {spec['value']}"

    if kind == "warnings_empty":
        warnings = data.get("warnings") or []
        return not warnings, f"warnings={warnings}"

    if kind == "level_class_present":
        names = [
            item.get("class_name", "")
            for level in data.get("levels", [])
            for item in level.get("items", [])
        ]
        ok = spec["value"] in names
        return ok, f"{spec['value']} in {names[:8]}"

    if kind == "navigation_reachable":
        ok = bool(data.get("reachable"))
        return ok, f"reachable={ok}, roots={data.get('roots')}"

    if kind == "navigation_edge":
        for path in data.get("paths", []):
            for hop in path.get("chain", []):
                if hop.get("from_class") != spec["from"]:
                    continue
                if hop.get("to_class") != spec["to"]:
                    continue
                if "edge_kind" in spec and hop.get("kind") != spec["edge_kind"]:
                    continue
                if "line" in spec and int(hop.get("line_number", 0)) != int(spec["line"]):
                    continue
                if "file_suffix" in spec and not hop.get("file_path", "").endswith(
                    spec["file_suffix"]
                ):
                    continue
                return True, f"{spec['from']}->{spec['to']} found"
        hops = [
            f"{hop.get('from_class')}->{hop.get('to_class')}"
            for path in data.get("paths", [])
            for hop in path.get("chain", [])
        ]
        return False, f"missing {spec['from']}->{spec['to']}; got {hops[:6]}"

    if kind == "notification_contains":
        for row in data.get("results", []):
            if (
                row.get("class_name") == spec["class"]
                and row.get("notification_name") == spec["name"]
            ):
                return True, f"{spec['class']}.{spec['name']}"
        actual = [
            f"{row.get('class_name')}.{row.get('notification_name')}"
            for row in data.get("results", [])
        ]
        return False, f"missing {spec['class']}.{spec['name']}; got {actual[:6]}"

    if kind == "summary_gte":
        actual = (data.get("summary") or {}).get(spec["field"])
        ok = actual is not None and actual >= spec["value"]
        return ok, f"summary.{spec['field']}={actual} >= {spec['value']}"

    if kind == "synonyms_applied":
        pairs = {(item.get("from"), item.get("to")) for item in data.get("synonyms") or []}
        ok = (spec["from"], spec["to"]) in pairs
        return ok, f"synonyms={sorted(pairs)}"

    if kind == "ambiguous":
        candidates = data.get("candidates") or []
        ok = bool(data.get("ambiguous")) and len(candidates) >= int(spec.get("min", 2))
        return ok, (
            f"ambiguous={data.get('ambiguous')} selected={data.get('selected_module')!r} "
            f"candidates={[(c.get('module'), c.get('method_count')) for c in candidates]}"
        )

    if kind == "candidate_modules":
        modules = {candidate.get("module") for candidate in data.get("candidates") or []}
        missing = [name for name in spec["values"] if name not in modules]
        return not missing, f"modules={sorted(modules)}"

    if kind == "all_methods_module":
        modules = {method.get("module") for method in data.get("methods") or []}
        ok = bool(modules) and modules == {spec["value"]}
        return ok, f"method modules={sorted(modules)}"

    if kind == "extension_category":
        for row in data.get("extensions", []):
            if row.get("category_name") != spec["value"]:
                continue
            if "file_suffix" in spec and not row.get("file_path", "").endswith(
                spec["file_suffix"]
            ):
                continue
            return True, (
                f"category {spec['value']} at "
                f"{row.get('file_path')}:{row.get('line_start')}"
            )
        actual = [
            (row.get("category_name"), row.get("file_path"))
            for row in data.get("extensions", [])
        ]
        return False, f"missing category {spec['value']}; got {actual[:6]}"

    raise ValueError(f"未知断言: {kind}")


def run_eval(
    profile: ProjectProfile,
    db_path: str | Path | None = None,
    cases_path: str | Path | None = None,
    only: list[str] | None = None,
) -> dict:
    path = resolve_cases_path(profile, cases_path)
    if not path.exists():
        return {
            "total": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "needs_review": 0,
            "missing": True,
            "cases_path": str(path),
            "cases": [],
        }
    config = json.loads(path.read_text(encoding="utf-8"))
    cases = config.get("cases") or []
    if only:
        selected = set(only)
        cases = [c for c in cases if c.get("id") in selected]

    # 索引文本契约（模型 + 模板）变了，golden 里的分数/排序必须人工复核：
    # 相似度是相对量，模板一改刻度就平移，自动判红或判绿都会误导。
    meta = read_meta(profile, db_path)
    built_template = str(meta.get("template_id") or "")
    case_template = str(config.get("template_id") or "")
    template_stale = bool(built_template and case_template and built_template != case_template)
    template_warning = ""
    if not template_stale:
        if not case_template:
            template_warning = (
                "golden 未绑定 template_id，改造索引文本后请人工复核向量类用例"
            )
        elif not built_template:
            template_warning = "向量索引缺少 template_id（旧索引），建议重新运行 vectors"

    results: list[dict] = []
    with QueryService(profile, db_path) as service:
        for case in cases:
            checks: list[dict] = []
            if case.get("requires") == "vectors":
                available, hint = service._vector_status()
                if not available:
                    results.append(
                        {
                            "id": case.get("id", ""),
                            "type": case.get("type", ""),
                            "reason": case.get("reason") or case.get("note", ""),
                            "last_reviewed": case.get("last_reviewed", ""),
                            "ok": True,
                            "skipped": True,
                            "skip_reason": hint,
                            "checks": [{"kind": "skipped", "ok": True, "detail": hint}],
                        }
                    )
                    continue
                if template_stale:
                    detail = (
                        f"索引 template_id={built_template}，"
                        f"用例 template_id={case_template}，需人工复核后更新用例"
                    )
                    results.append(
                        {
                            "id": case.get("id", ""),
                            "type": case.get("type", ""),
                            "reason": case.get("reason") or case.get("note", ""),
                            "last_reviewed": case.get("last_reviewed", ""),
                            "ok": False,
                            "needs_review": True,
                            "review_reason": detail,
                            "checks": [
                                {"kind": "template_changed", "ok": False, "detail": detail}
                            ],
                        }
                    )
                    continue
            try:
                data = _call(service, case)
                for spec in case.get("checks") or []:
                    ok, detail = _check(data, spec)
                    checks.append({"kind": spec["kind"], "ok": ok, "detail": detail})
            except Exception as exc:  # noqa: BLE001 - 单个用例失败继续跑
                checks.append(
                    {"kind": "exception", "ok": False,
                     "detail": f"{type(exc).__name__}: {exc}"}
                )
            results.append(
                {
                    "id": case.get("id", ""),
                    "type": case.get("type", ""),
                    "reason": case.get("reason") or case.get("note", ""),
                    "last_reviewed": case.get("last_reviewed", ""),
                    "ok": all(c["ok"] for c in checks),
                    "checks": checks,
                }
            )

    skipped = sum(1 for item in results if item.get("skipped"))
    review = sum(1 for item in results if item.get("needs_review"))
    passed = sum(
        1
        for item in results
        if item["ok"] and not item.get("skipped") and not item.get("needs_review")
    )
    return {
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed - skipped - review,
        "skipped": skipped,
        "needs_review": review,
        "template_id": built_template,
        "case_template_id": case_template,
        "template_warning": template_warning,
        "cases": results,
    }
