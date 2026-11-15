#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查询服务：纯 Python API，不依赖 MCP / CLI。

后续接 Agent Loom 时复用这一层；MCP 工具只是把这里的函数包一层 JSON。
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .config import ProjectProfile
from .db import connect, counts, get_metadata
from .synonyms import expand_query, load_groups
from .textnorm import build_search_text, fts_expr, like_pattern, snippet


def _json_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except (ValueError, TypeError):
        return []


def _class_row(row: sqlite3.Row, detail: bool = False) -> dict:
    data = {
        "class_name": row["class_name"],
        "kind": row["kind"],
        "language": row["language"],
        "module": row["module"],
        "modules": _json_list(row["modules_json"]),
        "superclass": row["superclass"],
        "protocols": _json_list(row["protocols_json"]),
        "properties": _json_list(row["properties_json"]),
        "method_count": row["method_count"],
        "extension_count": row["extension_count"],
        "header_path": row["header_path"],
        "impl_path": row["impl_path"],
        "file_path": row["file_path"],
        "doc_comment": row["doc_comment"],
        "is_view_controller": bool(row["is_view_controller"]),
        "vc_base": row["vc_base"],
        "is_app_entry": bool(row["is_app_entry"]),
        "line_start": row["line_start"],
        "line_end": row["line_end"],
        "ambiguous": bool(row["is_ambiguous"]),
        "sites": _json_list(row["sites_json"]),
    }
    if detail:
        data["interface_text"] = row["interface_text"]
        data["method_names"] = row["method_names"].split()
    return data


def _method_row(row: sqlite3.Row, with_body: bool = False) -> dict:
    data = {
        "class_name": row["class_name"],
        "method_name": row["method_name"],
        "signature": row["signature"],
        "file_path": row["file_path"],
        "line_start": row["line_start"],
        "line_end": row["line_end"],
        "language": row["language"],
        "module": row["module"],
        "is_class_method": bool(row["is_class_method"]),
        "is_extension": bool(row["is_extension"]),
        "has_body": bool(row["has_body"]),
        "doc_comment": row["doc_comment"],
        "body_preview": row["body_preview"],
    }
    if with_body:
        data["body"] = row["body"]
    return data


class QueryService:
    def __init__(self, profile: ProjectProfile, db_path: str | Path | None = None) -> None:
        self.profile = profile
        self.db_path = Path(db_path) if db_path else profile.resolved_db_path
        self.conn = connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.synonym_groups = load_groups(profile)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "QueryService":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 基础信息
    # ------------------------------------------------------------------
    def info(self) -> dict:
        metadata = get_metadata(self.conn)
        table_counts = counts(self.conn)
        warnings: list[str] = []

        build_time = metadata.get("build_time", "")
        age_days = None
        if build_time:
            try:
                age_days = (dt.datetime.now() - dt.datetime.fromisoformat(build_time)).days
                if age_days > 7:
                    warnings.append(f"索引已 {age_days} 天未重建，建议运行 build --full")
            except ValueError:
                pass

        current_branch = _git(self.profile.root, "rev-parse", "--abbrev-ref", "HEAD")
        if current_branch and metadata.get("git_branch") and current_branch != metadata["git_branch"]:
            warnings.append(
                f"索引分支 {metadata['git_branch']} 与当前分支 {current_branch} 不一致"
            )

        error_count = self.conn.execute(
            "SELECT COUNT(*) FROM files WHERE status='error'"
        ).fetchone()[0]
        if error_count:
            warnings.append(f"{error_count} 个文件解析失败，运行 build 查看错误")

        # 对应参考方案的 get_db_info() 检查：没索引到方法时检索不可用
        if table_counts.get("methods", 0) == 0:
            warnings.append("未索引到任何方法，请先运行 build 构建索引")

        unbalanced = self.conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE is_balanced=0"
        ).fetchone()[0]
        body_methods = self.conn.execute(
            "SELECT COUNT(*) FROM methods WHERE has_body=1"
        ).fetchone()[0]
        db_size = self.db_path.stat().st_size if self.db_path.exists() else 0
        last_run = self.conn.execute(
            "SELECT * FROM build_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()

        return {
            "project": metadata.get("project_name", self.profile.name),
            "project_root": metadata.get("project_root", str(self.profile.root)),
            "db_path": str(self.db_path),
            "db_size_mb": round(db_size / 1024 / 1024, 2),
            "schema_version": metadata.get("schema_version", ""),
            "build_time": build_time,
            "index_age_days": age_days,
            "git_branch": metadata.get("git_branch", ""),
            "git_commit": metadata.get("git_commit", ""),
            "git_dirty_files": metadata.get("git_dirty", ""),
            "source_file_count": metadata.get("source_file_count", ""),
            "source_loc": metadata.get("source_loc", ""),
            "counts": table_counts,
            "methods_with_body": body_methods,
            "unbalanced_notifications": unbalanced,
            "last_build": dict(last_run) if last_run else None,
            "warnings": warnings,
        }

    # ------------------------------------------------------------------
    # 功能 / 符号
    # ------------------------------------------------------------------
    def _feature_scores(
        self,
        query: str,
        limit: int,
        penalty: float = 0.0,
        matched_suffix: str = "",
        allow_like: bool = True,
    ) -> dict[str, dict]:
        """类名 / FTS / 方法体 / 入口四路打分，返回 ``class_name -> 结果``。"""
        scores: dict[str, dict] = {}

        def add(row: sqlite3.Row, score: float, matched_on: str, extra: dict | None = None) -> None:
            data = _class_row(row)
            key = data["class_name"]
            if data["is_view_controller"]:
                score += 4
            if data["is_app_entry"]:
                score += 3
            score -= penalty
            matched = f"{matched_on}{matched_suffix}"
            existing = scores.get(key)
            if existing is None or score > existing["score"]:
                scores[key] = {
                    **data,
                    "score": round(score, 2),
                    "matched_on": matched,
                    **(extra or {}),
                }
            elif extra:
                existing.update({k: v for k, v in extra.items() if k not in existing})

        # 1. 精确 / 包含的类名
        exact = self.conn.execute(
            "SELECT * FROM classes WHERE class_name = ? COLLATE NOCASE", (query,)
        ).fetchall()
        for row in exact:
            add(row, 100, "class_exact")
        contains = self.conn.execute(
            "SELECT * FROM classes WHERE class_name LIKE ? LIMIT ?",
            (f"%{query}%", limit * 3),
        ).fetchall()
        for row in contains:
            add(row, 80 if row["class_name"].lower().startswith(query.lower()) else 60,
                "class_name")

        # 2. FTS（先 AND 后 OR）
        for op in ("AND", "OR"):
            expr = fts_expr(query, op)
            if not expr:
                break
            rows = self.conn.execute(
                """
                SELECT c.*, bm25(classes_fts) AS rank
                FROM classes_fts f JOIN classes c ON c.rowid = f.rowid
                WHERE classes_fts MATCH ?
                ORDER BY rank LIMIT ?
                """,
                (expr, limit * 4),
            ).fetchall()
            for row in rows:
                rank = row["rank"] if row["rank"] is not None else 0
                # OR 是「部分命中」的兜底，不能和 AND 同分，否则会把
                # 只沾一个词的类顶到同义/精确命中前面。
                base = 50 if op == "AND" else 44
                add(row, base - max(-20.0, min(20.0, rank)), f"fts_{op.lower()}",
                    {"snippet": snippet(row["doc_comment"] or row["search_text"], query)})
            if rows:
                break

        # 3. 方法级命中，归并到类
        for op in ("AND", "OR"):
            expr = fts_expr(query, op)
            if not expr:
                break
            rows = self.conn.execute(
                """
                SELECT m.class_name, m.method_name, m.body_preview, m.file_path,
                       m.line_start, bm25(methods_fts) AS rank
                FROM methods_fts f JOIN methods m ON m.id = f.rowid
                WHERE methods_fts MATCH ?
                ORDER BY rank LIMIT ?
                """,
                (expr, limit * 5),
            ).fetchall()
            for row in rows:
                cls = self.conn.execute(
                    "SELECT * FROM classes WHERE class_name = ?", (row["class_name"],)
                ).fetchone()
                if cls is None:
                    continue
                add(cls, (35 if op == "AND" else 30) - max(-15.0, min(15.0, row["rank"] or 0)),
                    "method_fts",
                    {
                        "matched_method": row["method_name"],
                        "matched_file": row["file_path"],
                        "matched_line": row["line_start"],
                        "snippet": snippet(row["body_preview"], query),
                    })
            if rows:
                break

        # 4. 入口 / 路由 / storyboard id
        entries = self.conn.execute(
            "SELECT * FROM entrypoints WHERE class_name LIKE ? OR identifier LIKE ? LIMIT ?",
            (f"%{query}%", f"%{query}%", limit * 3),
        ).fetchall()
        for entry in entries:
            cls = self.conn.execute(
                "SELECT * FROM classes WHERE class_name = ?", (entry["class_name"],)
            ).fetchone()
            if cls:
                add(cls, 70, "entrypoint",
                    {"entrypoint": {"kind": entry["kind"], "identifier": entry["identifier"],
                                    "source": entry["source"], "file_path": entry["file_path"]}})

        # 5. LIKE 兜底（无法分词 / 极短查询）
        if allow_like and not scores:
            pattern = like_pattern(query)
            rows = self.conn.execute(
                """
                SELECT * FROM classes
                WHERE class_name LIKE ? ESCAPE '\\' OR doc_comment LIKE ? ESCAPE '\\'
                   OR method_names LIKE ? ESCAPE '\\'
                LIMIT ?
                """,
                (pattern, pattern, pattern, limit * 3),
            ).fetchall()
            for row in rows:
                add(row, 30, "like")

        return scores

    def find_feature(self, query: str, limit: int = 10) -> dict:
        query = (query or "").strip()
        if not query:
            return {"query": query, "results": []}
        limit = max(1, min(int(limit), 50))
        scores = self._feature_scores(query, limit)

        # 业务别名扩展：行情↔报价、崩溃↔crash、卡顿↔掉帧 …
        # 只在该查询字面命中明显不足时启用（最高分 < 55）：像「首页」这种
        # 直接命中很强的查询，扩展成 home 反而会灌进一堆 QWeatherHome* 视图。
        applied: list[dict] = []
        seen_pairs: set[tuple[str, str]] = set()
        primary_max = max((item["score"] for item in scores.values()), default=0.0)
        if not scores or primary_max < 55:
            for expansion in expand_query(query, self.synonym_groups):
                weak = self._feature_scores(
                    expansion["query"], limit, penalty=2.0,
                    matched_suffix="_synonym", allow_like=False,
                )
                if not weak:
                    continue
                pair = (expansion["from"], expansion["to"])
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    applied.append({"from": expansion["from"], "to": expansion["to"]})
                label = f"{expansion['from']}→{expansion['to']}"
                for key, item in weak.items():
                    existing = scores.get(key)
                    if existing is None:
                        item["synonym"] = label
                        scores[key] = item
                    elif "synonym" not in existing:
                        existing["synonym"] = label

        results = sorted(scores.values(), key=lambda x: (-x["score"], x["class_name"]))[:limit]
        for item in results:
            item["methods"] = self._top_methods(item["class_name"], 5)
        payload = {"query": query, "results": results}
        if applied:
            payload["synonyms"] = applied
        return payload

    def search_symbol(self, name: str, limit: int = 10) -> dict:
        name = (name or "").strip()
        if not name:
            return {"query": name, "results": []}
        limit = max(1, min(int(limit), 50))
        scores: dict[str, dict] = {}

        def add(row: sqlite3.Row, score: float, matched_on: str) -> None:
            data = _class_row(row, detail=True)
            key = data["class_name"]
            if key not in scores or score > scores[key]["score"]:
                scores[key] = {**data, "score": score, "matched_on": matched_on}

        for row in self.conn.execute(
            "SELECT * FROM classes WHERE class_name = ? COLLATE NOCASE", (name,)
        ):
            add(row, 100, "exact")
        selector_prefix = name.split(":")[0]
        for row in self.conn.execute(
            "SELECT * FROM classes WHERE class_name LIKE ? COLLATE NOCASE LIMIT ?",
            (f"{name}%", limit * 3),
        ):
            add(row, 80, "prefix")
        for row in self.conn.execute(
            "SELECT * FROM classes WHERE class_name LIKE ? COLLATE NOCASE LIMIT ?",
            (f"%{name}%", limit * 3),
        ):
            add(row, 60, "contains")

        method_rows = self.conn.execute(
            """
            SELECT * FROM methods
            WHERE method_name = ? OR method_name LIKE ?
            ORDER BY has_body DESC, class_name LIMIT ?
            """,
            (name, f"{selector_prefix}%", limit * 6),
        ).fetchall()
        for method in method_rows:
            cls = self.conn.execute(
                "SELECT * FROM classes WHERE class_name = ?", (method["class_name"],)
            ).fetchone()
            if cls:
                add(cls, 55 if method["method_name"] == name else 40, "method")

        # 分类 / 扩展名（``UIViewController+BreadCrumb``）也能被搜到
        category_rows = self.conn.execute(
            """
            SELECT class_name, category_name, kind, language, file_path, line_start
            FROM symbols
            WHERE category_name LIKE ? COLLATE NOCASE
            ORDER BY LENGTH(category_name), class_name LIMIT ?
            """,
            (f"%{name}%", limit * 3),
        ).fetchall()
        for symbol in category_rows:
            key = symbol["class_name"]
            item = scores.get(key)
            if item is None:
                cls = self.conn.execute(
                    "SELECT * FROM classes WHERE class_name = ?", (key,)
                ).fetchone()
                if cls is None:
                    continue
                item = {
                    **_class_row(cls, detail=True),
                    "score": 70 if symbol["category_name"].lower() == name.lower() else 50,
                    "matched_on": "category",
                }
                scores[key] = item
            item.setdefault(
                "category",
                {
                    "name": symbol["category_name"],
                    "kind": symbol["kind"],
                    "language": symbol["language"],
                    "file_path": symbol["file_path"],
                    "line_start": symbol["line_start"],
                },
            )

        results = sorted(scores.values(), key=lambda x: (-x["score"], x["class_name"]))[:limit]
        for item in results:
            item["methods"] = self._top_methods(item["class_name"], 30)
        return {"query": name, "results": results}

    def _top_methods(self, class_name: str, limit: int, module: str = "") -> list[dict]:
        cond = " AND module = ?" if module else ""
        params: tuple = (class_name, module, limit) if module else (class_name, limit)
        rows = self.conn.execute(
            f"""
            SELECT * FROM methods WHERE class_name = ?{cond}
            ORDER BY has_body DESC, is_class_method DESC, line_start LIMIT ?
            """,
            params,
        ).fetchall()
        return [_method_row(row) for row in rows]

    # ------------------------------------------------------------------
    # 数据流 / 调用链 / 影响面
    # ------------------------------------------------------------------
    def trace_class(self, class_name: str, limit: int = 80, module: str = "") -> dict:
        class_name = (class_name or "").strip()
        row = self.conn.execute(
            "SELECT * FROM classes WHERE class_name = ? COLLATE NOCASE", (class_name,)
        ).fetchone()
        resolved_from = ""
        if row is None:
            found = self.search_symbol(class_name, limit=1)
            if not found["results"]:
                return {"query": class_name, "found": False}
            resolved_from = class_name
            class_name = found["results"][0]["class_name"]
            row = self.conn.execute(
                "SELECT * FROM classes WHERE class_name = ?", (class_name,)
            ).fetchone()

        limit = max(1, min(int(limit), 300))
        info = _class_row(row, detail=True)
        sites = info.get("sites") or []
        ambiguous = bool(info.get("ambiguous"))
        candidates: list[dict] = []
        selected_module = ""
        if ambiguous:
            candidates = [
                {
                    "module": site.get("module", ""),
                    "kind": site.get("kind", ""),
                    "file_path": site.get("file_path", ""),
                    "line_start": site.get("line_start", 0),
                    "method_count": site.get("method_count", 0),
                }
                for site in sites
            ]
            requested = (module or "").strip()
            known = {candidate["module"] for candidate in candidates}
            selected_module = requested if requested in known else candidates[0]["module"]

        # 同名类合并了多个模块的声明时，按选中的声明点收敛，避免把两个类混在一起。
        # 只收敛「属于本类自己的行」（方法/依赖/调用/通知/跳转/扩展）；
        # 反向的「谁依赖我、谁调用我」按类名匹配，不同模块的同名类分不开，
        # 宁多勿漏，所以不过滤。
        def scoped(column: str = "module") -> tuple[str, tuple]:
            if not selected_module:
                return "", ()
            return f" AND {column} = ?", (selected_module,)

        cond, extra = scoped()
        chain = self._superclass_chain(class_name)
        methods = self._top_methods(class_name, limit, module=selected_module)
        deps = _dedup_dicts(
            (
                {"dep_class": r["dep_class"], "dep_kind": r["dep_kind"],
                 "file_path": r["file_path"], "line": r["line_number"]}
                for r in self.conn.execute(
                    "SELECT * FROM deps WHERE from_class = ?" + cond
                    + " ORDER BY dep_kind, dep_class",
                    (class_name, *extra),
                )
            ),
            ("dep_class", "dep_kind"),
        )
        dependents = _dedup_dicts(
            (
                {"from_class": r["from_class"], "dep_kind": r["dep_kind"],
                 "file_path": r["file_path"], "line": r["line_number"]}
                for r in self.conn.execute(
                    "SELECT * FROM deps WHERE dep_class = ? AND from_class != '<file>' "
                    "ORDER BY from_class",
                    (class_name,),
                )
            ),
            ("from_class", "dep_kind"),
        )
        api_calls = [
            dict(r) for r in self.conn.execute(
                "SELECT * FROM api_calls WHERE class_name = ?" + cond
                + " ORDER BY line_number",
                (class_name, *extra),
            )
        ]
        notifications = [
            dict(r) for r in self.conn.execute(
                "SELECT * FROM notifications WHERE class_name = ?" + cond
                + " ORDER BY register_line",
                (class_name, *extra),
            )
        ]
        callers = [
            dict(r) for r in self.conn.execute(
                "SELECT * FROM calls WHERE callee_class = ? "
                "ORDER BY caller_class, line_number LIMIT ?",
                (class_name, limit),
            )
        ]
        callees = [
            dict(r) for r in self.conn.execute(
                "SELECT * FROM calls WHERE caller_class = ?" + cond
                + " ORDER BY callee_class, line_number LIMIT ?",
                (class_name, *extra, limit),
            )
        ]
        nav_in = [
            dict(r) for r in self.conn.execute(
                "SELECT * FROM navigation WHERE to_class = ? "
                "ORDER BY from_class LIMIT ?",
                (class_name, limit),
            )
        ]
        nav_out = [
            dict(r) for r in self.conn.execute(
                "SELECT * FROM navigation WHERE from_class = ?" + cond
                + " ORDER BY to_class LIMIT ?",
                (class_name, *extra, limit),
            )
        ]
        extensions = [
            {
                "kind": r["kind"],
                "category_name": r["category_name"],
                "extension_of": r["extension_of"],
                "language": r["language"],
                "module": r["module"],
                "file_path": r["file_path"],
                "line_start": r["line_start"],
                "superclass": r["superclass"],
                "protocols": _json_list(r["protocols_json"]),
                "properties": _json_list(r["properties_json"]),
            }
            for r in self.conn.execute(
                "SELECT * FROM symbols WHERE class_name = ? "
                "AND kind IN ('extension','category','category_impl')" + cond
                + " ORDER BY line_start",
                (class_name, *extra),
            )
        ]
        scoped_count = len(methods)
        if ambiguous:
            for candidate in candidates:
                if candidate["module"] == selected_module:
                    scoped_count = candidate["method_count"]
                    break
        entrypoints = [
            dict(r) for r in self.conn.execute(
                "SELECT * FROM entrypoints WHERE class_name = ?", (class_name,)
            )
        ]
        qa = self.search_qa(class_name, limit=5).get("results", [])

        return {
            "query": class_name,
            "resolved_from": resolved_from,
            "found": True,
            **info,
            "ambiguous": ambiguous,
            "selected_module": selected_module,
            "scoped_method_count": scoped_count,
            "candidates": candidates,
            "superclass_chain": chain,
            "methods": methods,
            "deps": deps,
            "dependents": dependents,
            "api_calls": api_calls,
            "notifications": notifications,
            "callers": callers,
            "callees": callees,
            "navigation_in": nav_in,
            "navigation_out": nav_out,
            "extensions": extensions,
            "entrypoints": entrypoints,
            "related_qa": qa,
        }

    def _superclass_chain(self, class_name: str) -> list[str]:
        chain: list[str] = []
        seen: set[str] = set()
        current = class_name
        while current and current not in seen:
            seen.add(current)
            row = self.conn.execute(
                "SELECT superclass FROM classes WHERE class_name = ?", (current,)
            ).fetchone()
            if not row or not row["superclass"]:
                break
            current = row["superclass"]
            chain.append(current)
        return chain

    def find_callers(self, target: str, limit: int = 50) -> dict:
        target = (target or "").strip()
        if not target:
            return {"target": target, "callers": []}
        limit = max(1, min(int(limit), 300))
        if "." in target:
            cls, method = target.split(".", 1)
            rows = self.conn.execute(
                """
                SELECT * FROM calls
                WHERE callee_class = ? AND (callee_name = ? OR callee_name LIKE ?)
                ORDER BY caller_class, line_number LIMIT ?
                """,
                (cls, method, method.split(":")[0] + "%", limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT * FROM calls
                WHERE callee_class = ? OR callee_name = ? OR callee_name LIKE ?
                ORDER BY caller_class, line_number LIMIT ?
                """,
                (target, target, target.split(":")[0] + "%", limit),
            ).fetchall()
        callers = [
            {
                "caller_class": r["caller_class"],
                "caller_method": r["caller_method"],
                "callee_class": r["callee_class"],
                "callee_name": r["callee_name"],
                "kind": r["kind"],
                "confidence": r["confidence"],
                "file_path": r["file_path"],
                "line": r["line_number"],
            }
            for r in rows
        ]
        return {"target": target, "count": len(callers), "callers": callers}

    def impact_analysis(self, target: str, depth: int = 2, limit: int = 200) -> dict:
        """反向 BFS：改这个类/方法会影响谁。"""
        target = (target or "").strip()
        if not target:
            return {"target": target, "levels": []}
        depth = max(1, min(int(depth), 4))
        limit = max(1, min(int(limit), 500))

        start_classes: set[str] = set()
        method_target = ""
        if "." in target:
            cls, method_target = target.split(".", 1)
            start_classes.add(cls)
        else:
            start_classes.add(target)

        visited: set[str] = set(start_classes)
        levels: list[dict] = []
        frontier = sorted(start_classes)
        for level in range(1, depth + 1):
            next_frontier: list[str] = []
            items: list[dict] = []
            for cls in frontier:
                if len(visited) >= limit:
                    break
                reasons = self._reverse_reasons(cls, method_target if level == 1 else "")
                for reason in reasons:
                    source = reason["class_name"]
                    if not source or source in visited:
                        continue
                    visited.add(source)
                    next_frontier.append(source)
                    row = self.conn.execute(
                        "SELECT module, file_path, is_view_controller, kind FROM classes "
                        "WHERE class_name = ?", (source,)
                    ).fetchone()
                    items.append(
                        {
                            "class_name": source,
                            "module": row["module"] if row else "",
                            "file_path": row["file_path"] if row else reason.get("file_path", ""),
                            "is_view_controller": bool(row["is_view_controller"]) if row else False,
                            "kind": row["kind"] if row else "",
                            "via": reason["via"],
                            "detail": reason.get("detail", ""),
                        }
                    )
            if not items:
                break
            levels.append({"depth": level, "items": items[:limit]})
            frontier = next_frontier
            if len(visited) >= limit:
                break
        return {
            "target": target,
            "depth": depth,
            "affected_count": len(visited) - len(start_classes),
            "levels": levels,
            "truncated": len(visited) >= limit,
        }

    def _reverse_reasons(self, class_name: str, method: str) -> list[dict]:
        reasons: list[dict] = []
        if method:
            rows = self.conn.execute(
                "SELECT * FROM calls WHERE callee_class = ? AND callee_name LIKE ?",
                (class_name, method.split(":")[0] + "%"),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM calls WHERE callee_class = ?", (class_name,)
            ).fetchall()
        for row in rows:
            reasons.append(
                {
                    "class_name": row["caller_class"],
                    "via": "call",
                    "file_path": row["file_path"],
                    "detail": f'{row["caller_method"]} -> {row["callee_class"]}.{row["callee_name"]}',
                }
            )
        for row in self.conn.execute(
            "SELECT * FROM navigation WHERE to_class = ?", (class_name,)
        ):
            reasons.append(
                {
                    "class_name": row["from_class"],
                    "via": "navigation",
                    "file_path": row["file_path"],
                    "detail": f'{row["kind"]} {row["identifier"]}',
                }
            )
        for row in self.conn.execute(
            "SELECT * FROM deps WHERE dep_class = ? AND from_class != '<file>'",
            (class_name,),
        ):
            reasons.append(
                {
                    "class_name": row["from_class"],
                    "via": f'dep:{row["dep_kind"]}',
                    "file_path": row["file_path"],
                    "detail": f'{row["from_class"]} depends on {class_name}',
                }
            )
        return reasons

    # ------------------------------------------------------------------
    # 方法 / 页面 / 模块
    # ------------------------------------------------------------------
    def search_methods(
        self,
        query: str,
        class_name: str = "",
        module: str = "",
        limit: int = 20,
    ) -> dict:
        limit = max(1, min(int(limit), 100))
        filters: list[str] = []
        params: list[Any] = []
        if class_name:
            filters.append("m.class_name = ?")
            params.append(class_name)
        if module:
            filters.append("m.module = ?")
            params.append(module)
        where = (" AND " + " AND ".join(filters)) if filters else ""

        rows: Iterable[sqlite3.Row] = []
        query = (query or "").strip()
        if query:
            for op in ("AND", "OR"):
                expr = fts_expr(query, op)
                if not expr:
                    break
                rows = self.conn.execute(
                    f"""
                    SELECT m.*, bm25(methods_fts) AS rank
                    FROM methods_fts f JOIN methods m ON m.id = f.rowid
                    WHERE methods_fts MATCH ? {where}
                    ORDER BY rank LIMIT ?
                    """,
                    (expr, *params, limit),
                ).fetchall()
                if rows:
                    break
        else:
            rows = self.conn.execute(
                f"SELECT m.*, 0 AS rank FROM methods m WHERE 1=1 {where} "
                "ORDER BY m.class_name, m.line_start LIMIT ?",
                (*params, limit),
            ).fetchall()

        results = []
        for row in rows:
            item = _method_row(row)
            item["score"] = round(-row["rank"], 3) if "rank" in row.keys() and row["rank"] else 0
            item["snippet"] = snippet(row["body_preview"] or row["doc_comment"], query)
            results.append(item)
        return {"query": query, "filters": {"class_name": class_name, "module": module},
                "results": results}

    # ------------------------------------------------------------------
    # 页面导航 / 通知
    # ------------------------------------------------------------------
    def _find_class(self, name: str) -> sqlite3.Row | None:
        row = self.conn.execute(
            "SELECT * FROM classes WHERE class_name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        if row is not None:
            return row
        return self.conn.execute(
            "SELECT * FROM classes WHERE class_name LIKE ? COLLATE NOCASE "
            "ORDER BY LENGTH(class_name) LIMIT 1",
            (f"%{name}",),
        ).fetchone()

    def find_navigation(
        self,
        target: str,
        depth: int = 6,
        limit: int = 20,
        include_weak: bool = True,
    ) -> dict:
        """从入口到目标页面的跳转链。

        强边来自 ``navigation``（push/present/root/segue）；弱边来自
        ``deps`` 的 ``instantiate``（tab 容器里 ``let vc = FooVC()`` 这类），
        弱边标 ``confidence=low``，不会伪装成确定的跳转。
        """
        target = (target or "").strip()
        depth = max(1, min(int(depth), 12))
        limit = max(1, min(int(limit), 50))
        empty = {
            "target": target, "found": False, "reachable": False, "entrypoints": [],
            "roots": [], "paths": [], "incoming": [], "outgoing": [], "weak_edges": 0,
        }
        if not target:
            return empty
        cls = self._find_class(target)
        if cls is None:
            return empty
        name = cls["class_name"]

        vc_names = {
            row["class_name"]
            for row in self.conn.execute(
                "SELECT class_name FROM classes WHERE is_view_controller = 1"
            )
        }
        entries = self.conn.execute(
            "SELECT * FROM entrypoints WHERE class_name != ''"
        ).fetchall()
        entry_names = {row["class_name"] for row in entries}

        edges: list[dict] = []
        seen: set[tuple] = set()

        def add_edge(
            from_class: str, to_class: str, kind: str, identifier: str,
            module: str, file_path: str, line: int, confidence: str,
        ) -> None:
            key = (from_class, to_class, kind, file_path, line)
            if not from_class or not to_class or key in seen:
                return
            seen.add(key)
            edges.append(
                {
                    "from_class": from_class, "to_class": to_class, "kind": kind,
                    "identifier": identifier, "module": module,
                    "file_path": file_path, "line_number": line,
                    "confidence": confidence,
                }
            )

        for row in self.conn.execute(
            "SELECT from_class, to_class, kind, identifier, module, file_path, line_number "
            "FROM navigation WHERE from_class != '' AND to_class != ''"
        ):
            add_edge(row["from_class"], row["to_class"], row["kind"], row["identifier"],
                     row["module"], row["file_path"], row["line_number"], "high")

        weak_edges = 0
        if include_weak:
            for row in self.conn.execute(
                "SELECT from_class, dep_class, file_path, line_number FROM deps "
                "WHERE dep_kind = 'instantiate'"
            ):
                dep = row["dep_class"]
                if dep not in vc_names or dep == row["from_class"]:
                    continue
                if row["from_class"] not in vc_names and row["from_class"] not in entry_names:
                    continue
                add_edge(row["from_class"], dep, "instantiate", "", "", row["file_path"],
                         row["line_number"], "low")

            # 同一对 (from, to) 已有明确跳转时，不再重复记弱边
            strong_pairs = {
                (edge["from_class"], edge["to_class"])
                for edge in edges if edge["confidence"] == "high"
            }
            edges = [
                edge for edge in edges
                if edge["confidence"] != "low"
                or (edge["from_class"], edge["to_class"]) not in strong_pairs
            ]
            weak_edges = sum(1 for edge in edges if edge["confidence"] == "low")

        incoming: dict[str, list[dict]] = {}
        outgoing: dict[str, list[dict]] = {}
        for edge in edges:
            outgoing.setdefault(edge["from_class"], []).append(edge)
            incoming.setdefault(edge["to_class"], []).append(edge)

        paths: list[dict] = []
        budget = {"n": 0}

        def walk(node: str, chain: list[dict], visited: set[str]) -> None:
            if budget["n"] > 4000 or len(paths) >= limit * 4:
                return
            budget["n"] += 1
            if node in entry_names:
                paths.append(
                    {"root": node, "hops": len(chain), "chain": list(reversed(chain))}
                )
                return
            if len(chain) >= depth:
                return
            for edge in incoming.get(node, []):
                prev = edge["from_class"]
                if prev in visited:
                    continue
                visited.add(prev)
                chain.append(edge)
                walk(prev, chain, visited)
                chain.pop()
                visited.discard(prev)

        walk(name, [], {name})
        paths.sort(key=lambda item: (item["hops"], item["root"]))

        unique: list[dict] = []
        seen_paths: set[tuple] = set()
        for path in paths:
            # 同一条逻辑链只留一条：rootViewController 的创建与赋值可能被解析成
            # 两条同 (from, to, kind) 的边，行号不同但对「怎么到这个页面」没区别。
            key = tuple(
                (e["from_class"], e["to_class"], e["kind"], e["file_path"])
                for e in path["chain"]
            )
            if key in seen_paths:
                continue
            seen_paths.add(key)
            unique.append(path)
            if len(unique) >= limit:
                break

        target_entries = [
            {
                "kind": row["kind"], "class_name": row["class_name"],
                "identifier": row["identifier"], "source": row["source"],
                "file_path": row["file_path"], "line_number": row["line_number"],
            }
            for row in entries
            if row["class_name"] == name
        ]

        return {
            "target": name,
            "module": cls["module"],
            "is_view_controller": bool(cls["is_view_controller"]),
            "found": True,
            "reachable": bool(unique),
            "roots": sorted({path["root"] for path in unique}),
            "entrypoints": target_entries,
            "paths": unique,
            "incoming": incoming.get(name, []),
            "outgoing": outgoing.get(name, []),
            "weak_edges": weak_edges,
            "note": "" if unique else (
                "未从已知入口连通：可能是 tab/容器创建页面的写法未被解析，"
                "先看 incoming 里的直接来源"
            ),
        }

    def find_notifications(
        self, name: str = "", module: str = "", balanced: int | None = None,
        limit: int = 100,
    ) -> dict:
        """通知注册/移除查询。``balanced``：0 未配对，1 已配对，-1 无法判定。"""
        name = (name or "").strip()
        limit = max(1, min(int(limit), 500))
        filters: list[str] = []
        params: list[Any] = []
        if name:
            pattern = like_pattern(name)
            filters.append("(notification_name LIKE ? ESCAPE '\\' "
                           "OR class_name LIKE ? ESCAPE '\\')")
            params.extend([pattern, pattern])
        if module:
            filters.append("module = ?")
            params.append(module)
        if balanced is not None:
            filters.append("is_balanced = ?")
            params.append(int(balanced))
        where = (" WHERE " + " AND ".join(filters)) if filters else ""

        rows = self.conn.execute(
            "SELECT * FROM notifications" + where
            + " ORDER BY CASE is_balanced WHEN 0 THEN 0 WHEN -1 THEN 1 ELSE 2 END,"
              " notification_name, class_name LIMIT ?",
            (*params, limit),
        ).fetchall()

        summary = {"total": 0, "balanced": 0, "unbalanced": 0, "unknown": 0}
        for row in self.conn.execute(
            "SELECT is_balanced, COUNT(*) AS n FROM notifications" + where
            + " GROUP BY is_balanced",
            params,
        ):
            count = int(row["n"])
            summary["total"] += count
            if row["is_balanced"] == 1:
                summary["balanced"] = count
            elif row["is_balanced"] == 0:
                summary["unbalanced"] = count
            else:
                summary["unknown"] = count

        results = [
            {
                "class_name": row["class_name"],
                "notification_name": row["notification_name"],
                "observer_selector": row["observer_selector"],
                "register_method": row["register_method"],
                "register_line": row["register_line"],
                "remove_method": row["remove_method"],
                "remove_line": row["remove_line"],
                "is_balanced": row["is_balanced"],
                "module": row["module"],
                "language": row["language"],
                "file_path": row["file_path"],
                "line": row["register_line"],
            }
            for row in rows
        ]
        return {
            "query": name,
            "filter": {"module": module, "balanced": balanced},
            "summary": summary,
            "results": results,
        }

    def list_pages(self, module: str = "", limit: int = 100) -> dict:
        limit = max(1, min(int(limit), 500))
        if module:
            rows = self.conn.execute(
                "SELECT * FROM classes WHERE is_view_controller=1 AND module=? "
                "ORDER BY module, class_name LIMIT ?",
                (module, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM classes WHERE is_view_controller=1 "
                "ORDER BY module, class_name LIMIT ?",
                (limit,),
            ).fetchall()
        pages = []
        for row in rows:
            pages.append(
                {
                    "class_name": row["class_name"],
                    "module": row["module"],
                    "vc_base": row["vc_base"],
                    "file_path": row["impl_path"] or row["file_path"],
                    "method_count": row["method_count"],
                    "doc_comment": row["doc_comment"],
                }
            )
        return {"count": len(pages), "pages": pages}

    def list_module(self, module: str) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*) AS classes, SUM(method_count) AS methods, "
            "SUM(is_view_controller) AS vcs FROM classes WHERE module = ?",
            (module,),
        ).fetchone()
        files = self.conn.execute(
            "SELECT language, COUNT(*) AS n, SUM(loc) AS loc FROM files WHERE module=? "
            "GROUP BY language", (module,)
        ).fetchall()
        top = self.conn.execute(
            "SELECT class_name, method_count, impl_path FROM classes WHERE module=? "
            "ORDER BY method_count DESC LIMIT 15", (module,)
        ).fetchall()
        return {
            "module": module,
            "classes": row["classes"] or 0,
            "methods": row["methods"] or 0,
            "view_controllers": row["vcs"] or 0,
            "files": [dict(f) for f in files],
            "top_classes": [dict(t) for t in top],
        }

    # ------------------------------------------------------------------
    # 源码读取
    # ------------------------------------------------------------------
    def read_source(self, file_path: str, focus: str = "", context: int = 40) -> dict:
        path, rel = self._resolve_path(file_path)
        if path is None:
            return {"found": False, "error": f"文件不在项目内或不存在: {file_path}"}
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        total = len(lines)
        focus = (focus or "").strip()
        start = 1
        end = min(total, 400)
        matched = False
        if focus:
            for idx, line in enumerate(lines, 1):
                if focus.lower() in line.lower():
                    start = max(1, idx - context)
                    end = min(total, idx + context)
                    matched = True
                    break
            if not matched:
                start, end = 1, min(total, 80)
        content = "\n".join(f"{i:5d}| {lines[i - 1]}" for i in range(start, end + 1))
        result = {
            "found": True,
            "file": rel,
            "focus": focus,
            "focus_matched": matched,
            "line_start": start,
            "line_end": end,
            "total_lines": total,
            "content": content,
        }
        if focus and not matched:
            result["note"] = "未找到 focus，已返回文件开头"
        return result

    def _resolve_path(self, raw: str) -> tuple[Path | None, str]:
        raw = (raw or "").strip()
        if not raw:
            return None, ""
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.profile.root / candidate
        try:
            resolved = candidate.resolve()
            resolved.relative_to(self.profile.root.resolve())
        except (OSError, ValueError):
            # 允许只给文件名/后缀，从 files 表反查
            rows = self.conn.execute(
                "SELECT path FROM files WHERE path LIKE ? LIMIT 2",
                (f"%{raw}",),
            ).fetchall()
            if len(rows) == 1:
                rel = rows[0]["path"]
                return (self.profile.root / rel).resolve(), rel
            return None, ""
        if not resolved.exists():
            return None, ""
        rel = resolved.relative_to(self.profile.root.resolve()).as_posix()
        return resolved, rel

    # ------------------------------------------------------------------
    # Q&A
    # ------------------------------------------------------------------
    def search_qa(
        self,
        query: str,
        related_class: str = "",
        limit: int = 10,
    ) -> dict:
        limit = max(1, min(int(limit), 50))
        query = (query or "").strip()
        rows: list[sqlite3.Row] = []
        if query:
            for op in ("AND", "OR"):
                expr = fts_expr(query, op)
                if not expr:
                    break
                sql = (
                    "SELECT q.*, bm25(qa_fts) AS rank FROM qa_fts f "
                    "JOIN qa_history q ON q.id = f.rowid WHERE qa_fts MATCH ?"
                )
                params: list[Any] = [expr]
                if related_class:
                    sql += " AND q.related_class = ?"
                    params.append(related_class)
                sql += " ORDER BY rank LIMIT ?"
                params.append(limit)
                rows = self.conn.execute(sql, params).fetchall()
                if rows:
                    break
            if not rows:
                pattern = like_pattern(query)
                sql = (
                    "SELECT q.*, 0 AS rank FROM qa_history q WHERE "
                    "(q.question LIKE ? ESCAPE '\\' OR q.answer LIKE ? ESCAPE '\\' "
                    "OR q.tags_json LIKE ? ESCAPE '\\')"
                )
                params = [pattern, pattern, pattern]
                if related_class:
                    sql += " AND q.related_class = ?"
                    params.append(related_class)
                sql += " ORDER BY q.id DESC LIMIT ?"
                params.append(limit)
                rows = self.conn.execute(sql, params).fetchall()
        else:
            sql = "SELECT q.*, 0 AS rank FROM qa_history q"
            params = []
            if related_class:
                sql += " WHERE q.related_class = ?"
                params.append(related_class)
            sql += " ORDER BY q.id DESC LIMIT ?"
            params.append(limit)
            rows = self.conn.execute(sql, params).fetchall()

        results = []
        for row in rows:
            results.append(
                {
                    "id": row["id"],
                    "question": row["question"],
                    "answer": row["answer"],
                    "related_class": row["related_class"],
                    "related_module": row["related_module"],
                    "related_file": row["related_file"],
                    "tags": _json_list(row["tags_json"]),
                    "author": row["author"],
                    "created_at": row["created_at"],
                }
            )
        return {"query": query, "results": results}

    def save_qa(
        self,
        question: str,
        answer: str,
        related_class: str = "",
        related_module: str = "",
        related_file: str = "",
        tags: str | list = "",
        author: str = "",
    ) -> dict:
        if not question.strip() or not answer.strip():
            return {"ok": False, "error": "question / answer 不能为空"}
        if isinstance(tags, str):
            tag_list = [t.strip() for t in re.split(r"[,，\s]+", tags) if t.strip()]
        else:
            tag_list = [str(t).strip() for t in tags if str(t).strip()]
        search_text = build_search_text(question, answer, " ".join(tag_list), related_class)
        cursor = self.conn.execute(
            """
            INSERT INTO qa_history(question, answer, related_class, related_module,
                                   related_file, tags_json, author, search_text)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                question.strip(), answer.strip(), related_class, related_module,
                related_file, json.dumps(tag_list, ensure_ascii=False), author,
                search_text,
            ),
        )
        qa_id = cursor.lastrowid
        self.conn.execute(
            "INSERT INTO qa_fts(rowid, qa_id, search_text) VALUES (?,?,?)",
            (qa_id, qa_id, search_text),
        )
        self.conn.commit()
        return {"ok": True, "id": qa_id}

    def delete_qa(self, ids: list[int]) -> dict:
        deleted = 0
        for qa_id in ids or []:
            self.conn.execute("DELETE FROM qa_history WHERE id = ?", (int(qa_id),))
            self.conn.execute("DELETE FROM qa_fts WHERE rowid = ?", (int(qa_id),))
            deleted += 1
        self.conn.commit()
        return {"ok": True, "deleted": deleted}


def _dedup_dicts(items: Iterable[dict], keys: tuple[str, ...]) -> list[dict]:
    seen: set[tuple] = set()
    out: list[dict] = []
    for item in items:
        key = tuple(item.get(k, "") for k in keys)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _git(root: Path, *args: str) -> str:
    import subprocess

    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=3, check=False,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
