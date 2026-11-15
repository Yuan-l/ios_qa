#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""源码发现与过滤。

过滤策略（比参考实现更保守，宁可漏索引也不索引三方副本）：

1. 目录名黑名单：``Pods`` / ``build`` / ``.build`` / ``Frameworks`` / ``Example`` / ``Tests`` …
2. 路径片段黑名单：``/SDWebImage/``、``/Flutter.framework/`` 等 vendored 三方代码
3. 文件名黑名单：``*-Swift.h``、``Pods-*.h`` 等生成文件
4. 只在 ``source_dirs`` 下扫描，不碰 ``Pods/`` 安装副本

每个文件都算 sha256，供增量构建判断是否变更。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from .config import ProjectProfile

LANGUAGE_BY_EXT = {
    ".h": "objc",
    ".m": "objc",
    ".mm": "objc",
    ".swift": "swift",
    ".storyboard": "ui",
    ".xib": "ui",
}


@dataclass(frozen=True)
class SourceFile:
    path: Path
    rel_path: str
    module: str
    language: str
    size: int
    mtime: float
    sha256: str
    loc: int

    @property
    def suffix(self) -> str:
        return self.path.suffix.lower()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _count_lines(path: Path) -> int:
    try:
        with path.open("rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def discover(profile: ProjectProfile) -> list[SourceFile]:
    """返回过滤后的源文件列表（按相对路径排序）。"""
    root = profile.root
    found: dict[str, SourceFile] = {}

    for source_dir in profile.source_dirs:
        base = (root / source_dir).resolve()
        if not base.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            current = Path(dirpath)
            # 原地裁剪目录，避免进入 Pods / .git / 三方 vendored 目录
            kept: list[str] = []
            for name in dirnames:
                full = current / name
                try:
                    rel = full.relative_to(root).as_posix()
                except ValueError:
                    continue
                if profile.should_skip_dir(name) or profile.should_skip_path(rel + "/"):
                    continue
                if name.startswith(".") and name not in {".", ".."}:
                    continue
                kept.append(name)
            dirnames[:] = sorted(kept)

            for filename in filenames:
                path = current / filename
                suffix = path.suffix.lower()
                if suffix not in profile.include_exts:
                    continue
                if profile.should_skip_file(filename):
                    continue
                try:
                    rel_path = path.relative_to(root).as_posix()
                except ValueError:
                    continue
                if profile.should_skip_path(rel_path):
                    continue
                if rel_path in found:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                found[rel_path] = SourceFile(
                    path=path,
                    rel_path=rel_path,
                    module=profile.module_for(path.relative_to(root)),
                    language=LANGUAGE_BY_EXT.get(suffix, "unknown"),
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                    sha256=_sha256(path),
                    loc=_count_lines(path),
                )

    return [found[key] for key in sorted(found)]


def summarize(files: list[SourceFile]) -> dict[str, int]:
    result: dict[str, int] = {"total": len(files), "loc": 0}
    for file in files:
        result["loc"] += file.loc
        result[file.language] = result.get(file.language, 0) + 1
        result[f"module:{file.module}"] = result.get(f"module:{file.module}", 0) + 1
    return result
