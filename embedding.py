#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""向量层：Embedding 模型 + SQLite 向量缓存。

与参考方案对齐（``Ios-qa MCP Server完整技术分析.md`` / ``iOS向量库 && 飞书loom.md``）：

- 稠密模型 ``BAAI/bge-small-zh-v1.5``（512 维），fastembed + ONNX Runtime，
  Mac 上 fastembed 会优先用 CoreML provider；
- 文本前缀：索引 ``search_document: 类:{class} 方法:{method}\\n{body[:2000]}``，
  查询 ``search_query: {query}``，代码片段 ``search_document: {code}``；
- 缓存 ``embedding_cache.db``：``key = MD5("{model}:{text}")``，
  ``value = struct.pack("<512f", *vector)``，只对未命中的文本做推理；
- 稀疏通道 ``Qdrant/bm42-all-minilm-l6-v2-attentions``（BM42），
  与稠密向量一起写入 Qdrant，由 RRF 融合。

模型是**可替换的运行时依赖**：没装 ``fastembed`` 时抛
:class:`EmbeddingUnavailable`，构建向量索引会给出安装提示，不影响结构化索引。
"""

from __future__ import annotations

import hashlib
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path

EMBED_MODEL = "BAAI/bge-small-zh-v1.5"
EMBED_DIM = 512
SPARSE_MODEL = "Qdrant/bm42-all-minilm-l6-v2-attentions"

BODY_LIMIT = 2000
MIN_BODY_CHARS = 80
MIN_BODY_LINES = 3

CACHE_BATCH = 500
EMBED_BATCH = 256

INSTALL_HINT = (
    "向量层依赖未安装：python3 -m pip install qdrant-client fastembed\n"
    "首次运行会下载模型；直连 huggingface.co 失败时可加 "
    "HF_ENDPOINT=https://hf-mirror.com"
)


class EmbeddingUnavailable(RuntimeError):
    """缺少向量依赖或模型无法加载。"""


def document_text(class_name: str, method_name: str, body: str) -> str:
    """索引侧文本：类/方法是精确词，必须进 embedding 输入。"""
    return (
        f"search_document: 类:{class_name} 方法:{method_name}\n"
        f"{(body or '')[:BODY_LIMIT]}"
    )


def query_text(query: str) -> str:
    return f"search_query: {(query or '').strip()}"


def snippet_text(code: str) -> str:
    return f"search_document: {(code or '')[:BODY_LIMIT]}"


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


class DenseEmbedder:
    """BGE-small-zh-v1.5，走 ``embedding_cache.db`` 缓存。"""

    def __init__(
        self,
        cache: EmbeddingCache,
        model: str = EMBED_MODEL,
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
