#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""解析层的规范化数据模型。

解析器只产出这些记录，不直接碰 SQL；存储层负责落表与聚合。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SymbolRec:
    symbol_key: str
    class_name: str
    kind: str  # class / struct / enum / protocol / category / extension / implementation
    language: str  # objc / swift
    module: str
    file_path: str
    line_start: int = 0
    line_end: int = 0
    superclass: str = ""
    protocols: list[str] = field(default_factory=list)
    properties: list[dict] = field(default_factory=list)
    doc_comment: str = ""
    interface_text: str = ""
    extension_of: str = ""
    category_name: str = ""


@dataclass
class MethodRec:
    symbol_key: str
    class_name: str
    method_name: str
    signature: str
    language: str
    module: str
    file_path: str
    line_start: int = 0
    line_end: int = 0
    is_class_method: bool = False
    is_extension: bool = False
    has_body: bool = True
    doc_comment: str = ""
    body: str = ""


@dataclass
class DepRec:
    from_class: str
    dep_class: str
    dep_kind: str  # superclass / protocol / import / property / singleton / instantiate / bridge
    module: str
    file_path: str
    line_number: int = 0


@dataclass
class ApiCallRec:
    class_name: str
    method_name: str
    api_kind: str  # http / manager / url / notification?
    url_pattern: str
    http_method: str
    response_type: str
    context_snippet: str
    module: str
    file_path: str
    line_number: int = 0


@dataclass
class NotificationRec:
    class_name: str
    notification_name: str
    observer_selector: str
    register_method: str
    register_line: int
    remove_method: str
    remove_line: int
    file_path: str
    module: str
    language: str
    is_balanced: int = 0  # 1 配对 / 0 未配对 / -1 无法判定（RAC 等）


@dataclass
class CallRec:
    caller_class: str
    caller_method: str
    callee_class: str
    callee_name: str
    kind: str  # message / static / init / super / self
    confidence: str  # high / medium / low
    module: str
    file_path: str
    line_number: int = 0


@dataclass
class EntrypointRec:
    kind: str  # app / storyboard / root / scene
    class_name: str
    identifier: str
    source: str
    module: str
    file_path: str
    line_number: int = 0
    confidence: str = "high"


@dataclass
class NavigationRec:
    from_class: str
    to_class: str
    kind: str  # push / present / root / tab / segue / instantiate
    identifier: str
    module: str
    file_path: str
    line_number: int = 0


@dataclass
class ParseResult:
    symbols: list[SymbolRec] = field(default_factory=list)
    methods: list[MethodRec] = field(default_factory=list)
    deps: list[DepRec] = field(default_factory=list)
    api_calls: list[ApiCallRec] = field(default_factory=list)
    notifications: list[NotificationRec] = field(default_factory=list)
    calls: list[CallRec] = field(default_factory=list)
    entrypoints: list[EntrypointRec] = field(default_factory=list)
    navigation: list[NavigationRec] = field(default_factory=list)

    def extend(self, other: "ParseResult") -> None:
        self.symbols.extend(other.symbols)
        self.methods.extend(other.methods)
        self.deps.extend(other.deps)
        self.api_calls.extend(other.api_calls)
        self.notifications.extend(other.notifications)
        self.calls.extend(other.calls)
        self.entrypoints.extend(other.entrypoints)
        self.navigation.extend(other.navigation)


def make_symbol_key(rel_path: str, line_start: int, kind: str, class_name: str) -> str:
    return f"decl:{rel_path}:{line_start}:{kind}:{class_name}"
