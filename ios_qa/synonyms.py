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

from .config import CONFIG_DIR_NAME, ProjectProfile

MAX_EXPANSIONS = 8
# 索引期标签：一个方法最多带 3 组同义词、12 个词，避免标签压过方法体。
TAG_GROUP_LIMIT = 3
TAG_TERM_LIMIT = 12


def _candidate_paths(profile: ProjectProfile | None) -> list[Path]:
    root = profile.root if profile is not None else Path.cwd()
    return [
        root / CONFIG_DIR_NAME / "synonyms.json",
        root / "ios_qa" / "synonyms.json",
    ]


def load_groups(profile: ProjectProfile | None = None) -> list[list[str]]:
    """读工程自己的 ``.ios_qa/synonyms.json``（兼容旧布局 ``ios_qa/``）。"""
    for path in _candidate_paths(profile):
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


def match_tags(
    text: str,
    groups: list[list[str]],
    max_groups: int = TAG_GROUP_LIMIT,
    max_terms: int = TAG_TERM_LIMIT,
) -> list[str]:
    """索引期同义词标签：文本命中某组任一词时，返回该组里**尚未出现**的词。

    和查询期扩展互补：查询期扩展依赖查询里出现别名，这里把别名写进方法向量，
    让「崩溃 / 卡顿 / 行情」这类中文词能命中英文命名的类（``TBCrashReporter``、
    ``TDHangWatchdog``），即使问题里没有代码字面量。
    """
    lowered = (text or "").lower()
    if not lowered or not groups:
        return []
    tags: list[str] = []
    matched = 0
    for group in groups:
        if not any(term and term.lower() in lowered for term in group):
            continue
        matched += 1
        for term in group:
            if term.lower() in lowered or term in tags:
                continue
            tags.append(term)
            if len(tags) >= max_terms:
                return tags
        if matched >= max_groups:
            break
    return tags


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
