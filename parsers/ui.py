#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Storyboard / XIB 解析：补 UI 入口与页面跳转关系。

只做保守解析：拿 ``customClass``、``storyboardIdentifier``、``initialViewController``
和 ``segue``，不尝试还原完整视图层级。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from ..config import ProjectProfile
from ..discover import SourceFile
from ..model import EntrypointRec, NavigationRec, ParseResult, SymbolRec

_UI_TAGS = {
    "viewController", "tableViewController", "collectionViewController",
    "navigationController", "tabBarController", "splitViewController",
    "pageViewController", "hostingController",
}


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_line(text: str, pattern: str) -> int:
    match = re.search(pattern, text)
    if not match:
        return 0
    return text.count("\n", 0, match.start()) + 1


def _collect_classes(root: ET.Element) -> dict[str, str]:
    mapping: dict[str, str] = {}

    def walk(node: ET.Element) -> None:
        custom = node.attrib.get("customClass", "")
        node_id = node.attrib.get("id", "")
        if custom and node_id:
            mapping[node_id] = custom
        for child in node:
            walk(child)

    walk(root)
    return mapping


def parse_ui(text: str, file: SourceFile, profile: ProjectProfile) -> ParseResult:
    result = ParseResult()
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return result

    rel = file.rel_path
    initial_id = root.attrib.get("initialViewController", "")
    class_by_id = _collect_classes(root)
    seen_entry: set[str] = set()

    def walk(node: ET.Element, parent_class: str = "") -> None:
        tag = _strip_ns(node.tag)
        custom = node.attrib.get("customClass", "")
        ident = node.attrib.get("storyboardIdentifier") or node.attrib.get("id") or ""
        node_id = node.attrib.get("id", "")
        current_class = custom or parent_class

        if tag in _UI_TAGS and custom and node_id not in seen_entry:
            seen_entry.add(node_id)
            line = _find_line(text, rf'customClass="{re.escape(custom)}"')
            result.entrypoints.append(
                EntrypointRec(
                    kind="root" if node_id == initial_id else "storyboard",
                    class_name=custom,
                    identifier=ident,
                    source="storyboard",
                    module=file.module,
                    file_path=rel,
                    line_number=line,
                    confidence="high",
                )
            )
            result.symbols.append(
                SymbolRec(
                    symbol_key=f"ui:{rel}:{custom}:{node_id}",
                    class_name=custom,
                    kind="storyboard",
                    language="ui",
                    module=file.module,
                    file_path=rel,
                    line_start=line,
                    line_end=line,
                    doc_comment=node.attrib.get("userLabel", ""),
                )
            )

        if tag == "segue":
            destination = node.attrib.get("destination", "")
            kind = node.attrib.get("kind", "segue")
            identifier = node.attrib.get("identifier", "")
            source_class = current_class or parent_class
            dest = class_by_id.get(destination, "")
            if source_class and (dest or identifier):
                result.navigation.append(
                    NavigationRec(
                        from_class=source_class,
                        to_class=dest,
                        kind="segue",
                        identifier=identifier or kind,
                        module=file.module,
                        file_path=rel,
                        line_number=0,
                    )
                )

        for child in node:
            walk(child, current_class)

    walk(root)
    return result
