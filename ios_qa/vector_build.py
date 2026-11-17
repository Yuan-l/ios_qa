#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""向量索引构建：``methods`` collection（两个 dense + sparse BM42）。

对应参考方案的 ``build_vector_index.py``，并按本项目的片段检索需求拆成两条
同构的 dense 通道：

- ``dense``：文档向量，文本 = 类/方法名 + 同义词标签 + 注释 + 方法体，
  服务自然语言检索（``vector_search`` / ``hybrid_find_feature``）；
- ``dense_body``：片段向量，文本 = ``snippet_text(body)``，与
  ``search_similar_snippet`` 查询侧用同一份文本，两边同构，所以「贴一段代码
  找它自己」不会被类名/注释/标签稀释；
- ``sparse``：BM42，只服务文档通道的 hybrid 检索。

其余流程：

1. 从 ``methods`` 表取方法体，跳过 <3 行或 <80 字符的短方法；
2. ``point_id = MD5(file_path:method_name:line_start)[:16]`` 去重，保证幂等
   upsert；行号必须参与，否则同文件里的重载方法会互相覆盖；
3. 文档文本 ``类:{class} 方法:{method}\n业务标签:{synonyms}\n{comment}\n{body[:2000]}``，
   同义词标签与文档注释让中文问题命中英文命名的实现；
4. 写入 Qdrant ``methods``（dense/dense_body 余弦 + sparse BM42）；
5. ``build_meta.json`` 记录 git / model / method_count / ``template_id`` /
   ``vectors``（命名向量列表）/ 耗时；
6. ``file_hashes.json`` 记录源文件 SHA256 与 ``template_id``，下次只重算变更
   文件；模板或索引布局变了则强制全量重算。
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
    snippet_text,
    template_id,
)
from .synonyms import load_groups, match_tags
from .vector_store import (
    BODY_VECTOR,
    DENSE_VECTOR,
    POINT_SCHEME,
    SPARSE_VECTOR,
    VectorStore,
    point_id,
)

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
    template_id: str = ""
    vectors: list[str] = field(default_factory=list)
    methods_total: int = 0
    methods_selected: int = 0
    points_written: int = 0
    embedded: int = 0
    cached: int = 0
    body_embedded: int = 0
    body_cached: int = 0
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
            "template_id": self.template_id,
            "vectors": self.vectors,
            "methods_total": self.methods_total,
            "methods_selected": self.methods_selected,
            "points_written": self.points_written,
            "embedded": self.embedded,
            "cached": self.cached,
            "body_embedded": self.body_embedded,
            "body_cached": self.body_cached,
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
        "line_start, line_end, body, doc_comment FROM methods WHERE has_body = 1"
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


def method_document(row: Any, groups: list[list[str]]) -> str:
    """索引文本：类/方法 + 同义词标签 + 文档注释 + 方法体。"""
    tag_source = " ".join(
        (
            row["class_name"] or "",
            row["method_name"] or "",
            row["module"] or "",
            row["doc_comment"] or "",
        )
    )
    return document_text(
        row["class_name"],
        row["method_name"],
        row["body"] or "",
        comment=row["doc_comment"] or "",
        tags=match_tags(tag_source, groups),
    )


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
        contract = template_id()
        same_template = previous.get("template_id") == contract
        report.template_id = contract
        # 索引布局也要认：命名向量（旧 collection 只有 dense）和 point id
        # 方案（旧版没带行号，重载方法会互相覆盖）变了都必须重建。
        layout_ok = (
            BODY_VECTOR in store.vector_names()
            and previous.get("point_scheme") == POINT_SCHEME
        )
        try:
            # 文本模板变了，旧向量在语义上已经作废：增量构建按文件 sha256 判断
            # 「没变就跳过」，不看模板，所以必须在这里强制全量重算。
            recreate = full or not same_model or not same_template or not layout_ok
            if recreate:
                store.ensure_collection(recreate=store.collection_exists())
            else:
                store.ensure_collection()
            report.vectors = store.vector_names()

            if recreate or not previous:
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
                selected[
                    point_id(row["file_path"], row["method_name"], row["line_start"])
                ] = row
            picked = list(selected.items())
            if limit:
                picked = picked[:limit]
            report.methods_selected = len(picked)

            if picked:
                groups = load_groups(profile)
                texts = [method_document(row, groups) for _, row in picked]
                # 片段通道：和查询侧 snippet_text 用同一份文本，两边同构。
                body_texts = [snippet_text(row["body"] or "") for _, row in picked]
                dense_vectors, dense_cached = dense.embed_documents(texts)
                report.cached += dense_cached
                report.embedded += len(texts) - dense_cached
                body_vectors, body_cached = dense.embed_documents(body_texts)
                report.body_cached += body_cached
                report.body_embedded += len(body_texts) - body_cached
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
                    vector = {
                        DENSE_VECTOR: dense_vectors[index],
                        BODY_VECTOR: body_vectors[index],
                    }
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
                "template_id": contract,
                "vectors": store.vector_names(),
                "point_scheme": POINT_SCHEME,
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
                        "template_id": contract,
                        "point_scheme": POINT_SCHEME,
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
