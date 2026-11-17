#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cross-Encoder 精排（检索的第二阶段）。

混合检索先用 FTS + 向量召回方法级候选，这里再用 ``BAAI/bge-reranker-v2-m3``
对 ``(问题, 类名+方法名+注释+方法体)`` 逐对打分，按相关性重排 top-K。模型是
多语言的，能直接处理「中文问题 ↔ Objective-C/Swift 代码」这种跨模态匹配。

CPU 上每对约 0.7s，所以分数会缓存到 ``embedding_cache.db.rerank_cache``，
同一问题重复问不再重复推理。

默认用 INT8 版（约 0.57 GB），CPU 上比 fp32 快且精度损失很小。
没装 ``fastembed`` 或缺模型文件时抛 :class:`RerankUnavailable`，调用方
回退到规则排序，不影响检索可用性。
"""

from __future__ import annotations

import math
import os
from pathlib import Path

from .embedding import EmbeddingCache, cache_key

RERANK_MODEL = "BAAI/bge-reranker-v2-m3-int8"
# 精排是方法级的：FTS 方法命中取前 30 个，再并上向量 top-40 的方法。
# 不能只拿「类 + 向量最相近的方法」去精排——向量最近的方法可能是
# tb_updateMonitorScreen 这类无关实现，会把真正干活的 loadQuotesForCategory:
# 挤出榜单。候选太少还会漏掉 FTS 排 23、向量排 35 的
# TBFinanceHTTP.attemptGETAtIndex:...（它精排分其实排第 2）。
METHOD_RERANK_TOP = 30
VECTOR_RERANK_TOP = 40

INSTALL_HINT = (
    "精排依赖未安装：python3 -m pip install fastembed\n"
    "首次运行会下载 reranker；直连 huggingface.co 失败时可加 "
    "HF_ENDPOINT=https://hf-mirror.com"
)


class RerankUnavailable(RuntimeError):
    """缺少 fastembed 或 reranker 模型无法加载。"""


def sigmoid(value: float) -> float:
    """把 Cross-Encoder 的 logit 压到 0-1；极端值防溢出。"""
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exp = math.exp(value)
    return exp / (1.0 + exp)


class CrossEncoderReranker:
    def __init__(
        self,
        model: str | None = None,
        cache_dir: str | Path | None = None,
        cache: EmbeddingCache | None = None,
    ) -> None:
        self.model = model or os.environ.get("IOS_QA_RERANK_MODEL") or RERANK_MODEL
        self.cache_dir = cache_dir
        self.cache = cache
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
        except ImportError as exc:  # pragma: no cover - 环境相关
            raise RerankUnavailable(INSTALL_HINT) from exc
        try:
            self._model = TextCrossEncoder(
                model_name=self.model,
                cache_dir=str(self.cache_dir) if self.cache_dir else None,
            )
        except Exception as exc:  # pragma: no cover - 网络/模型相关
            raise RerankUnavailable(
                f"加载 reranker 失败（{self.model}）：{exc}\n{INSTALL_HINT}"
            ) from exc
        return self._model

    def scores(self, query: str, documents: list[str]) -> list[float]:
        """返回与 ``documents`` 等长的相关性分数（已 sigmoid 到 0-1）。

        CPU 上每对约 0.7s，所以按 ``(model, query, document)`` 缓存到
        ``embedding_cache.db.rerank_cache``：同一问题重复问、或者 eval 重跑时
        不再重复推理；文档（方法体/注释）变了 key 自然失效。
        """
        if not documents:
            return []
        keys = [cache_key(self.model, f"{query}\x1f{document}") for document in documents]
        cached = self.cache.get_rerank(keys) if self.cache is not None else {}
        missing = [
            (key, document)
            for key, document in zip(keys, documents)
            if key not in cached
        ]
        if missing:
            model = self._load()
            fresh: list[tuple[str, float]] = []
            pairs = [(query, document) for _, document in missing]
            for (key, _), value in zip(missing, model.rerank_pairs(pairs)):
                score = sigmoid(float(value))
                cached[key] = score
                fresh.append((key, score))
            if self.cache is not None:
                self.cache.put_rerank(fresh)
        return [cached[key] for key in keys]
