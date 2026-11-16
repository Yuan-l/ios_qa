#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cross-Encoder 精排（检索的第二阶段）。

混合检索先用 FTS + 向量召回候选，这里再用 ``BAAI/bge-reranker-v2-m3``
对 ``(问题, 类名+方法体)`` 逐对打分，按相关性重排 top-K。模型是多语言的，
能直接处理「中文问题 ↔ Objective-C/Swift 代码」这种跨模态匹配。

默认用 INT8 版（约 0.57 GB），CPU 上比 fp32 快且精度损失很小。
没装 ``fastembed`` 或缺模型文件时抛 :class:`RerankUnavailable`，调用方
回退到规则排序，不影响检索可用性。
"""

from __future__ import annotations

import math
from pathlib import Path

RERANK_MODEL = "BAAI/bge-reranker-v2-m3-int8"
# 规则召回后送进 Cross-Encoder 的候选数：要覆盖向量第 20 名左右的方法
# （例如「网络失败重试」的 TBFinanceHTTP，cosine 排名不高，但精排分最高）。
RERANK_CANDIDATES = 20
# 额外补进精排的向量 top 类：规则分低的语义正确项（如 cosine 第 22 的
# TBFinanceHTTP.attemptGETAtIndex:...）也要让 Cross-Encoder 看到。
VECTOR_RERANK_TOP = 30

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
        model: str = RERANK_MODEL,
        cache_dir: str | Path | None = None,
    ) -> None:
        self.model = model
        self.cache_dir = cache_dir
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
        """返回与 ``documents`` 等长的相关性分数（已 sigmoid 到 0-1）。"""
        if not documents:
            return []
        model = self._load()
        return [sigmoid(float(value)) for value in model.rerank(query, documents)]
