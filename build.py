#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""索引构建编排：发现 → 增量解析 → 落库 → 聚合 → 元数据。"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import SCHEMA_VERSION
from .config import ProjectProfile
from .db import (
    connect,
    counts,
    delete_file,
    ensure_schema,
    get_metadata,
    rebuild_classes,
    reset_all,
    set_metadata,
    write_result,
)
from .discover import SourceFile, discover, summarize
from .parsers import parse_file, read_text


@dataclass
class BuildReport:
    mode: str
    db_path: str
    project_root: str
    duration_s: float = 0.0
    files_total: int = 0
    files_parsed: int = 0
    files_skipped: int = 0
    files_deleted: int = 0
    symbols: int = 0
    methods: int = 0
    classes: int = 0
    errors: list[dict] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    git_branch: str = ""
    git_commit: str = ""

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "db_path": self.db_path,
            "project_root": self.project_root,
            "duration_s": round(self.duration_s, 2),
            "files_total": self.files_total,
            "files_parsed": self.files_parsed,
            "files_skipped": self.files_skipped,
            "files_deleted": self.files_deleted,
            "symbols": self.symbols,
            "methods": self.methods,
            "classes": self.classes,
            "errors": self.errors,
            "counts": self.counts,
            "git_branch": self.git_branch,
            "git_commit": self.git_commit,
        }


def git_meta(root: Path) -> dict[str, str]:
    def run(*args: str) -> str:
        try:
            completed = subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return completed.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    branch = run("rev-parse", "--abbrev-ref", "HEAD")
    commit = run("rev-parse", "--short", "HEAD")
    status = run("status", "--porcelain")
    dirty = str(len([line for line in status.splitlines() if line.strip()]))
    return {"git_branch": branch, "git_commit": commit, "git_dirty": dirty}


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def build(
    profile: ProjectProfile,
    db_path: str | Path | None = None,
    full: bool = False,
    verbose: bool = True,
) -> BuildReport:
    started = time.perf_counter()
    started_at = _now()
    path = Path(db_path) if db_path else profile.resolved_db_path
    report = BuildReport(
        mode="full" if full else "incremental",
        db_path=str(path),
        project_root=str(profile.root),
    )
    git = git_meta(profile.root)
    report.git_branch = git["git_branch"]
    report.git_commit = git["git_commit"]

    conn = connect(path)
    ensure_schema(conn)
    if full:
        reset_all(conn)

    files = discover(profile)
    report.files_total = len(files)
    existing = {
        row["path"]: row for row in conn.execute("SELECT path, sha256, status FROM files")
    }

    changed: list[SourceFile] = []
    unchanged: list[SourceFile] = []
    for file in files:
        old = existing.get(file.rel_path)
        if (
            old
            and old["sha256"] == file.sha256
            and old["status"] == "ok"
            and not full
        ):
            unchanged.append(file)
        else:
            changed.append(file)

    current_paths = {file.rel_path for file in files}
    deleted = [p for p in existing if p not in current_paths]
    report.files_skipped = len(unchanged)
    report.files_deleted = len(deleted)

    # 删除已不存在的文件
    with conn:
        for rel_path in deleted:
            delete_file(conn, rel_path)
            conn.execute("DELETE FROM files WHERE path = ?", (rel_path,))

    # 解析变更文件
    parsed_count = 0
    with conn:
        for index, file in enumerate(changed, 1):
            try:
                text = read_text(file)
                result = parse_file(file, profile, text)
                delete_file(conn, file.rel_path)
                written = write_result(conn, file.rel_path, result, profile)
                report.symbols += len(result.symbols)
                report.methods += len(result.methods)
                conn.execute(
                    """
                    INSERT INTO files(path, module, language, size, mtime, sha256,
                                      loc, parsed_at, status, error)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(path) DO UPDATE SET
                        module=excluded.module, language=excluded.language,
                        size=excluded.size, mtime=excluded.mtime,
                        sha256=excluded.sha256, loc=excluded.loc,
                        parsed_at=excluded.parsed_at, status=excluded.status,
                        error=excluded.error
                    """,
                    (
                        file.rel_path, file.module, file.language, file.size,
                        file.mtime, file.sha256, file.loc, _now(), "ok", "",
                    ),
                )
                parsed_count += 1
            except Exception as exc:  # noqa: BLE001 - 单个文件失败不能中断全量
                error = {"file": file.rel_path, "error": f"{type(exc).__name__}: {exc}"}
                report.errors.append(error)
                conn.execute(
                    """
                    INSERT INTO files(path, module, language, size, mtime, sha256,
                                      loc, parsed_at, status, error)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(path) DO UPDATE SET
                        parsed_at=excluded.parsed_at, status=excluded.status,
                        error=excluded.error
                    """,
                    (
                        file.rel_path, file.module, file.language, file.size,
                        file.mtime, file.sha256, file.loc, _now(), "error",
                        error["error"],
                    ),
                )
            if verbose and index % 50 == 0:
                print(f"  parsed {index}/{len(changed)} ...", flush=True)

    report.files_parsed = parsed_count
    report.classes = rebuild_classes(conn, profile)
    report.counts = counts(conn)
    report.duration_s = time.perf_counter() - started

    finished_at = _now()
    set_metadata(
        conn,
        {
            "schema_version": str(SCHEMA_VERSION),
            "project_name": profile.name,
            "project_root": str(profile.root),
            "build_mode": report.mode,
            "build_time": finished_at,
            "last_parse_started": started_at,
            "source_file_count": str(report.files_total),
            "source_loc": str(summarize(files).get("loc", 0)),
            **git,
        },
    )
    conn.execute(
        """
        INSERT INTO build_runs(started_at, finished_at, mode, duration_s,
            files_total, files_parsed, files_skipped, files_deleted,
            symbols, methods, classes, errors, error_json, git_branch, git_commit)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            started_at, finished_at, report.mode, report.duration_s,
            report.files_total, report.files_parsed, report.files_skipped,
            report.files_deleted, report.symbols, report.methods, report.classes,
            len(report.errors),
            json.dumps(report.errors[:50], ensure_ascii=False),
            git["git_branch"], git["git_commit"],
        ),
    )
    conn.commit()
    conn.close()
    return report


def current_metadata(db_path: str | Path) -> dict[str, str]:
    conn = connect(db_path)
    try:
        return get_metadata(conn)
    finally:
        conn.close()
