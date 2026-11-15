#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qdrant 向量库适配：collection ``methods``，dense + sparse 双通道。

与参考方案一致：

- collection ``methods``，``dense`` 用余弦距离，``sparse`` 是 BM42；
- ``point_id = int(MD5(f"{file_path}:{method_name}")[:16], 16)``，天然幂等；
- payload：``class_name / method_name / module / language / file_path /
  line_start / line_end / body_preview``；
- 查询支持 ``module`` 过滤；hybrid 查询用 Qdrant 原生 RRF（k=60）。

参考方案跑的是 Qdrant server；这里用 ``QdrantClient(path=...)`` 的本地持久化
模式，API 与语义一致，但不需要额外启动服务，便于离线验收。切到 server 只需
把 ``QdrantClient`` 的构造参数换成 ``url``。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .embedding import EMBED_DIM, SparseVectorData

COLLECTION = "methods"
DENSE_VECTOR = "dense"
SPARSE_VECTOR = "sparse"
RRF_K = 60


class VectorUnavailable(RuntimeError):
    """qdrant-client 未安装或向量库尚未构建。"""


def point_id(file_path: str, method_name: str) -> int:
    digest = hashlib.md5(f"{file_path}:{method_name}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def _hit(point: Any, fusion: str) -> dict:
    payload = dict(point.payload or {})
    return {
        "class_name": payload.get("class_name", ""),
        "method_name": payload.get("method_name", ""),
        "module": payload.get("module", ""),
        "language": payload.get("language", ""),
        "file_path": payload.get("file_path", ""),
        "line_start": payload.get("line_start", 0),
        "line_end": payload.get("line_end", 0),
        "body_preview": payload.get("body_preview", ""),
        "score": round(float(point.score or 0.0), 4),
        "fusion": fusion,
    }


class VectorStore:
    def __init__(
        self,
        path: str | Path,
        dim: int = EMBED_DIM,
        sparse: bool = True,
        url: str = "",
    ) -> None:
        self.path = Path(path)
        self.dim = dim
        self.sparse = sparse
        self.url = url
        self._client = None

    # -- 生命周期 ------------------------------------------------------
    @property
    def client(self):
        if self._client is None:
            try:
                from qdrant_client import QdrantClient
            except ImportError as exc:  # pragma: no cover - 环境相关
                raise VectorUnavailable(
                    "向量库依赖未安装：python3 -m pip install qdrant-client fastembed"
                ) from exc
            if self.url:
                self._client = QdrantClient(url=self.url)
            else:
                self.path.mkdir(parents=True, exist_ok=True)
                self._client = QdrantClient(path=str(self.path))
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def collection_exists(self) -> bool:
        try:
            return bool(self.client.collection_exists(COLLECTION))
        except Exception:  # pragma: no cover - 旧版本 qdrant-client
            try:
                self.client.get_collection(COLLECTION)
                return True
            except Exception:
                return False

    def count(self) -> int:
        if not self.collection_exists():
            return 0
        return int(self.client.count(COLLECTION, exact=True).count)

    # -- 建库 ----------------------------------------------------------
    def ensure_collection(self, recreate: bool = False) -> None:
        from qdrant_client import models

        if recreate and self.collection_exists():
            self.client.delete_collection(COLLECTION)
        if self.collection_exists():
            return
        self.client.create_collection(
            collection_name=COLLECTION,
            vectors_config={
                DENSE_VECTOR: models.VectorParams(
                    size=self.dim, distance=models.Distance.COSINE
                )
            },
            sparse_vectors_config=(
                {SPARSE_VECTOR: models.SparseVectorParams()} if self.sparse else None
            ),
        )

    def upsert(self, points: list[dict], batch: int = 64) -> int:
        """``points`` 每项：id + vector(dense/sparse) + payload 字段。"""
        from qdrant_client import models

        written = 0
        for start in range(0, len(points), batch):
            chunk = points[start : start + batch]
            self.client.upsert(
                COLLECTION,
                points=[
                    models.PointStruct(
                        id=item["id"],
                        vector=item["vector"],
                        payload=item["payload"],
                    )
                    for item in chunk
                ],
                wait=True,
            )
            written += len(chunk)
        return written

    def delete_files(self, file_paths: list[str]) -> None:
        if not file_paths:
            return
        from qdrant_client import models

        self.client.delete(
            COLLECTION,
            points_selector=models.Filter(
                must=[
                    models.FieldCondition(
                        key="file_path", match=models.MatchAny(any=sorted(file_paths))
                    )
                ]
            ),
            wait=True,
        )

    # -- 查询 ----------------------------------------------------------
    def _filter(self, module: str = ""):
        from qdrant_client import models

        conditions = []
        if module:
            conditions.append(
                models.FieldCondition(key="module", match=models.MatchValue(value=module))
            )
        return models.Filter(must=conditions) if conditions else None

    def search_dense(self, vector: list[float], limit: int, module: str = "") -> list[dict]:
        response = self.client.query_points(
            COLLECTION,
            query=vector,
            using=DENSE_VECTOR,
            query_filter=self._filter(module),
            limit=limit,
            with_payload=True,
        )
        return [_hit(point, "dense") for point in response.points]

    def search_sparse(
        self, vector: SparseVectorData, limit: int, module: str = ""
    ) -> list[dict]:
        from qdrant_client import models

        response = self.client.query_points(
            COLLECTION,
            query=models.SparseVector(indices=vector.indices, values=vector.values),
            using=SPARSE_VECTOR,
            query_filter=self._filter(module),
            limit=limit,
            with_payload=True,
        )
        return [_hit(point, "sparse") for point in response.points]

    def search_hybrid(
        self,
        dense: list[float],
        sparse: SparseVectorData,
        limit: int,
        module: str = "",
    ) -> list[dict]:
        """dense + sparse 各取 prefetch，Qdrant 原生 RRF 融合（k=60）。"""
        from qdrant_client import models

        response = self.client.query_points(
            COLLECTION,
            prefetch=[
                models.Prefetch(query=dense, using=DENSE_VECTOR, limit=limit * 4),
                models.Prefetch(
                    query=models.SparseVector(
                        indices=sparse.indices, values=sparse.values
                    ),
                    using=SPARSE_VECTOR,
                    limit=limit * 4,
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            query_filter=self._filter(module),
            limit=limit,
            with_payload=True,
        )
        return [_hit(point, "rrf") for point in response.points]

    def find_method(self, class_name: str, method_name: str) -> dict | None:
        """按类名 + 方法名取一条 point（含向量），用于「以方法找相似」。"""
        from qdrant_client import models

        records, _ = self.client.scroll(
            COLLECTION,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="class_name", match=models.MatchValue(value=class_name)
                    ),
                    models.FieldCondition(
                        key="method_name", match=models.MatchValue(value=method_name)
                    ),
                ]
            ),
            limit=5,
            with_payload=True,
            with_vectors=True,
        )
        if not records:
            return None
        record = records[0]
        payload = dict(record.payload or {})
        vectors = record.vector if isinstance(record.vector, dict) else {}
        return {
            "id": record.id,
            "payload": payload,
            "dense": list(vectors.get(DENSE_VECTOR) or []),
        }

    def get(self, point: int) -> dict | None:
        records = self.client.retrieve(
            COLLECTION, ids=[point], with_payload=True, with_vectors=False
        )
        if not records:
            return None
        return dict(records[0].payload or {})
