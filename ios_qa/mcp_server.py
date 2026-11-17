#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP stdio server。

只做协议适配：每个工具 → :class:`ios_qa.query.QueryService` 的一次调用，
结果统一序列化成 JSON 字符串。所有业务逻辑留在查询层，后续接 Agent Loom
时可以直接复用，不需要经过 MCP。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from mcp.server.fastmcp import FastMCP

from .config import ProjectProfile
from .query import QueryService

_INSTRUCTIONS = """\
iOS 工程静态知识库。回答代码问题前优先用这里的工具定位，不要凭空猜测：
- 找功能/页面：find_feature（支持中文描述）；同时要语义补位时用
  hybrid_find_feature（FTS + 向量，按类合并）
- 查类结构/调用链/通知/API：trace_class
- 查方法：search_methods；查谁调用：find_callers；评估改动影响：impact_analysis
- 找复制粘贴：find_similar_code（单方法）/ find_duplicates（全仓库聚类）
- 语义搜索：vector_search（自然语言）/ search_similar_snippet（代码片段）；
  find_similar_code(mode="semantic") 用向量找语义近邻
- 需要精确行号时用 read_source 读原文件
所有结果都带 file_path 与行号。
"""

mcp = FastMCP("ios_qa", instructions=_INSTRUCTIONS)

_profile: ProjectProfile | None = None
_db_path: str | None = None


def configure(profile: ProjectProfile, db_path: str | Path | None = None) -> None:
    global _profile, _db_path
    _profile = profile
    _db_path = str(db_path) if db_path else str(profile.resolved_db_path)


def _current_profile() -> ProjectProfile:
    if _profile is None:
        configure(ProjectProfile.load())
    assert _profile is not None
    return _profile


@contextmanager
def _service() -> Iterator[QueryService]:
    profile = _current_profile()
    service = QueryService(profile, _db_path)
    try:
        yield service
    finally:
        service.close()


def _dump(data) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


@mcp.tool()
def kb_info() -> str:
    """索引健康信息：构建时间、分支、文件/类/方法数量、警告。"""
    with _service() as service:
        return _dump(service.info())


@mcp.tool()
def find_feature(query: str, limit: int = 10) -> str:
    """按功能描述找类/页面（支持中文，如「首页 数据」「网络 请求」）。"""
    with _service() as service:
        return _dump(service.find_feature(query, limit=limit))


@mcp.tool()
def hybrid_find_feature(
    query: str, limit: int = 5, module: str = "", rerank: bool = True
) -> str:
    """FTS + 向量混合检索：精确词命中优先，语义相近的实现补位。

    结果按类去重，``sources`` 标明来自 ``fts`` / ``vector``，
    ``vector_method`` 给出该类下语义最接近的方法；默认对方法级候选做
    Cross-Encoder 精排，``rerank_method`` 是真正被精排选中的方法，
    ``score`` 是精排概率、``rule_score`` 保留规则分。
    ``rerank=False`` 只看规则排序（快，适合批量扫）。
    """
    with _service() as service:
        return _dump(
            service.hybrid_find_feature(
                query, limit=limit, module=module, rerank=rerank
            )
        )


@mcp.tool()
def search_symbol(name: str, limit: int = 10) -> str:
    """按类名/方法名查符号，返回继承、协议、属性、方法签名与文件行号。"""
    with _service() as service:
        return _dump(service.search_symbol(name, limit=limit))


@mcp.tool()
def trace_class(class_name: str, limit: int = 80, module: str = "") -> str:
    """查类数据流：继承链、方法、依赖/被依赖、API、通知、调用方/被调方、页面跳转。

    同名类出现在多个模块时默认只看方法最多的声明点，返回 ``ambiguous`` 与
    ``candidates``；传 ``module`` 可以切到指定模块。
    """
    with _service() as service:
        return _dump(service.trace_class(class_name, limit=limit, module=module))


@mcp.tool()
def search_methods(
    query: str = "",
    class_name: str = "",
    module: str = "",
    limit: int = 20,
) -> str:
    """全文检索方法体/签名/注释，可按类名或模块过滤。"""
    with _service() as service:
        return _dump(
            service.search_methods(
                query, class_name=class_name, module=module, limit=limit
            )
        )


@mcp.tool()
def find_callers(target: str, limit: int = 50) -> str:
    """查谁调用了某个类或 ``类.方法``。"""
    with _service() as service:
        return _dump(service.find_callers(target, limit=limit))


@mcp.tool()
def impact_analysis(target: str, depth: int = 2, limit: int = 200) -> str:
    """改这个类/方法会影响谁（反向 BFS，返回分层影响面）。"""
    with _service() as service:
        return _dump(service.impact_analysis(target, depth=depth, limit=limit))


@mcp.tool()
def find_navigation(target: str, depth: int = 6, limit: int = 20) -> str:
    """页面是从哪个入口、经过哪些跳转到达的（含低置信度的实例化边）。"""
    with _service() as service:
        return _dump(service.find_navigation(target, depth=depth, limit=limit))


@mcp.tool()
def find_notifications(
    name: str = "", module: str = "", unbalanced_only: bool = False, limit: int = 100
) -> str:
    """通知注册/移除查询；unbalanced_only=true 只看注册后没有移除的。"""
    with _service() as service:
        return _dump(
            service.find_notifications(
                name=name,
                module=module,
                balanced=0 if unbalanced_only else None,
                limit=limit,
            )
        )


@mcp.tool()
def find_similar_code(
    target: str = "",
    limit: int = 10,
    min_similarity: float | None = None,
    mode: str = "lexical",
    class_name: str = "",
    method_name: str = "",
    module: str = "",
) -> str:
    """找相近的方法。

    - ``mode=lexical``（默认）：词法指纹，改过变量名/注释的复制粘贴也能认出来；
      similarity 是归一化 token 的 shingle 集合 Jaccard。
    - ``mode=semantic``：Qdrant 语义近邻（Qwen3-1024 + BM42），返回余弦
      similarity 与复用建议；可传 ``class_name`` + ``method_name`` 或
      ``target="Class.method"``。
    """
    with _service() as service:
        return _dump(
            service.find_similar_code(
                target,
                limit=limit,
                min_similarity=min_similarity,
                mode=mode,
                class_name=class_name,
                method_name=method_name,
                module=module,
            )
        )


@mcp.tool()
def vector_search(query: str, limit: int = 5, module: str = "") -> str:
    """自然语言语义搜索方法体（dense + BM42 sparse，Qdrant RRF 融合）。"""
    with _service() as service:
        return _dump(
            service.vector_search(query, limit=limit, module=module)
        )


@mcp.tool()
def search_similar_snippet(
    code: str, limit: int = 5, module: str = "", min_similarity: float = 0.75
) -> str:
    """任意代码片段找项目里语义相近的方法（返回 source_code 与复用建议）。

    走独立的 ``dense_body`` 片段通道：索引侧与查询侧都是裸方法体文本，贴一段
    已索引方法的代码时自相似≈1.0；默认阈值 0.75 只挡掉明显不相干的方法。
    """
    with _service() as service:
        return _dump(
            service.search_similar_snippet(
                code,
                limit=limit,
                module=module,
                min_similarity=min_similarity,
            )
        )


@mcp.tool()
def find_duplicates(
    min_lines: int = 6,
    min_similarity: float = 0.8,
    limit: int = 20,
    class_name: str = "",
) -> str:
    """重复代码聚类：精确复制 + 近似复制（默认 ≥80% 结构相同、≥6 行）。

    返回 ``clusters``，每组带 ``members``（类.方法 + 文件行号）与
    ``cross_module``；``class_name`` 可只看某个类。
    """
    with _service() as service:
        return _dump(
            service.find_duplicates(
                min_lines=min_lines,
                min_similarity=min_similarity,
                limit=limit,
                class_name=class_name,
            )
        )


@mcp.tool()
def read_source(file_path: str, focus: str = "", context: int = 40) -> str:
    """按行号读取源码；给 focus 时定位到首次命中附近。"""
    with _service() as service:
        return _dump(service.read_source(file_path, focus=focus, context=context))


@mcp.tool()
def list_pages(module: str = "", limit: int = 100) -> str:
    """列出页面（UIViewController 子类）入口。"""
    with _service() as service:
        return _dump(service.list_pages(module=module, limit=limit))


@mcp.tool()
def list_module(module: str) -> str:
    """模块概览：类/方法/页面数量、文件行数、方法最多的类。"""
    with _service() as service:
        return _dump(service.list_module(module))


@mcp.tool()
def search_qa(query: str, related_class: str = "", limit: int = 10) -> str:
    """检索历史问答知识库（中文可用）。"""
    with _service() as service:
        return _dump(
            service.search_qa(query, related_class=related_class, limit=limit)
        )


@mcp.tool()
def save_qa(
    question: str,
    answer: str,
    related_class: str = "",
    related_module: str = "",
    related_file: str = "",
    tags: str = "",
    author: str = "",
) -> str:
    """保存一条问答到历史知识库。"""
    with _service() as service:
        return _dump(
            service.save_qa(
                question,
                answer,
                related_class=related_class,
                related_module=related_module,
                related_file=related_file,
                tags=tags,
                author=author,
            )
        )


def run_mcp(profile: ProjectProfile, db_path: str | Path | None = None) -> None:
    configure(profile, db_path)
    mcp.run(transport="stdio")
