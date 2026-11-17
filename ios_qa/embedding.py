#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""向量层：Embedding 模型配置 + SQLite 向量缓存。

模型按 :class:`EmbeddingProfile` 描述“模型 / 维度 / 文本模板”，默认 ``qwen3-0.6b``：

- ``qwen3-0.6b``：``Qwen/Qwen3-Embedding-0.6B-Q``（INT8），1024 维；query 用
  ``Instruct: {task}\\nQuery: {query}``，document 为裸文本（无前缀）；
- ``bge-small-zh``：``BAAI/bge-small-zh-v1.5``，512 维；
  ``search_document:`` / ``search_query:`` 前缀（参考实现用的模型）；
- ``jina-code``：``jinaai/jina-embeddings-v2-base-code``，768 维，裸文本；
  只覆盖英文 + 编程语言，中文 query 属于分布外，仅用于 A/B。

切换 profile 用环境变量 ``IOS_QA_EMBED_PROFILE``。缓存
``embedding_cache.db`` 的 key 是 ``MD5("{model}:{text}")``，换模型自动失效；
``build_meta.json`` 记录 model/dim，查询侧发现与当前模型不一致会要求重建，
不读取旧维度的索引。

稀疏通道 ``Qdrant/bm42-all-minilm-l6-v2-attentions``（BM42）与稠密向量一起写入
Qdrant，由 RRF 融合。没装 ``fastembed`` 时抛 :class:`EmbeddingUnavailable`，
构建向量索引会给出安装提示，不影响结构化索引。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PROFILE = "qwen3-0.6b"
SPARSE_MODEL = "Qdrant/bm42-all-minilm-l6-v2-attentions"

BODY_LIMIT = 2000
# 声明前的文档注释通常是一句业务说明（中文居多），对「问题里没有代码字面量」
# 的检索帮助最大，但要截断，避免复制粘贴的大段注释稀释方法体。
COMMENT_LIMIT = 300
MIN_BODY_CHARS = 80
MIN_BODY_LINES = 3

CACHE_BATCH = 500
# 0.6B 模型按 256 条推理会把内存顶到 12 GB 并触发 swap，32 条稳定在 ~2 GB。
EMBED_BATCH = 32

INSTALL_HINT = (
    "向量层依赖未安装：python3 -m pip install qdrant-client fastembed\n"
    "首次运行会下载模型；直连 huggingface.co 失败时可加 "
    "HF_ENDPOINT=https://hf-mirror.com"
)


class EmbeddingUnavailable(RuntimeError):
    """缺少向量依赖或模型无法加载。"""


# 查询侧任务描述：Qwen3 要求 query 带一句任务说明，document 不带。
QUERY_INSTRUCTION = (
    "Given a natural-language question about an iOS app, retrieve the "
    "Objective-C or Swift method whose implementation answers it"
)

# 这些注释是分隔符/待办，放进 embedding 只会稀释方法体语义。
_COMMENT_NOISE = ("MARK:", "PRAGMA MARK", "FIXME:")


def clean_comment(text: str) -> str:
    """把声明前的注释压成一行可检索文本，丢掉 ``MARK:`` 之类的分隔注释。"""
    lines = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.upper().startswith(_COMMENT_NOISE):
            continue
        lines.append(line)
    return " ".join(lines)[:COMMENT_LIMIT]


@dataclass(frozen=True)
class EmbeddingProfile:
    """一个 embedding 模型的文本契约：维度 + 索引/查询/片段模板。"""

    name: str
    model: str
    dim: int
    document: str
    query: str
    snippet: str

    def document_text(
        self,
        class_name: str,
        method_name: str,
        body: str,
        comment: str = "",
        tags: list[str] | None = None,
    ) -> str:
        tag_text = f"\n业务标签:{' '.join(tags)}" if tags else ""
        comment = clean_comment(comment)
        return self.document.format(
            class_name=class_name,
            method_name=method_name,
            tags=tag_text,
            comment=f"{comment}\n" if comment else "",
            body=(body or "")[:BODY_LIMIT],
        )

    def query_text(self, query: str) -> str:
        return self.query.format(
            query=(query or "").strip(), instruction=QUERY_INSTRUCTION
        )

    def snippet_text(self, code: str) -> str:
        return self.snippet.format(code=(code or "")[:BODY_LIMIT])


PROFILES: dict[str, EmbeddingProfile] = {
    "qwen3-0.6b": EmbeddingProfile(
        name="qwen3-0.6b",
        model="Qwen/Qwen3-Embedding-0.6B-Q",
        dim=1024,
        document="类:{class_name} 方法:{method_name}{tags}\n{comment}{body}",
        query="Instruct: {instruction}\nQuery: {query}",
        snippet="{code}",
    ),
    "bge-small-zh": EmbeddingProfile(
        name="bge-small-zh",
        model="BAAI/bge-small-zh-v1.5",
        dim=512,
        document="search_document: 类:{class_name} 方法:{method_name}{tags}\n{comment}{body}",
        query="search_query: {query}",
        snippet="search_document: {code}",
    ),
    "jina-code": EmbeddingProfile(
        name="jina-code",
        model="jinaai/jina-embeddings-v2-base-code",
        dim=768,
        document="{class_name} {method_name}{tags}\n{comment}{body}",
        query="{query}",
        snippet="{code}",
    ),
}


def get_profile(name: str | None = None) -> EmbeddingProfile:
    """按名字/环境变量取 profile；未指定时用默认模型。"""
    key = (name or os.environ.get("IOS_QA_EMBED_PROFILE") or DEFAULT_PROFILE).strip()
    profile = PROFILES.get(key)
    if profile is None:
        available = ", ".join(sorted(PROFILES))
        raise ValueError(f"未知的 embedding profile：{key}（可选：{available}）")
    return profile


ACTIVE_PROFILE = get_profile()
EMBED_MODEL = ACTIVE_PROFILE.model
EMBED_DIM = ACTIVE_PROFILE.dim


def document_text(
    class_name: str,
    method_name: str,
    body: str,
    comment: str = "",
    tags: list[str] | None = None,
) -> str:
    """索引侧文本：类/方法/业务标签/文档注释/方法体。"""
    return ACTIVE_PROFILE.document_text(
        class_name, method_name, body, comment=comment, tags=tags
    )


def query_text(query: str) -> str:
    return ACTIVE_PROFILE.query_text(query)


def snippet_text(code: str) -> str:
    return ACTIVE_PROFILE.snippet_text(code)


def cache_key(model: str, text: str) -> str:
    return hashlib.md5(f"{model}:{text}".encode("utf-8")).hexdigest()


def pack_vector(values: list[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def unpack_vector(blob: bytes, dim: int) -> list[float]:
    return list(struct.unpack(f"<{dim}f", blob))


def normalize(values: list[float]) -> list[float]:
    norm = sum(value * value for value in values) ** 0.5
    if norm <= 0:
        return values
    return [value / norm for value in values]


@dataclass
class SparseVectorData:
    indices: list[int]
    values: list[float]

    def to_dict(self) -> dict:
        return {"indices": self.indices, "values": self.values}


class EmbeddingCache:
    """``embedding_cache.db``：稠密表 ``cache`` + 稀疏表 ``sparse_cache``。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, vector BLOB)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS sparse_cache ("
            "key TEXT PRIMARY KEY, indices BLOB, vals BLOB)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS rerank_cache ("
            "key TEXT PRIMARY KEY, score REAL)"
        )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "EmbeddingCache":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    # -- 稠密 ----------------------------------------------------------
    def get_dense(self, keys: list[str]) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for start in range(0, len(keys), CACHE_BATCH):
            chunk = keys[start : start + CACHE_BATCH]
            marks = ",".join("?" for _ in chunk)
            rows = self.conn.execute(
                f"SELECT key, vector FROM cache WHERE key IN ({marks})", chunk
            ).fetchall()
            for key, blob in rows:
                out[key] = unpack_vector(blob, len(blob) // 4)
        return out

    def put_dense(self, items: list[tuple[str, list[float]]]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO cache(key, vector) VALUES (?,?)",
            [(key, pack_vector(values)) for key, values in items],
        )
        self.conn.commit()

    # -- 稀疏 ----------------------------------------------------------
    def get_sparse(self, keys: list[str]) -> dict[str, SparseVectorData]:
        out: dict[str, SparseVectorData] = {}
        for start in range(0, len(keys), CACHE_BATCH):
            chunk = keys[start : start + CACHE_BATCH]
            marks = ",".join("?" for _ in chunk)
            rows = self.conn.execute(
                f"SELECT key, indices, vals FROM sparse_cache WHERE key IN ({marks})",
                chunk,
            ).fetchall()
            for key, indices, values in rows:
                out[key] = SparseVectorData(
                    list(struct.unpack(f"<{len(indices) // 4}I", indices)),
                    list(struct.unpack(f"<{len(values) // 4}f", values)),
                )
        return out

    def put_sparse(self, items: list[tuple[str, SparseVectorData]]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO sparse_cache(key, indices, vals) VALUES (?,?,?)",
            [
                (
                    key,
                    struct.pack(f"<{len(vec.indices)}I", *vec.indices),
                    struct.pack(f"<{len(vec.values)}f", *vec.values),
                )
                for key, vec in items
            ],
        )
        self.conn.commit()

    # -- 精排 ----------------------------------------------------------
    def get_rerank(self, keys: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for start in range(0, len(keys), CACHE_BATCH):
            chunk = keys[start : start + CACHE_BATCH]
            marks = ",".join("?" for _ in chunk)
            rows = self.conn.execute(
                f"SELECT key, score FROM rerank_cache WHERE key IN ({marks})", chunk
            ).fetchall()
            for key, score in rows:
                out[key] = float(score)
        return out

    def put_rerank(self, items: list[tuple[str, float]]) -> None:
        if not items:
            return
        self.conn.executemany(
            "INSERT OR REPLACE INTO rerank_cache(key, score) VALUES (?,?)",
            [(key, float(score)) for key, score in items],
        )
        self.conn.commit()


class DenseEmbedder:
    """稠密向量，模型/维度来自 :class:`EmbeddingProfile`，走 ``embedding_cache.db``。"""

    def __init__(
        self,
        cache: EmbeddingCache,
        profile: EmbeddingProfile | None = None,
        model: str | None = None,
        model_cache_dir: str | Path | None = None,
    ) -> None:
        self.cache = cache
        self.profile = profile or ACTIVE_PROFILE
        self.model = model or self.profile.model
        self.dim = self.profile.dim
        self.model_cache_dir = model_cache_dir
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - 环境相关
            raise EmbeddingUnavailable(INSTALL_HINT) from exc
        try:
            self._model = TextEmbedding(
                model_name=self.model,
                cache_dir=str(self.model_cache_dir) if self.model_cache_dir else None,
            )
        except Exception as exc:  # pragma: no cover - 网络/模型相关
            raise EmbeddingUnavailable(
                f"加载 embedding 模型失败（{self.model}）：{exc}\n{INSTALL_HINT}"
            ) from exc
        return self._model

    def embed_documents(self, texts: list[str]) -> tuple[list[list[float]], int]:
        """返回 ``(vectors, cached_count)``。"""
        if not texts:
            return [], 0
        keys = [cache_key(self.model, text) for text in texts]
        cached = self.cache.get_dense(keys)
        missing = [
            (index, key, text)
            for index, (key, text) in enumerate(zip(keys, texts))
            if key not in cached
        ]
        if missing:
            model = self._load()
            fresh: list[tuple[str, list[float]]] = []
            for start in range(0, len(missing), EMBED_BATCH):
                batch = missing[start : start + EMBED_BATCH]
                vectors = model.embed([text for _, _, text in batch], batch_size=EMBED_BATCH)
                for (index, key, _), vector in zip(batch, vectors):
                    values = normalize([float(value) for value in vector])
                    cached[key] = values
                    fresh.append((key, values))
            self.cache.put_dense(fresh)
        return [cached[key] for key in keys], len(texts) - len(missing)

    def embed_query(self, text: str) -> list[float]:
        vectors, _ = self.embed_documents([text])
        return vectors[0]


class SparseEmbedder:
    """BM42 稀疏向量，同样走缓存（``sparse_cache`` 表）。"""

    def __init__(
        self,
        cache: EmbeddingCache,
        model: str = SPARSE_MODEL,
        model_cache_dir: str | Path | None = None,
    ) -> None:
        self.cache = cache
        self.model = model
        self.model_cache_dir = model_cache_dir
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from fastembed import SparseTextEmbedding
        except ImportError as exc:  # pragma: no cover - 环境相关
            raise EmbeddingUnavailable(INSTALL_HINT) from exc
        try:
            self._model = SparseTextEmbedding(
                model_name=self.model,
                cache_dir=str(self.model_cache_dir) if self.model_cache_dir else None,
            )
        except Exception as exc:  # pragma: no cover - 网络/模型相关
            raise EmbeddingUnavailable(
                f"加载稀疏模型失败（{self.model}）：{exc}\n{INSTALL_HINT}"
            ) from exc
        return self._model

    def embed_documents(self, texts: list[str]) -> tuple[list[SparseVectorData], int]:
        if not texts:
            return [], 0
        keys = [cache_key(self.model, text) for text in texts]
        cached = self.cache.get_sparse(keys)
        missing = [
            (index, key, text)
            for index, (key, text) in enumerate(zip(keys, texts))
            if key not in cached
        ]
        if missing:
            model = self._load()
            fresh: list[tuple[str, SparseVectorData]] = []
            for start in range(0, len(missing), EMBED_BATCH):
                batch = missing[start : start + EMBED_BATCH]
                vectors = model.embed([text for _, _, text in batch], batch_size=EMBED_BATCH)
                for (index, key, _), vector in zip(batch, vectors):
                    data = SparseVectorData(
                        [int(value) for value in vector.indices],
                        [float(value) for value in vector.values],
                    )
                    cached[key] = data
                    fresh.append((key, data))
            self.cache.put_sparse(fresh)
        return [cached[key] for key in keys], len(texts) - len(missing)

    def embed_query(self, text: str) -> SparseVectorData:
        """查询用 BM42 的 ``query_embed``（重量不同，且只算一次不入缓存）。"""
        model = self._load()
        vector = list(model.query_embed([text]))[0]
        return SparseVectorData(
            [int(value) for value in vector.indices],
            [float(value) for value in vector.values],
        )
