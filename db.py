#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite 存储层：DDL、增量写入、类级聚合、FTS 重建。

表分两类：

- **文件级事实表**（symbols/methods/deps/api_calls/notifications/calls/
  entrypoints/navigation）：按 ``file_path`` 增量删除/写入；
- **聚合表**（classes）：每次构建从事实表重建，查询走它，避免跨文件合并逻辑
  散落在每个查询里。

中文检索不依赖 FTS5 的分词器：写入前由 :mod:`ios_qa.textnorm` 做 CJK bigram
预分词，FTS5 只负责倒排。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from . import SCHEMA_VERSION
from .config import ProjectProfile
from .model import ParseResult
from .textnorm import build_search_text

DDL = """
CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS files (
    path       TEXT PRIMARY KEY,
    module     TEXT DEFAULT '',
    language   TEXT DEFAULT '',
    size       INTEGER DEFAULT 0,
    mtime      REAL DEFAULT 0,
    sha256     TEXT DEFAULT '',
    loc        INTEGER DEFAULT 0,
    parsed_at  TEXT DEFAULT '',
    status     TEXT DEFAULT 'ok',
    error      TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS symbols (
    symbol_key     TEXT PRIMARY KEY,
    class_name     TEXT NOT NULL,
    kind           TEXT DEFAULT '',
    language       TEXT DEFAULT '',
    module         TEXT DEFAULT '',
    file_path      TEXT DEFAULT '',
    line_start     INTEGER DEFAULT 0,
    line_end       INTEGER DEFAULT 0,
    superclass     TEXT DEFAULT '',
    protocols_json TEXT DEFAULT '[]',
    properties_json TEXT DEFAULT '[]',
    doc_comment    TEXT DEFAULT '',
    interface_text TEXT DEFAULT '',
    extension_of   TEXT DEFAULT '',
    category_name  TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_symbols_class ON symbols(class_name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);

CREATE TABLE IF NOT EXISTS methods (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_key      TEXT DEFAULT '',
    class_name      TEXT NOT NULL,
    method_name     TEXT NOT NULL,
    signature       TEXT DEFAULT '',
    language        TEXT DEFAULT '',
    module          TEXT DEFAULT '',
    file_path       TEXT DEFAULT '',
    line_start      INTEGER DEFAULT 0,
    line_end        INTEGER DEFAULT 0,
    is_class_method INTEGER DEFAULT 0,
    is_extension    INTEGER DEFAULT 0,
    has_body        INTEGER DEFAULT 1,
    doc_comment     TEXT DEFAULT '',
    body            TEXT DEFAULT '',
    body_preview    TEXT DEFAULT '',
    body_hash       TEXT DEFAULT '',
    search_text     TEXT DEFAULT '',
    UNIQUE(file_path, line_start, method_name)
);
CREATE INDEX IF NOT EXISTS idx_methods_class ON methods(class_name, method_name);
CREATE INDEX IF NOT EXISTS idx_methods_file ON methods(file_path);
CREATE INDEX IF NOT EXISTS idx_methods_module ON methods(module);

CREATE TABLE IF NOT EXISTS deps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    from_class  TEXT NOT NULL,
    dep_class   TEXT NOT NULL,
    dep_kind    TEXT NOT NULL,
    module      TEXT DEFAULT '',
    file_path   TEXT DEFAULT '',
    line_number INTEGER DEFAULT 0,
    UNIQUE(from_class, dep_class, dep_kind, file_path, line_number)
);
CREATE INDEX IF NOT EXISTS idx_deps_from ON deps(from_class);
CREATE INDEX IF NOT EXISTS idx_deps_dep ON deps(dep_class);
CREATE INDEX IF NOT EXISTS idx_deps_file ON deps(file_path);

CREATE TABLE IF NOT EXISTS api_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    class_name      TEXT NOT NULL,
    method_name     TEXT DEFAULT '',
    api_kind        TEXT DEFAULT '',
    url_pattern     TEXT DEFAULT '',
    http_method     TEXT DEFAULT '',
    response_type   TEXT DEFAULT '',
    context_snippet TEXT DEFAULT '',
    module          TEXT DEFAULT '',
    file_path       TEXT DEFAULT '',
    line_number     INTEGER DEFAULT 0,
    UNIQUE(class_name, file_path, line_number, api_kind, url_pattern)
);
CREATE INDEX IF NOT EXISTS idx_api_class ON api_calls(class_name);
CREATE INDEX IF NOT EXISTS idx_api_url ON api_calls(url_pattern);
CREATE INDEX IF NOT EXISTS idx_api_file ON api_calls(file_path);

CREATE TABLE IF NOT EXISTS notifications (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    class_name        TEXT NOT NULL,
    notification_name TEXT NOT NULL,
    observer_selector TEXT DEFAULT '',
    register_method   TEXT DEFAULT '',
    register_line     INTEGER DEFAULT 0,
    remove_method     TEXT DEFAULT '',
    remove_line       INTEGER DEFAULT 0,
    file_path         TEXT DEFAULT '',
    module            TEXT DEFAULT '',
    language          TEXT DEFAULT '',
    is_balanced       INTEGER DEFAULT 0,
    UNIQUE(class_name, file_path, register_line, notification_name, observer_selector)
);
CREATE INDEX IF NOT EXISTS idx_notif_class ON notifications(class_name);
CREATE INDEX IF NOT EXISTS idx_notif_name ON notifications(notification_name);
CREATE INDEX IF NOT EXISTS idx_notif_balanced ON notifications(is_balanced);
CREATE INDEX IF NOT EXISTS idx_notif_file ON notifications(file_path);

CREATE TABLE IF NOT EXISTS calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_class  TEXT NOT NULL,
    caller_method TEXT DEFAULT '',
    callee_class  TEXT DEFAULT '',
    callee_name   TEXT DEFAULT '',
    kind          TEXT DEFAULT '',
    confidence    TEXT DEFAULT 'medium',
    module        TEXT DEFAULT '',
    file_path     TEXT DEFAULT '',
    line_number   INTEGER DEFAULT 0,
    UNIQUE(caller_class, caller_method, callee_class, callee_name, file_path, line_number)
);
CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller_class);
CREATE INDEX IF NOT EXISTS idx_calls_callee ON calls(callee_class, callee_name);
CREATE INDEX IF NOT EXISTS idx_calls_file ON calls(file_path);

CREATE TABLE IF NOT EXISTS entrypoints (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT DEFAULT '',
    class_name  TEXT DEFAULT '',
    identifier  TEXT DEFAULT '',
    source      TEXT DEFAULT '',
    module      TEXT DEFAULT '',
    file_path   TEXT DEFAULT '',
    line_number INTEGER DEFAULT 0,
    confidence  TEXT DEFAULT 'high',
    UNIQUE(file_path, kind, class_name, identifier)
);
CREATE INDEX IF NOT EXISTS idx_entry_class ON entrypoints(class_name);
CREATE INDEX IF NOT EXISTS idx_entry_file ON entrypoints(file_path);

CREATE TABLE IF NOT EXISTS navigation (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    from_class  TEXT DEFAULT '',
    to_class    TEXT DEFAULT '',
    kind        TEXT DEFAULT '',
    identifier  TEXT DEFAULT '',
    module      TEXT DEFAULT '',
    file_path   TEXT DEFAULT '',
    line_number INTEGER DEFAULT 0,
    UNIQUE(from_class, to_class, kind, file_path, line_number)
);
CREATE INDEX IF NOT EXISTS idx_nav_from ON navigation(from_class);
CREATE INDEX IF NOT EXISTS idx_nav_to ON navigation(to_class);
CREATE INDEX IF NOT EXISTS idx_nav_file ON navigation(file_path);

CREATE TABLE IF NOT EXISTS classes (
    class_name      TEXT PRIMARY KEY,
    kind            TEXT DEFAULT '',
    language        TEXT DEFAULT '',
    module          TEXT DEFAULT '',
    modules_json    TEXT DEFAULT '[]',
    superclass      TEXT DEFAULT '',
    protocols_json  TEXT DEFAULT '[]',
    properties_json TEXT DEFAULT '[]',
    method_count    INTEGER DEFAULT 0,
    method_names    TEXT DEFAULT '',
    extension_count INTEGER DEFAULT 0,
    header_path     TEXT DEFAULT '',
    impl_path       TEXT DEFAULT '',
    file_path       TEXT DEFAULT '',
    doc_comment     TEXT DEFAULT '',
    interface_text  TEXT DEFAULT '',
    is_view_controller INTEGER DEFAULT 0,
    vc_base         TEXT DEFAULT '',
    is_app_entry    INTEGER DEFAULT 0,
    line_start      INTEGER DEFAULT 0,
    line_end        INTEGER DEFAULT 0,
    search_text     TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_classes_module ON classes(module);
CREATE INDEX IF NOT EXISTS idx_classes_vc ON classes(is_view_controller);

CREATE TABLE IF NOT EXISTS qa_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    question       TEXT NOT NULL,
    answer         TEXT NOT NULL,
    related_class  TEXT DEFAULT '',
    related_module TEXT DEFAULT '',
    related_file   TEXT DEFAULT '',
    tags_json      TEXT DEFAULT '[]',
    author         TEXT DEFAULT '',
    created_at     TEXT DEFAULT (datetime('now')),
    search_text    TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS build_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT DEFAULT '',
    finished_at TEXT DEFAULT '',
    mode        TEXT DEFAULT '',
    duration_s  REAL DEFAULT 0,
    files_total INTEGER DEFAULT 0,
    files_parsed INTEGER DEFAULT 0,
    files_skipped INTEGER DEFAULT 0,
    files_deleted INTEGER DEFAULT 0,
    symbols     INTEGER DEFAULT 0,
    methods     INTEGER DEFAULT 0,
    classes     INTEGER DEFAULT 0,
    errors      INTEGER DEFAULT 0,
    error_json  TEXT DEFAULT '[]',
    git_branch  TEXT DEFAULT '',
    git_commit  TEXT DEFAULT ''
);

CREATE VIRTUAL TABLE IF NOT EXISTS classes_fts USING fts5(
    class_name UNINDEXED,
    search_text,
    tokenize='unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS methods_fts USING fts5(
    method_id UNINDEXED,
    search_text,
    tokenize='unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS qa_fts USING fts5(
    qa_id UNINDEXED,
    search_text,
    tokenize='unicode61'
);
"""

FILE_SCOPED_TABLES = (
    "symbols",
    "methods",
    "deps",
    "api_calls",
    "notifications",
    "calls",
    "entrypoints",
    "navigation",
)


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


def reset_all(conn: sqlite3.Connection) -> None:
    for table in (
        "symbols", "methods", "deps", "api_calls", "notifications", "calls",
        "entrypoints", "navigation", "classes", "files", "build_runs",
        "classes_fts", "methods_fts",
    ):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _body_hash(body: str) -> str:
    return hashlib.sha1(body.encode("utf-8", errors="replace")).hexdigest()[:20]


def delete_file(conn: sqlite3.Connection, rel_path: str) -> None:
    for table in FILE_SCOPED_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE file_path = ?", (rel_path,))


def write_result(
    conn: sqlite3.Connection,
    rel_path: str,
    result: ParseResult,
    profile: ProjectProfile,
) -> int:
    """写入单文件的解析结果（调用方负责事务与删除旧行）。"""
    for symbol in result.symbols:
        conn.execute(
            """
            INSERT OR REPLACE INTO symbols(
                symbol_key, class_name, kind, language, module, file_path,
                line_start, line_end, superclass, protocols_json,
                properties_json, doc_comment, interface_text, extension_of,
                category_name
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                symbol.symbol_key, symbol.class_name, symbol.kind, symbol.language,
                symbol.module, symbol.file_path, symbol.line_start, symbol.line_end,
                symbol.superclass, _json(symbol.protocols), _json(symbol.properties),
                symbol.doc_comment, symbol.interface_text, symbol.extension_of,
                symbol.category_name,
            ),
        )

    for method in result.methods:
        body = method.body or ""
        preview = reflow_preview(body, method.signature)
        search_text = build_search_text(
            method.class_name,
            method.method_name,
            method.signature,
            method.doc_comment,
            body[: profile.max_fts_body_chars],
            method.module,
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO methods(
                symbol_key, class_name, method_name, signature, language, module,
                file_path, line_start, line_end, is_class_method, is_extension,
                has_body, doc_comment, body, body_preview, body_hash, search_text
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                method.symbol_key, method.class_name, method.method_name,
                method.signature, method.language, method.module, method.file_path,
                method.line_start, method.line_end, int(method.is_class_method),
                int(method.is_extension), int(method.has_body), method.doc_comment,
                body, preview, _body_hash(body), search_text,
            ),
        )

    for dep in result.deps:
        conn.execute(
            """
            INSERT OR IGNORE INTO deps(
                from_class, dep_class, dep_kind, module, file_path, line_number
            ) VALUES (?,?,?,?,?,?)
            """,
            (dep.from_class, dep.dep_class, dep.dep_kind, dep.module,
             dep.file_path, dep.line_number),
        )

    for call in result.api_calls:
        conn.execute(
            """
            INSERT OR IGNORE INTO api_calls(
                class_name, method_name, api_kind, url_pattern, http_method,
                response_type, context_snippet, module, file_path, line_number
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                call.class_name, call.method_name, call.api_kind, call.url_pattern,
                call.http_method, call.response_type, call.context_snippet,
                call.module, call.file_path, call.line_number,
            ),
        )

    for notif in result.notifications:
        conn.execute(
            """
            INSERT OR IGNORE INTO notifications(
                class_name, notification_name, observer_selector, register_method,
                register_line, remove_method, remove_line, file_path, module,
                language, is_balanced
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                notif.class_name, notif.notification_name, notif.observer_selector,
                notif.register_method, notif.register_line, notif.remove_method,
                notif.remove_line, notif.file_path, notif.module, notif.language,
                notif.is_balanced,
            ),
        )

    for call in result.calls:
        conn.execute(
            """
            INSERT OR IGNORE INTO calls(
                caller_class, caller_method, callee_class, callee_name, kind,
                confidence, module, file_path, line_number
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                call.caller_class, call.caller_method, call.callee_class,
                call.callee_name, call.kind, call.confidence, call.module,
                call.file_path, call.line_number,
            ),
        )

    for entry in result.entrypoints:
        conn.execute(
            """
            INSERT OR IGNORE INTO entrypoints(
                kind, class_name, identifier, source, module, file_path,
                line_number, confidence
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                entry.kind, entry.class_name, entry.identifier, entry.source,
                entry.module, entry.file_path, entry.line_number, entry.confidence,
            ),
        )

    for nav in result.navigation:
        conn.execute(
            """
            INSERT OR IGNORE INTO navigation(
                from_class, to_class, kind, identifier, module, file_path, line_number
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                nav.from_class, nav.to_class, nav.kind, nav.identifier, nav.module,
                nav.file_path, nav.line_number,
            ),
        )

    return len(result.symbols) + len(result.methods)


def reflow_preview(body: str, signature: str) -> str:
    text = body.strip() if body else signature
    text = " ".join(line.strip() for line in text.splitlines() if line.strip())
    return text[:320]


def rebuild_classes(conn: sqlite3.Connection, profile: ProjectProfile) -> int:
    """从 symbols/methods 重建 classes 聚合表，并刷新全部 FTS。"""
    symbols = conn.execute(
        "SELECT * FROM symbols ORDER BY class_name, line_start"
    ).fetchall()
    methods_by_class: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute(
        "SELECT class_name, method_name, signature, has_body FROM methods "
        "ORDER BY class_name, method_name"
    ):
        methods_by_class.setdefault(row["class_name"], []).append(row)

    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in symbols:
        grouped.setdefault(row["class_name"], []).append(row)

    # 超类映射：优先 class/actor/struct 声明
    superclass_map: dict[str, str] = {}
    kind_priority = {"class": 0, "actor": 1, "struct": 2, "enum": 3, "protocol": 4}
    for class_name, rows in grouped.items():
        best = sorted(
            (r for r in rows if r["kind"] in kind_priority),
            key=lambda r: (kind_priority.get(r["kind"], 9), 0 if r["superclass"] else 1),
        )
        for row in best:
            if row["superclass"]:
                superclass_map[class_name] = row["superclass"]
                break

    def is_view_controller(class_name: str) -> tuple[int, str]:
        seen: set[str] = set()
        current = class_name
        while current and current not in seen:
            seen.add(current)
            if current in profile.view_controller_bases:
                return 1, current
            parent = superclass_map.get(current, "")
            if not parent:
                break
            current = parent
        if class_name.endswith(("ViewController", "Controller")) and any(
            row["language"] == "objc" for row in grouped.get(class_name, [])
        ):
            return 1, "name_heuristic"
        return 0, ""

    rows_out: list[tuple] = []
    for class_name, rows in grouped.items():
        if any(r["kind"] == "storyboard" for r in rows) and all(
            r["kind"] == "storyboard" for r in rows
        ):
            continue
        base = sorted(
            rows,
            key=lambda r: (
                0 if r["kind"] in {"class", "struct", "enum", "protocol", "actor"} else 1,
                0 if r["file_path"].endswith((".h", ".swift")) else 1,
                0 if r["superclass"] else 1,
                r["line_start"],
            ),
        )[0]
        protocols: list[str] = []
        properties: list[dict] = []
        categories: list[str] = []
        modules: set[str] = set()
        languages: set[str] = set()
        header_path = ""
        impl_path = ""
        extension_count = 0
        doc = ""
        for row in rows:
            modules.add(row["module"])
            if row["language"]:
                languages.add(row["language"])
            if row["category_name"] and row["category_name"] not in categories:
                categories.append(row["category_name"])
            for proto in json.loads(row["protocols_json"] or "[]"):
                if proto and proto not in protocols:
                    protocols.append(proto)
            for prop in json.loads(row["properties_json"] or "[]"):
                if prop and prop not in properties:
                    properties.append(prop)
            if row["file_path"].endswith(".h") and not header_path:
                header_path = row["file_path"]
            if row["file_path"].endswith((".m", ".mm", ".swift")) and not impl_path:
                impl_path = row["file_path"]
            if row["kind"] in {"category", "extension", "category_impl"}:
                extension_count += 1
            if not doc and row["doc_comment"]:
                doc = row["doc_comment"]
        method_rows = methods_by_class.get(class_name, [])
        method_names = [m["method_name"] for m in method_rows]
        body_method_count = sum(1 for m in method_rows if m["has_body"])
        primary_module = (
            "App" if "App" in modules else sorted(modules)[0] if modules else ""
        )
        vc_flag, vc_base = is_view_controller(class_name)
        is_app_entry = int(
            "UIApplicationDelegate" in protocols
            or "UIWindowSceneDelegate" in protocols
            or class_name in {"AppDelegate", "SceneDelegate"}
        )
        search_text = build_search_text(
            class_name,
            " ".join(modules),
            base["superclass"],
            " ".join(protocols),
            " ".join(categories),
            " ".join(f'{p.get("name", "")} {p.get("type", "")}' for p in properties),
            " ".join(method_names[:80]),
            doc,
            header_path,
            impl_path,
        )
        rows_out.append(
            (
                class_name,
                base["kind"],
                "swift" if "swift" in languages and "objc" not in languages
                else "objc" if "objc" in languages else base["language"],
                primary_module,
                _json(sorted(modules)),
                base["superclass"],
                _json(protocols),
                _json(properties),
                body_method_count,
                " ".join(dict.fromkeys(method_names))[:4000],
                extension_count,
                header_path,
                impl_path,
                base["file_path"],
                doc,
                base["interface_text"][:8000],
                vc_flag,
                vc_base,
                is_app_entry,
                base["line_start"],
                base["line_end"],
                search_text,
            )
        )

    conn.execute("DELETE FROM classes")
    conn.executemany(
        """
        INSERT INTO classes(
            class_name, kind, language, module, modules_json, superclass,
            protocols_json, properties_json, method_count, method_names,
            extension_count, header_path, impl_path, file_path, doc_comment,
            interface_text, is_view_controller, vc_base, is_app_entry,
            line_start, line_end, search_text
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        rows_out,
    )

    # FTS 全量重建（结构化索引规模下成本可接受；后续可换触发器增量）
    conn.execute("DELETE FROM classes_fts")
    conn.execute(
        "INSERT INTO classes_fts(rowid, class_name, search_text) "
        "SELECT rowid, class_name, search_text FROM classes"
    )
    conn.execute("DELETE FROM methods_fts")
    conn.execute(
        "INSERT INTO methods_fts(rowid, method_id, search_text) "
        "SELECT id, id, search_text FROM methods"
    )
    conn.execute("DELETE FROM qa_fts")
    conn.execute(
        "INSERT INTO qa_fts(rowid, qa_id, search_text) "
        "SELECT id, id, search_text FROM qa_history"
    )
    conn.commit()
    return len(rows_out)


def set_metadata(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    conn.executemany(
        "INSERT INTO metadata(key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        [(str(k), str(v)) for k, v in values.items()],
    )
    conn.commit()


def get_metadata(conn: sqlite3.Connection) -> dict[str, str]:
    return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM metadata")}


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    result: dict[str, int] = {}
    for table in (
        "files", "symbols", "classes", "methods", "deps", "api_calls",
        "notifications", "calls", "entrypoints", "navigation", "qa_history",
    ):
        result[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return result
