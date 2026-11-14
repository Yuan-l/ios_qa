#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检索文本归一化。

为什么需要这一层：SQLite FTS5 自带的 ``unicode61`` 会把一整段连续中文
当成 **一个 token**，实测::

    INSERT '首页数据加载逻辑'; MATCH '首页' -> 0 命中

参考实现靠 LIKE 兜底掩盖了这个问题。这里改成写入时预分词：

- 中文：单字 + 相邻 bigram（``首页`` / ``页数`` / ``数据`` …）
- 英文/代码标识符：按驼峰、下划线、数字边界拆词并小写
- 两者混合时分别处理

这样 2 个汉字、驼峰类名、方法名都能命中，且中文不依赖 jieba 这类词典。
"""

from __future__ import annotations

import re

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_CAMEL_RE = re.compile(
    r"[A-Z]+(?=[A-Z][a-z]|\d|\b)|[A-Z]?[a-z]+|\d+|_+"
)
_WORD_SPLIT_RE = re.compile(r"[^0-9A-Za-z_\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_IDENT_SPLIT_RE = re.compile(r"[^0-9A-Za-z]+")

MAX_TOKEN_LEN = 64
MAX_SEARCH_TEXT_CHARS = 24000


def _cjk_tokens(run: str) -> list[str]:
    """单字 + bigram。单字保证 1 字查询可用，bigram 保证 2 字查询可用。"""
    chars = list(run)
    out: list[str] = []
    out.extend(chars)
    out.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
    return out


def _ascii_tokens(word: str) -> list[str]:
    """把标识符拆成可检索的小写词。

    ``TDURLSessionNetworkClient`` -> ``td url session network client``
    ``TBMonitorNetwork.swift`` -> ``tb monitor network swift``
    """
    parts: list[str] = []
    for chunk in _IDENT_SPLIT_RE.split(word):
        if not chunk:
            continue
        camel = [p for p in _CAMEL_RE.findall(chunk) if p and not p.startswith("_")]
        if camel:
            parts.extend(camel)
        else:
            parts.append(chunk)
    # 全大写缩写会粘成一段（TDURL），按常见前缀再拆一次，保证 ``url`` 可检索
    out: list[str] = []
    for part in parts:
        split = _split_known_prefix(part)
        out.extend(p.lower() for p in split)
    return out


_KNOWN_PREFIXES = (
    "TB", "TD", "LM", "UI", "NS", "CG", "CA", "CF", "AV", "WK", "QL", "SK",
    "MK", "CL", "PH", "XC",
)


def _split_known_prefix(part: str) -> list[str]:
    if len(part) <= 4:
        return [part]
    for prefix in _KNOWN_PREFIXES:
        if part.startswith(prefix) and part[len(prefix)].isupper():
            rest = part[len(prefix):]
            if rest.isupper() and len(rest) >= 2:
                return [prefix, rest]
    return [part]


def tokenize(text: str) -> list[str]:
    """把任意文本切成检索 token（保序去重）。"""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []

    def add(token: str) -> None:
        if not token or len(token) > MAX_TOKEN_LEN:
            return
        if token in seen:
            return
        seen.add(token)
        out.append(token)

    for raw in _WORD_SPLIT_RE.split(text):
        if not raw:
            continue
        if _CJK_RE.fullmatch(raw):
            for tok in _cjk_tokens(raw):
                add(tok)
            continue
        # 混合串（如 TB首页）：按中文段和非中文段再拆一次
        pos = 0
        for match in _CJK_RE.finditer(raw):
            if match.start() > pos:
                for tok in _ascii_tokens(raw[pos:match.start()]):
                    add(tok)
            for tok in _cjk_tokens(match.group()):
                add(tok)
            pos = match.end()
        if pos < len(raw):
            for tok in _ascii_tokens(raw[pos:]):
                add(tok)
    return out


def build_search_text(*parts: str, limit: int = MAX_SEARCH_TEXT_CHARS) -> str:
    """拼出写入 FTS 的检索列。"""
    tokens: list[str] = []
    for part in parts:
        if not part:
            continue
        tokens.extend(tokenize(part))
        if sum(len(t) + 1 for t in tokens) >= limit:
            break
    # 全局去重，避免大段方法体把索引撑爆
    seen: set[str] = set()
    out: list[str] = []
    size = 0
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
        size += len(token) + 1
        if size >= limit:
            break
    return " ".join(out)


def _quote(token: str) -> str:
    return '"' + token.replace('"', '""') + '"'


def fts_expr(query: str, op: str = "AND", prefix: bool = False) -> str | None:
    """把用户输入转成 FTS5 MATCH 表达式。

    每个 token 单独加引号，避免 ``and`` / ``or`` / ``not`` 被当成操作符。
    """
    tokens = tokenize(query)
    if not tokens:
        return None
    rendered = []
    for token in tokens[:24]:
        rendered.append(_quote(token) + ("*" if prefix else ""))
    return f" {op} ".join(rendered)


def like_pattern(query: str) -> str:
    """LIKE 兜底用的模式（转义 ``%`` / ``_``）。"""
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def snippet(text: str, query: str, width: int = 120) -> str:
    """从文本里截一段命中上下文。"""
    if not text:
        return ""
    for token in tokenize(query):
        if len(token) < 2 or token.isdigit():
            continue
        idx = text.lower().find(token.lower())
        if idx >= 0:
            start = max(0, idx - width // 2)
            end = min(len(text), idx + len(token) + width // 2)
            prefix = "…" if start > 0 else ""
            suffix = "…" if end < len(text) else ""
            return prefix + text[start:end].replace("\n", " ").strip() + suffix
    return text[:width].replace("\n", " ").strip()
