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

from .config import ProjectProfile
from .query import QueryService

DEFAULT_CASES = Path(__file__).with_name("eval_cases.json")


def _call(service: QueryService, case: dict) -> dict:
    kind = case["type"]
    args = case.get("args") or {}
    if kind == "info":
        return service.info()
    if kind == "find_feature":
        return service.find_feature(args["query"], limit=args.get("limit", 10))
    if kind == "search_symbol":
        return service.search_symbol(args["name"], limit=args.get("limit", 10))
    if kind == "trace_class":
        return service.trace_class(args["class_name"], limit=args.get("limit", 80))
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


def _check(data: dict, spec: dict) -> tuple[bool, str]:
    kind = spec["kind"]

    if kind == "class_in_results":
        names = _class_names(data)
        ok = spec["value"] in names
        return ok, f"{spec['value']} in {names[:8]}"

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
    path = Path(cases_path) if cases_path else DEFAULT_CASES
    config = json.loads(path.read_text(encoding="utf-8"))
    cases = config.get("cases") or []
    if only:
        selected = set(only)
        cases = [c for c in cases if c.get("id") in selected]

    results: list[dict] = []
    with QueryService(profile, db_path) as service:
        for case in cases:
            checks: list[dict] = []
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

    passed = sum(1 for item in results if item["ok"])
    return {
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "cases": results,
    }
