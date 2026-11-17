#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""相似度阈值标定。

为什么需要这个命令：余弦相似度是**相对量**，同一对文本在不同模型/不同索引
文本模板下的分数会整体平移（实测 Qwen3 下无关方法之间底噪约 0.40，而
最后一 token pooling 让「方法体查自身」只有 0.75~0.98）。因此
``min_similarity`` 这类常数不能照抄参考实现，必须按当前模型与模板标定。

做法：抽 N 个不同类的方法，把方法体当查询检索一次，统计「自身」「第二名」
「间隔」的分布，再给出建议阈值：
- ``min_score``：p05(自身) 再留 0.02 余量，保证绝大多数方法能查到自己；
- ``min_margin``：p05(top1 − top2)，保证首选与次选有可分辨的差距。

绝对阈值只能挡住「完全不像」的结果，真正的区分靠 margin，所以两个都要给。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import ProjectProfile
from .embedding import EMBED_MODEL, MIN_BODY_CHARS, template_id
from .query import QueryService

DEFAULT_SAMPLES = 30
# 太短的方法体（getter 包装）向量不稳定，标定会失真。
MIN_CALIBRATION_CHARS = max(300, MIN_BODY_CHARS)


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(ratio * (len(ordered) - 1))))
    return ordered[index]


def _stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {"min": 0.0, "p05": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}
    return {
        "min": round(min(values), 4),
        "p05": round(_percentile(values, 0.05), 4),
        "p50": round(_percentile(values, 0.50), 4),
        "p95": round(_percentile(values, 0.95), 4),
        "max": round(max(values), 4),
    }


def _pick_samples(service: QueryService, samples: int) -> list[Any]:
    """不同类各取一个方法，避免同族实现（saveImage / saveImageToLocal）扎堆。"""
    rows = service.conn.execute(
        "SELECT class_name, method_name, body FROM methods "
        "WHERE has_body = 1 AND length(body) >= ? ORDER BY id",
        (MIN_CALIBRATION_CHARS,),
    ).fetchall()
    picked: list[Any] = []
    seen: set[str] = set()
    for row in rows:
        if row["class_name"] in seen:
            continue
        seen.add(row["class_name"])
        picked.append(row)
        if len(picked) >= samples:
            break
    return picked


def calibrate(
    profile: ProjectProfile,
    db_path: str | Path | None = None,
    samples: int = DEFAULT_SAMPLES,
) -> dict:
    samples = max(3, min(int(samples), 200))
    result: dict = {
        "samples_requested": samples,
        "template_id": template_id(),
        "model": "",
        "stats": {},
        "suggested": {},
        "cases": [],
    }
    with QueryService(profile, db_path) as service:
        available, hint = service._vector_status()
        if not available:
            return {**result, "available": False, "hint": hint}
        result["available"] = True
        result["model"] = EMBED_MODEL
        selfs: list[float] = []
        seconds: list[float] = []
        margins: list[float] = []
        for row in _pick_samples(service, samples):
            data = service.search_similar_snippet(
                row["body"] or "", limit=5, min_similarity=0.0
            )
            hits = data.get("results") or []
            if not hits:
                continue
            top = hits[0]
            is_self = (
                top["class_name"] == row["class_name"]
                and top["method_name"] == row["method_name"]
            )
            rest = [float(hit["similarity"]) for hit in hits[1:]]
            selfs.append(float(top["similarity"]) if is_self else 0.0)
            seconds.append(rest[0] if rest else 0.0)
            margins.append(
                float(top["similarity"]) - rest[0]
                if rest
                else float(top["similarity"])
            )
            result["cases"].append(
                {
                    "target": f"{row['class_name']}.{row['method_name']}",
                    "self_first": is_self,
                    "self": round(float(top["similarity"]), 4),
                    "second": round(rest[0], 4) if rest else 0.0,
                }
            )
        result["stats"] = {
            "self": _stats(selfs),
            "second": _stats(seconds),
            "margin": _stats(margins),
            "self_misses": sum(1 for row in result["cases"] if not row["self_first"]),
            "count": len(result["cases"]),
        }
        self_stats = result["stats"]["self"]
        margin_stats = result["stats"]["margin"]
        min_score = round(max(0.5, self_stats["p05"] - 0.02), 2)
        min_margin = round(max(0.0, margin_stats["p05"] - 0.01), 2)
        result["suggested"] = {
            "min_score": min_score,
            "min_margin": min_margin,
            "check_template": {
                "kind": "vector_top",
                "value": "<类名>.<方法名>",
                "min_margin": min_margin,
            },
            "note": (
                "绝对值随模型/模板平移，优先用 min_margin 断言排序；"
                "min_score 只用于挡掉完全不相干的结果"
            ),
        }
    return result
