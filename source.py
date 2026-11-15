#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""源码词法工具：屏蔽注释/字符串、括号配对、行号换算、文档注释提取。

纯 Python 词法扫描，不依赖 tree-sitter。关键点：
- 结构分析跑在 **masked** 文本上，字符串/注释里的 ``{`` ``}`` 不会干扰配对；
- 需要字面量（URL、通知名、storyboard id）时用 ``mask_comments``，只屏蔽注释；
- 所有函数都保留字符偏移，便于把解析结果还原到真实行号。
"""

from __future__ import annotations

import bisect
import re
from functools import lru_cache


def mask_code(text: str, language: str = "objc") -> str:
    """屏蔽注释和字符串内容（保留换行与长度）。"""
    return _mask(text, language, mask_strings=True)


def mask_comments(text: str, language: str = "objc") -> str:
    """只屏蔽注释，保留字符串字面量。"""
    return _mask(text, language, mask_strings=False)


def _mask(text: str, language: str, mask_strings: bool) -> str:
    if not text:
        return text
    chars = list(text)
    n = len(text)
    i = 0
    state = "code"
    swift = language == "swift"

    def blank(a: int, b: int) -> None:
        for k in range(a, b):
            if chars[k] != "\n":
                chars[k] = " "

    while i < n:
        c = text[i]
        if state == "code":
            nxt = text[i + 1] if i + 1 < n else ""
            if c == "/" and nxt == "/":
                if mask_strings:
                    state = "line_comment"
                else:
                    state = "line_comment"
                blank(i, i + 2)
                i += 2
            elif c == "/" and nxt == "*":
                state = "block_comment"
                blank(i, i + 2)
                i += 2
            elif c == '"':
                # 即使不屏蔽字符串内容，也必须跟踪字符串状态，
                # 否则 ``https://`` 里的 ``//`` 会被误当成行注释。
                if swift and text.startswith('"""', i):
                    state = "multiline_string"
                    if mask_strings:
                        blank(i, i + 3)
                    i += 3
                else:
                    state = "string"
                    if mask_strings:
                        blank(i, i + 1)
                    i += 1
            elif c == "'":
                state = "char"
                if mask_strings:
                    blank(i, i + 1)
                i += 1
            else:
                i += 1
        elif state == "line_comment":
            if c == "\n":
                state = "code"
                i += 1
            else:
                blank(i, i + 1)
                i += 1
        elif state == "block_comment":
            if c == "*" and i + 1 < n and text[i + 1] == "/":
                blank(i, i + 2)
                i += 2
                state = "code"
            else:
                blank(i, i + 1)
                i += 1
        elif state == "string":
            if c == "\\" and i + 1 < n:
                if mask_strings:
                    blank(i, i + 2)
                i += 2
            elif c == '"':
                if mask_strings:
                    blank(i, i + 1)
                i += 1
                state = "code"
            else:
                if mask_strings:
                    blank(i, i + 1)
                i += 1
        elif state == "char":
            if c == "\\" and i + 1 < n:
                if mask_strings:
                    blank(i, i + 2)
                i += 2
            elif c == "'":
                if mask_strings:
                    blank(i, i + 1)
                i += 1
                state = "code"
            else:
                if mask_strings:
                    blank(i, i + 1)
                i += 1
        elif state == "multiline_string":
            if text.startswith('"""', i):
                if mask_strings:
                    blank(i, i + 3)
                i += 3
                state = "code"
            elif c == "\\" and i + 1 < n:
                if mask_strings:
                    blank(i, i + 2)
                i += 2
            else:
                if mask_strings:
                    blank(i, i + 1)
                i += 1
        else:  # pragma: no cover - defensive
            i += 1
    return "".join(chars)


CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def ui_strings(
    text: str,
    language: str = "objc",
    limit: int = 40,
    max_chars: int = 120,
) -> list[str]:
    """提取含中文的字符串字面量（页面标题、按钮文案、错误提示）。

    输入必须是 **已屏蔽注释** 的文本（:func:`mask_comments` 的结果），否则注释
    里的引号会被当成字面量。只保留含中文的、长度合理的串，用于把「用户看得见的
    文案」接进类级检索：``self.title = @"行情"`` 这类代码里类名是英文的页面，
    用户在知识库里往往用界面上的中文去找。
    """
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    n = len(text)
    i = 0
    swift = language == "swift"
    while i < n:
        if text[i] != '"':
            i += 1
            continue
        if swift and text.startswith('"""', i):
            end = text.find('"""', i + 3)
            if end < 0:
                break
            chunk = text[i + 3 : end]
            i = end + 3
        else:
            j = i + 1
            buf: list[str] = []
            while j < n:
                char = text[j]
                if char == "\\" and j + 1 < n:
                    buf.append(text[j : j + 2])
                    j += 2
                    continue
                if char == '"':
                    j += 1
                    break
                buf.append(char)
                j += 1
            chunk = "".join(buf)
            i = j
        chunk = " ".join(chunk.split())
        if not chunk or len(chunk) > max_chars or not CJK_RE.search(chunk):
            continue
        if chunk in seen:
            continue
        seen.add(chunk)
        out.append(chunk)
        if len(out) >= limit:
            break
    return out


class LineMap:
    """字符偏移 <-> 1-based 行号。"""

    __slots__ = ("text", "starts")

    def __init__(self, text: str) -> None:
        self.text = text
        starts = [0]
        idx = text.find("\n")
        while idx >= 0:
            starts.append(idx + 1)
            idx = text.find("\n", idx + 1)
        self.starts = starts

    def line_of(self, offset: int) -> int:
        if offset <= 0:
            return 1
        offset = min(offset, len(self.text))
        return bisect.bisect_right(self.starts, offset)

    def offset_of(self, line: int) -> int:
        line = max(1, min(line, len(self.starts)))
        return self.starts[line - 1]

    def line_text(self, line: int) -> str:
        start = self.offset_of(line)
        end = self.starts[line] if line < len(self.starts) else len(self.text)
        return self.text[start:end].rstrip("\n")


def find_matching_brace(masked: str, open_index: int) -> int:
    """返回与 ``open_index`` 处 ``{`` 配对的 ``}`` 的下标；找不到返回 -1。"""
    if open_index < 0 or open_index >= len(masked) or masked[open_index] != "{":
        return -1
    depth = 0
    for i in range(open_index, len(masked)):
        ch = masked[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def find_body_open(masked: str, start: int, limit: int = 6000) -> int:
    """从声明起点向后找方法体的 ``{``。

    遇到 ``;``、``@end`` 或超出 ``limit`` 视为没有方法体（协议/头文件声明）。
    """
    end = min(len(masked), start + limit)
    for i in range(start, end):
        ch = masked[i]
        if ch == "{":
            return i
        if ch == ";":
            # ObjC 声明结束；Swift 里 ; 极少出现在签名中
            return -1
        if ch == "}" and i > start:
            return -1
        if masked.startswith("@end", i):
            return -1
    return -1


_COMMENT_LINE_RE = re.compile(r"^\s*//[/!]?\s?(.*)$")
_BLOCK_START_RE = re.compile(r"/\*[*!]?")


def leading_comment(lines: list[str], line_index: int, max_lines: int = 40) -> str:
    """向上收集紧邻声明前的文档注释。

    ``line_index`` 为 0-based 的声明行号。支持 ``//`` / ``///`` / ``/* */``。
    """
    if line_index <= 0:
        return ""
    collected: list[str] = []
    i = line_index - 1
    steps = 0

    # 先处理块注释结尾
    while i >= 0 and steps < max_lines:
        stripped = lines[i].strip()
        if not stripped:
            # 中间允许一个空行，但不允许连续空行
            if i - 1 >= 0 and lines[i - 1].strip().startswith(("//", "/*")):
                i -= 1
                steps += 1
                continue
            break
        if stripped.endswith("*/"):
            block: list[str] = []
            j = i
            while j >= 0 and steps < max_lines:
                block.append(lines[j])
                steps += 1
                if "/*" in lines[j]:
                    break
                j -= 1
            text = "\n".join(reversed(block))
            text = _BLOCK_START_RE.sub("", text, count=1)
            text = text.rsplit("*/", 1)[0]
            cleaned = " ".join(
                ln.strip().lstrip("*").strip() for ln in text.splitlines()
            ).strip()
            if cleaned:
                collected.append(cleaned)
            i = j - 1
            continue
        m = _COMMENT_LINE_RE.match(lines[i])
        if m:
            collected.append(m.group(1).strip())
            i -= 1
            steps += 1
            continue
        break

    return " ".join(reversed([c for c in collected if c])).strip()


def split_top_level(text: str, sep: str = ",") -> list[str]:
    """按分隔符切分，忽略 ``<>`` / ``()`` / ``[]`` / ``{}`` 内的分隔符。"""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch in "<([{":
            depth += 1
        elif ch in ">)]}":
            depth = max(0, depth - 1)
        if ch == sep and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current).strip())
    return [p for p in parts if p]


def strip_generics(text: str) -> str:
    out: list[str] = []
    depth = 0
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def normalize_type(raw: str) -> str:
    """把属性/参数类型收敛成可比较的类名。

    ``NSString *`` -> ``NSString``
    ``nullable TBOrderModel *`` -> ``TBOrderModel``
    ``[String: Any]?`` -> ``[String: Any]``
    ``some View`` -> ``View``
    """
    if not raw:
        return ""
    text = raw.strip()
    text = re.sub(r"@\w+(\([^)]*\))?", " ", text)
    text = re.sub(
        r"\b(nullable|nonnull|_Nullable|_Nonnull|__kindof|IBOutlet|IBInspectable|"
        r"weak|strong|copy|assign|atomic|nonatomic|readonly|readwrite|"
        r"public|private|fileprivate|internal|open|final|override|static|class|"
        r"lazy|lateinit|inout|mutating|nonmutating)\b",
        " ",
        text,
    )
    text = text.replace("*", " ").replace("!", "").replace("?", "")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(?:some|any)\s+", "", text)
    # 去掉元组参数标签之外的多余装饰
    text = text.strip(" ,;")
    return text


_URL_SCHEME_RE = re.compile(r"^(?:[a-zA-Z][\w+.-]*:|/)")


def clean_url(raw: str) -> str:
    """清洗 URL 字面量：只取第一行，拒绝空白/残缺片段。"""
    text = (raw or "").strip()
    if not text:
        return ""
    text = text.splitlines()[0].strip()
    if any(ch.isspace() for ch in text):
        return ""
    if not _URL_SCHEME_RE.match(text):
        return ""
    # ``https:`` / ``http:`` 这种被截断的多行字符串
    if text.endswith(":") and "//" not in text:
        return ""
    return text[:300]


def find_type_names(text: str) -> list[str]:
    """从类型文本里提取类名候选（处理泛型、元组、字典）。"""
    names: list[str] = []
    for token in re.findall(r"\b[A-Z][A-Za-z0-9_]*\b", text or ""):
        if token in {
            "Any", "AnyObject", "Void", "Bool", "Int", "Int8", "Int16", "Int32",
            "Int64", "UInt", "Float", "Double", "String", "Character", "Data",
            "Self", "Never", "Optional", "Array", "Dictionary", "Set", "Result",
            "Error", "URL", "Date", "UUID", "Decimal", "NSNull",
        }:
            continue
        names.append(token)
    return names


def parse_doc_params(doc: str) -> dict[str, str]:
    """从文档注释里抽 ``- parameter name: ...``（Swift）与 ``@param name``。"""
    result: dict[str, str] = {}
    for m in re.finditer(r"(?:-\s*parameter\s+(\w+)\s*:|@param\s+(\w+)\s+)([^\n]+)", doc or ""):
        name = m.group(1) or m.group(2)
        result[name] = m.group(3).strip()
    return result
