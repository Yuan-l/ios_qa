#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""业务别名扩展。

用途和参考方案的 ``route_synonyms.json`` 一致：把「行情 / 报价 / quote」
这类同一业务概念的词互相关联，查询时互相扩展，避免只用字面词做 FTS。

只做有界扩展：一次查询最多产生 ``MAX_EXPANSIONS`` 条替代查询，
避免长查询把检索放大成笛卡尔积。
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import ProjectProfile

DEFAULT_FILE = Path(__file__).with_name("synonyms.json")
MAX_EXPANSIONS = 8


def load_groups(profile: ProjectProfile | None = None) -> list[list[str]]:
    """优先读工程自己的 ``ios_qa/synonyms.json``，否则用包内默认。"""
    candidates: list[Path] = []
    if profile is not None:
        candidates.append(profile.root / "ios_qa" / "synonyms.json")
    candidates.append(DEFAULT_FILE)
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        groups = data.get("groups") or []
        return [
            [str(term) for term in group if str(term).strip()]
            for group in groups
            if isinstance(group, list) and len(group) > 1
        ]
    return []


def expand_query(
    query: str, groups: list[list[str]], limit: int = MAX_EXPANSIONS
) -> list[dict]:
    """返回替代查询列表：``[{"from": 别名, "to": 同义词, "query": 替代查询}]``。"""
    text = (query or "").strip()
    if not text or not groups:
        return []
    lowered = text.lower()
    expansions: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        hits = [term for term in group if term and term.lower() in lowered]
        if not hits:
            continue
        for alias in hits:
            for other in group:
                if other == alias:
                    continue
                key = (alias.lower(), other.lower())
                if key in seen:
                    continue
                seen.add(key)
                replaced = text.replace(alias, other)
                if replaced == text:
                    replaced = f"{text} {other}"
                expansions.append({"from": alias, "to": other, "query": replaced})
                if len(expansions) >= limit:
                    return expansions
    return expansions
