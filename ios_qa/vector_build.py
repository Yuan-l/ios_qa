#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""向量索引构建：``methods`` collection（dense BGE + sparse BM42）。

对应参考方案的 ``build_vector_index.py``：

1. 从 ``methods`` 表取方法体，跳过 <3 行或 <80 字符的短方法；
2. ``point_id = MD5(file_path:method_name)[:16]`` 去重，保证幂等 upsert；
3. 文本 ``search_document: 类:{class} 方法:{method}\\n{body[:2000]}``，
   经 ``embedding_cache.db`` 缓存后只算新增/变更的方法；
4. 写入 Qdrant ``methods``（dense 余弦 + sparse BM42）；
5. ``build_meta.json`` 记录 git / model / method_count / 耗时；
6. ``file_hashes.json`` 记录源文件 SHA256，下次只重算变更文件。
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import ProjectProfile
from .db import connect, reflow_preview
from .embedding import (
    EMBED_DIM,
    EMBED_MODEL,
    MIN_BODY_CHARS,
    MIN_BODY_LINES,
    SPARSE_MODEL,
    DenseEmbedder,
    EmbeddingCache,
    EmbeddingUnavailable,
    SparseEmbedder,
    document_text,
)
from .vector_store import DENSE_VECTOR, SPARSE_VECTOR, VectorStore, point_id

VECTOR_DIR_NAME = "qdrant_storage"
META_FILE = "build_meta.json"
HASH_FILE = "file_hashes.json"


@dataclass
class VectorBuildReport:
    mode: str = "incremental"
    vector_dir: str = ""
    collection: str = "methods"
    model: str = EMBED_MODEL
    sparse_model: str = SPARSE_MODEL
    dim: int = EMBED_DIM
    methods_total: int = 0
    methods_selected: int = 0
    points_written: int = 0
    embedded: int = 0
    cached: int = 0
    sparse_embedded: int = 0
    sparse_cached: int = 0
    files_changed: int = 0
    files_deleted: int = 0
    collection_points: int = 0
    elapsed_s: float = 0.0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "vector_dir": self.vector_dir,
            "collection": self.collection,
            "model": self.model,
            "sparse_model": self.sparse_model,
            "dim": self.dim,
            "methods_total": self.methods_total,
            "methods_selected": self.methods_selected,
            "points_written": self.points_written,
            "embedded": self.embedded,
            "cached": self.cached,
            "sparse_embedded": self.sparse_embedded,
            "sparse_cached": self.sparse_cached,
            "files_changed": self.files_changed,
            "files_deleted": self.files_deleted,
            "collection_points": self.collection_points,
            "elapsed_s": round(self.elapsed_s, 2),
            "errors": self.errors,
        }


def vector_dir(profile: ProjectProfile, db_path: str | Path | None = None) -> Path:
    db = Path(db_path) if db_path else profile.resolved_db_path
    return db.parent / VECTOR_DIR_NAME


def read_meta(profile: ProjectProfile, db_path: str | Path | None = None) -> dict:
    path = vector_dir(profile, db_path) / META_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _read_hashes(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _select_methods(conn: sqlite3.Connection, files: set[str] | None = None) -> list[sqlite3.Row]:
    sql = (
        "SELECT class_name, method_name, module, language, file_path, "
        "line_start, line_end, body FROM methods WHERE has_body = 1"
    )
    params: list[Any] = []
    if files is not None:
        if not files:
            return []
        marks = ",".join("?" for _ in files)
        sql += f" AND file_path IN ({marks})"
        params = sorted(files)
    return conn.execute(sql + " ORDER BY file_path, line_start", params).fetchall()


def _eligible(row: sqlite3.Row) -> bool:
    body = row["body"] or ""
    lines = (row["line_end"] or row["line_start"] or 0) - (row["line_start"] or 0) + 1
    return len(body.strip()) >= MIN_BODY_CHARS and lines >= MIN_BODY_LINES


def build_vectors(
    profile: ProjectProfile,
    db_path: str | Path | None = None,
    full: bool = False,
    verbose: bool = False,
    limit: int = 0,
    store: VectorStore | None = None,
    cache: EmbeddingCache | None = None,
    dense_embedder: DenseEmbedder | None = None,
    sparse_embedder: SparseEmbedder | None = None,
) -> VectorBuildReport:
    started = time.perf_counter()
    db = Path(db_path) if db_path else profile.resolved_db_path
    target_dir = vector_dir(profile, db)
    target_dir.mkdir(parents=True, exist_ok=True)
    report = VectorBuildReport(
        mode="full" if full else "incremental", vector_dir=str(target_dir)
    )

    conn = connect(db)
    conn.row_factory = sqlite3.Row
    try:
        current_files = {
            row["path"]: row["sha256"]
            for row in conn.execute("SELECT path, sha256 FROM files")
        }
        previous = _read_hashes(target_dir / HASH_FILE)
        prev_files = previous.get("files") or {}

        own_cache = cache is None
        cache = cache or EmbeddingCache(db.parent / "embedding_cache.db")
        dense = dense_embedder or DenseEmbedder(cache)
        sparse = sparse_embedder or SparseEmbedder(cache)
        # 维度跟着 provider 走：真模型是 1024，测试里的假 embedder 是 8
        dim = int(getattr(dense, "dim", EMBED_DIM))
        model_name = getattr(dense, "model", EMBED_MODEL)
        sparse_name = getattr(sparse, "model", SPARSE_MODEL)
        own_store = store is None
        store = store or VectorStore(target_dir, dim=dim)
        report.dim = store.dim
        report.model = model_name
        report.sparse_model = sparse_name
        same_model = (
            previous.get("model") == model_name and previous.get("dim") == store.dim
        )
        try:
            recreate = full or not same_model
            if recreate:
                store.ensure_collection(recreate=store.collection_exists())
            else:
                store.ensure_collection()

            if full or not same_model or not previous:
                changed = set(current_files)
                deleted: set[str] = set()
                report.mode = "full"
            else:
                changed = {
                    path
                    for path, sha in current_files.items()
                    if prev_files.get(path) != sha
                }
                deleted = set(prev_files) - set(current_files)

            report.files_changed = len(changed)
            report.files_deleted = len(deleted)
            # 变更文件里的旧 point 先删掉：方法可能被删除或行号变化
            store.delete_files(sorted(changed | deleted))

            rows = _select_methods(conn, changed)
            report.methods_total = len(rows)
            selected: dict[int, sqlite3.Row] = {}
            for row in rows:
                if not _eligible(row):
                    continue
                selected[point_id(row["file_path"], row["method_name"])] = row
            picked = list(selected.items())
            if limit:
                picked = picked[:limit]
            report.methods_selected = len(picked)

            if picked:
                texts = [
                    document_text(
                        row["class_name"], row["method_name"], row["body"] or ""
                    )
                    for _, row in picked
                ]
                dense_vectors, dense_cached = dense.embed_documents(texts)
                report.cached += dense_cached
                report.embedded += len(texts) - dense_cached
                sparse_vectors = None
                if store.sparse:
                    sparse_vectors, sparse_cached = sparse.embed_documents(texts)
                    report.sparse_cached += sparse_cached
                    report.sparse_embedded += len(texts) - sparse_cached

                points = []
                for index, (pid, row) in enumerate(picked):
                    payload = {
                        "class_name": row["class_name"],
                        "method_name": row["method_name"],
                        "module": row["module"],
                        "language": row["language"],
                        "file_path": row["file_path"],
                        "line_start": row["line_start"],
                        "line_end": row["line_end"],
                        "body_preview": reflow_preview(
                            row["body"] or "", row["method_name"]
                        ),
                    }
                    vector = {DENSE_VECTOR: dense_vectors[index]}
                    if sparse_vectors is not None:
                        vector[SPARSE_VECTOR] = {
                            "indices": sparse_vectors[index].indices,
                            "values": sparse_vectors[index].values,
                        }
                    points.append({"id": pid, "vector": vector, "payload": payload})
                report.points_written = store.upsert(points)
                if verbose:
                    print(f"  vectors {report.points_written} points ...", flush=True)

            report.collection_points = store.count()
            elapsed = time.perf_counter() - started
            meta = {
                "build_time": _now(),
                "git_branch": _git(profile.root, "rev-parse", "--abbrev-ref", "HEAD"),
                "git_commit": _git(profile.root, "rev-parse", "--short", "HEAD"),
                "source_root": str(profile.root),
                "method_count": report.collection_points,
                "model": report.model,
                "sparse_model": report.sparse_model,
                "dim": report.dim,
                "collection": "methods",
                "elapsed_seconds": round(elapsed, 2),
            }
            (target_dir / META_FILE).write_text(
                json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            (target_dir / HASH_FILE).write_text(
                json.dumps(
                    {
                        "model": report.model,
                        "dim": report.dim,
                        "built_at": meta["build_time"],
                        "files": current_files,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        finally:
            if own_store:
                store.close()
            if own_cache:
                cache.close()
    finally:
        conn.close()

    report.elapsed_s = time.perf_counter() - started
    return report


def _git(root: Path, *args: str) -> str:
    import subprocess

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
