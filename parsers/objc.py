#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ObjC 解析器（.h / .m / .mm）。

纯词法扫描：结构判断跑在 masked 文本上，方法体从原文截取。
覆盖：
- @interface / @implementation / category / class extension / @protocol
- @property（含 block、泛型、nullable）
- 方法声明与方法体（花括号配对）
- import / property / singleton / instantiate 依赖
- HTTP / Manager 调用、URL 字面量
- NSNotification 注册/移除配对（含 RAC、block API）
- push/present/root/tab 导航边
"""

from __future__ import annotations

import re
from pathlib import Path

from ..config import ProjectProfile
from ..discover import SourceFile
from ..model import (
    ApiCallRec,
    CallRec,
    DepRec,
    EntrypointRec,
    MethodRec,
    NavigationRec,
    NotificationRec,
    ParseResult,
    SymbolRec,
    make_symbol_key,
)
from ..source import (
    LineMap,
    clean_url,
    find_body_open,
    find_matching_brace,
    leading_comment,
    mask_code,
    mask_comments,
    normalize_type,
)
from ..textnorm import build_search_text  # noqa: F401  (由 classes 聚合使用)

INTERFACE_RE = re.compile(
    r"@interface\s+(\w+)"
    r"(?:\s*<[^>\n]*>)?"
    r"\s*(?:\(\s*(\w*)\s*\))?"
    r"\s*(?::\s*([\w<>]+))?"
    r"\s*(?:<([^>\n]+)>)?"
)
IMPLEMENTATION_RE = re.compile(
    r"@implementation\s+(\w+)"
    r"\s*(?:\(\s*(\w*)\s*\))?" r"(?:\s*<[^>\n]*>)?"
)
PROTOCOL_RE = re.compile(r"@protocol\s+(\w+)\s*(?:<([^>\n]+)>)?")
PROPERTY_RE = re.compile(r"@property\s*\(([^)]*)\)\s*([^;]+);", re.DOTALL)
HEADER_METHOD_RE = re.compile(
    r"^[ \t]*([-+])\s*\(([^)\n]*)\)\s*([^;{]*);", re.MULTILINE
)
METHOD_START_RE = re.compile(r"^[ \t]*([-+])\s*\(", re.MULTILINE)
METHOD_SIG_RE = re.compile(r"([-+])\s*\(([^)]*)\)\s*([\s\S]*)$")
IMPORT_RE = re.compile(r'#\s*import\s*[<"]([^>"]+)[>"]')
MODULE_IMPORT_RE = re.compile(r"@import\s+(\w+)\s*;")
SINGLETON_RE = re.compile(
    r"\[(\w+)\s+(?:sharedInstance|shared|defaultManager|standardUserDefaults)\]"
    r"|(\w+)\.(?:sharedInstance|shared)\b"
)
INSTANTIATE_RE = re.compile(
    r"\[\[?(\w+)\s+(?:alloc|new)\]|\[\s*(\w+)\s+new\]"
)
OBJC_MESSAGE_RE = re.compile(r"\[\s*(self|super|[A-Z]\w*)\s+([A-Za-z_]\w*)")
NESTED_CALL_RE = re.compile(
    r"\[\s*\[\s*([A-Z]\w*)\s+(?:sharedInstance|shared|new|alloc)\s*\]\s*"
    r"([A-Za-z_]\w*)"
)
VAR_CLASS_RE = re.compile(
    r"\b(\w+)\s*\*\s*(\w+)\s*=\s*(?:\[\[?\s*(\w+)\s+(?:alloc|new)\]|\[\s*(\w+)\s+new\])"
)
HTTP_CALL_RE = re.compile(
    r"\[\[?\s*(\w+)\s+(?:sharedInstance\]\s*|shared\]\s*)?"
    r"(POST|GET|PUT|DELETE|PATCH|get|post|put|delete|patch|requestWithMethod)\s*:"
    r"\s*(?:@\"([^\"]*)\"|(\w+))"
)
MANAGER_CALL_RE = re.compile(
    r"\[\[?\s*(\w+)\s+(?:sharedInstance\]\s*|shared\]\s*)?([A-Za-z_]\w*)"
)
URL_LITERAL_RE = re.compile(r'@?"(https?://[^"\s]+)"')
URL_WITH_STRING_RE = re.compile(r'URLWithString\s*:\s*@?"([^"]+)"')
_HTTP_VERBS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
_SKIP_MANAGER_METHODS = {
    "alloc", "new", "init", "shared", "sharedInstance", "sharedManager",
    "defaultManager", "standardUserDefaults", "class", "self", "super",
}
RESPONSE_TYPE_RE = re.compile(r"\^\s*\(\s*(\w+)\s*\*?")
ADD_OBSERVER_RE = re.compile(
    r"\[\[NSNotificationCenter\s+defaultCenter\]\s+addObserver\s*:\s*\w+"
    r"\s+selector\s*:\s*@selector\s*\(\s*([\w:]+)\s*\)"
    r"\s+name\s*:\s*([^\s\]]+)"
    r"\s+object\s*:[^\]]+\]"
)
ADD_OBSERVER_BLOCK_RE = re.compile(
    r"\[\[NSNotificationCenter\s+defaultCenter\]\s+addObserverForName\s*:\s*([^\s\]]+)"
)
REMOVE_OBSERVER_RE = re.compile(
    r"\[\[NSNotificationCenter\s+defaultCenter\]\s+removeObserver\s*:\s*(\w+)"
    r"(?:\s+name\s*:\s*([^\s\]]+)\s+object\s*:[^\]]+)?\s*\]"
)
RAC_OBSERVER_RE = re.compile(
    r"rac_addObserverForName\s*:\s*([^\s\]]+)"
)
PUSH_RE = re.compile(r"pushViewController\s*:\s*([^\s\]]+)")
PRESENT_RE = re.compile(r"presentViewController\s*:\s*([^\s\]]+)")
ROOT_VC_RE = re.compile(r"rootViewController\s*=\s*([^;]+);")
INIT_WITH_ROOT_RE = re.compile(r"initWithRootViewController\s*:\s*([^\s\]]+)")
SELECTOR_PART_RE = re.compile(r"(\w+)\s*$")


def _clean_literal(raw: str) -> str:
    return raw.strip().strip("@\"").strip('"').strip()


def _selector_from_signature(signature: str) -> str:
    """从方法签名提取 selector：``setTitle:forState:``。"""
    text = re.sub(r"\s+", " ", signature or "").strip()
    text = re.sub(r"\b(NS_SWIFT_NAME|NS_DESIGNATED_INITIALIZER|API_AVAILABLE|"
                  r"NS_ASSUME_NONNULL_BEGIN|NS_ASSUME_NONNULL_END|"
                  r"DEPRECATED_ATTRIBUTE|UI_APPEARANCE_SELECTOR)\b(\([^)]*\))?", "", text)
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in text:
        if ch in "<([{":
            depth += 1
        elif ch in ">)]}":
            depth = max(0, depth - 1)
        if ch == ":" and depth == 0:
            m = SELECTOR_PART_RE.search("".join(current))
            if m:
                parts.append(m.group(1))
            current = []
        else:
            current.append(ch)
    if parts:
        return "".join(p + ":" for p in parts)
    m = re.search(r"[A-Za-z_]\w*", text)
    return m.group(0) if m else ""


def _property_from_decl(decl: str) -> dict | None:
    text = re.sub(r"\s+", " ", decl or "").strip()
    if not text:
        return None
    block_match = re.search(r"\(\s*\^\s*(\w+)\s*\)", text)
    if block_match:
        name = block_match.group(1)
        dep_type = "block"
    else:
        name_match = re.search(r"([A-Za-z_]\w*)\s*$", text)
        if not name_match:
            return None
        name = name_match.group(1)
        dep_type = normalize_type(text[: name_match.start()])
        if not dep_type:
            dep_type = normalize_type(text)
    if not name:
        return None
    return {"name": name, "type": dep_type}


def _method_ranges(methods: list[MethodRec]) -> list[tuple[int, int, str]]:
    return [
        (m.line_start, m.line_end, m.method_name)
        for m in methods
        if m.line_start and m.line_end
    ]


def _method_at_line(ranges: list[tuple[int, int, str]], line: int) -> str:
    for start, end, name in ranges:
        if start <= line <= end:
            return name
    return ""


def _rel(file: SourceFile) -> str:
    return file.rel_path


def parse_header(
    text: str, file: SourceFile, profile: ProjectProfile
) -> ParseResult:
    result = ParseResult()
    masked = mask_code(text, "objc")
    line_map = LineMap(text)
    lines = text.splitlines()
    rel = _rel(file)

    # 先记录 import 依赖（后续归属到每个 @interface 也可，这里按文件级别）
    imports = _parse_imports(masked, file, profile)

    for match in INTERFACE_RE.finditer(masked):
        class_name = match.group(1)
        category = match.group(2)
        superclass = (match.group(3) or "").strip()
        protocols_raw = (match.group(4) or "").strip()
        body_start = match.end()
        end = masked.find("@end", body_start)
        if end < 0:
            continue
        body_masked = masked[body_start:end]
        body_original = text[body_start:end]
        line_start = line_map.line_of(match.start())
        line_end = line_map.line_of(end)
        doc = leading_comment(lines, line_start - 1)

        if category is None and not superclass:
            # @interface Foo () —— class extension，方法/属性并入主类
            kind = "extension"
            extension_of = class_name
            category_name = ""
        elif category is not None:
            kind = "category"
            extension_of = class_name
            category_name = category
        else:
            kind = "class"
            extension_of = ""
            category_name = ""

        protocols = [
            p.strip()
            for p in re.split(r"[,]", protocols_raw)
            if p.strip()
        ]
        properties = _parse_properties(body_masked)
        methods = _parse_header_methods(
            body_masked, text, file, class_name, profile, line_map, lines, kind, body_start
        )
        symbol_key = make_symbol_key(rel, line_start, kind, class_name)
        for method in methods:
            method.symbol_key = symbol_key

        symbol = SymbolRec(
            symbol_key=symbol_key,
            class_name=class_name,
            kind=kind,
            language="objc",
            module=file.module,
            file_path=rel,
            line_start=line_start,
            line_end=line_end,
            superclass=superclass,
            protocols=protocols,
            properties=properties,
            doc_comment=doc,
            interface_text=body_original.strip()[:8000],
            extension_of=extension_of,
            category_name=category_name,
        )
        result.symbols.append(symbol)
        result.methods.extend(methods)

        if kind == "class":
            if superclass:
                result.deps.append(
                    DepRec(class_name, superclass, "superclass", file.module, rel, line_start)
                )
            for proto in protocols:
                result.deps.append(
                    DepRec(class_name, proto, "protocol", file.module, rel, line_start)
                )
        for prop in properties:
            dep_type = prop.get("type", "")
            if profile.is_own_class(dep_type):
                result.deps.append(
                    DepRec(class_name, dep_type, "property", file.module, rel, line_start)
                )

    # @protocol
    for match in PROTOCOL_RE.finditer(masked):
        name = match.group(1)
        start = match.end()
        # 前向声明 @protocol Foo; 没有 @end
        if masked[start:].lstrip().startswith(";"):
            continue
        end = masked.find("@end", start)
        if end < 0:
            continue
        line_start = line_map.line_of(match.start())
        line_end = line_map.line_of(end)
        protocols = [
            p.strip() for p in re.split(r"[,]", match.group(2) or "") if p.strip()
        ]
        result.symbols.append(
            SymbolRec(
                symbol_key=make_symbol_key(rel, line_start, "protocol", name),
                class_name=name,
                kind="protocol",
                language="objc",
                module=file.module,
                file_path=rel,
                line_start=line_start,
                line_end=line_end,
                protocols=protocols,
                doc_comment=leading_comment(lines, line_start - 1),
            )
        )

    result.deps.extend(imports)
    return result


def _parse_imports(
    masked: str, file: SourceFile, profile: ProjectProfile
) -> list[DepRec]:
    deps: list[DepRec] = []
    line_map = LineMap(masked)
    seen: set[str] = set()
    for match in IMPORT_RE.finditer(masked):
        header = match.group(1)
        stem = Path(header).stem
        # TBTableView+Extension -> TBTableView
        dep = stem.split("+", 1)[0]
        if not dep or dep in seen:
            continue
        seen.add(dep)
        deps.append(
            DepRec(
                from_class="<file>",
                dep_class=dep,
                dep_kind="import",
                module=file.module,
                file_path=file.rel_path,
                line_number=line_map.line_of(match.start()),
            )
        )
    return deps


def _parse_properties(body_masked: str) -> list[dict]:
    props: list[dict] = []
    seen: set[str] = set()
    for match in PROPERTY_RE.finditer(body_masked):
        prop = _property_from_decl(match.group(2))
        if not prop or prop["name"] in seen:
            continue
        seen.add(prop["name"])
        props.append(prop)
    return props


def _parse_header_methods(
    body_masked: str,
    text: str,
    file: SourceFile,
    class_name: str,
    profile: ProjectProfile,
    line_map: LineMap,
    lines: list[str],
    kind: str,
    offset: int,
) -> list[MethodRec]:
    methods: list[MethodRec] = []
    for match in HEADER_METHOD_RE.finditer(body_masked):
        prefix = match.group(1)
        signature = re.sub(r"\s+", " ", match.group(3)).strip()
        name = _selector_from_signature(signature)
        if not name:
            continue
        line = line_map.line_of(offset + match.start())
        line_end = line_map.line_of(offset + match.end())
        methods.append(
            MethodRec(
                symbol_key="",
                class_name=class_name,
                method_name=name,
                signature=f"{prefix} ({match.group(2).strip()}) {signature}".strip(),
                language="objc",
                module=file.module,
                file_path=file.rel_path,
                line_start=line,
                line_end=line_end,
                is_class_method=(prefix == "+"),
                is_extension=(kind in {"category", "extension"}),
                has_body=False,
                doc_comment=leading_comment(lines, line - 1),
            )
        )
    return methods


def parse_implementation(
    text: str, file: SourceFile, profile: ProjectProfile
) -> ParseResult:
    result = ParseResult()
    masked = mask_code(text, "objc")
    comments = mask_comments(text, "objc")
    line_map = LineMap(text)
    lines = text.splitlines()
    rel = _rel(file)

    result.deps.extend(_parse_imports(masked, file, profile))

    for match in IMPLEMENTATION_RE.finditer(masked):
        class_name = match.group(1)
        category = match.group(2)
        body_start = match.end()
        end = masked.find("@end", body_start)
        if end < 0:
            continue
        impl_start = match.start()
        line_start = line_map.line_of(impl_start)
        line_end = line_map.line_of(end)
        is_category = category is not None
        kind = "category_impl" if is_category else "implementation"
        symbol_key = make_symbol_key(rel, line_start, kind, class_name)

        methods = _parse_impl_methods(
            masked, text, body_start, end, file, class_name, profile,
            line_map, lines, is_category,
        )
        for method in methods:
            method.symbol_key = symbol_key

        result.symbols.append(
            SymbolRec(
                symbol_key=symbol_key,
                class_name=class_name,
                kind=kind,
                language="objc",
                module=file.module,
                file_path=rel,
                line_start=line_start,
                line_end=line_end,
                extension_of=class_name if is_category else "",
                category_name=category or "",
                doc_comment=leading_comment(lines, line_start - 1),
            )
        )
        result.methods.extend(methods)

        ranges = _method_ranges(methods)
        body_masked = masked[impl_start:end]
        body_original = text[impl_start:end]
        body_comments = comments[impl_start:end]

        # 类扩展里的 @property
        properties = _parse_properties(body_masked)
        for prop in properties:
            dep_type = prop.get("type", "")
            if profile.is_own_class(dep_type):
                result.deps.append(
                    DepRec(class_name, dep_type, "property", file.module, rel, line_start)
                )
            # 私有属性也补进 implementation symbol，便于 trace
        result.symbols[-1].properties = properties

        result.deps.extend(
            _parse_impl_deps(body_masked, class_name, file, profile, line_map, impl_start)
        )
        result.calls.extend(
            _parse_calls(body_masked, class_name, file, ranges, line_map, impl_start)
        )
        result.api_calls.extend(
            _parse_api_calls(
                body_comments, body_original, class_name, file, profile, ranges,
                line_map, impl_start,
            )
        )
        result.notifications.extend(
            _parse_notifications(
                body_comments, body_original, class_name, file, ranges,
                line_map, impl_start,
            )
        )
        result.navigation.extend(
            _parse_navigation(
                body_comments, body_original, class_name, file, line_map, impl_start
            )
        )

    # 文件级入口（AppDelegate 等）
    if re.search(r"\bUIApplicationMain\s*\(", masked):
        result.entrypoints.append(
            EntrypointRec(
                kind="app",
                class_name="AppDelegate",
                identifier="UIApplicationMain",
                source="objc",
                module=file.module,
                file_path=rel,
                line_number=1,
                confidence="high",
            )
        )
    return result


def _parse_impl_methods(
    masked: str,
    text: str,
    body_start: int,
    body_end: int,
    file: SourceFile,
    class_name: str,
    profile: ProjectProfile,
    line_map: LineMap,
    lines: list[str],
    is_category: bool,
) -> list[MethodRec]:
    methods: list[MethodRec] = []
    region = masked[body_start:body_end]
    for match in METHOD_START_RE.finditer(region):
        start = body_start + match.start()
        open_brace = find_body_open(masked, start, limit=8000)
        if open_brace < 0:
            continue
        close_brace = find_matching_brace(masked, open_brace)
        if close_brace < 0:
            continue
        sig_match = METHOD_SIG_RE.match(masked[start:open_brace].strip())
        if not sig_match:
            continue
        prefix = sig_match.group(1)
        signature = re.sub(r"\s+", " ", sig_match.group(3)).strip()
        name = _selector_from_signature(signature)
        if not name:
            continue
        line_start = line_map.line_of(start)
        line_end = line_map.line_of(close_brace)
        inner_lines = max(0, line_end - line_start - 1)
        if inner_lines < profile.min_method_body_lines:
            continue
        body = text[open_brace + 1 : close_brace]
        methods.append(
            MethodRec(
                symbol_key="",
                class_name=class_name,
                method_name=name,
                signature=f"{prefix} ({sig_match.group(2).strip()}) {signature}".strip(),
                language="objc",
                module=file.module,
                file_path=file.rel_path,
                line_start=line_start,
                line_end=line_end,
                is_class_method=(prefix == "+"),
                is_extension=is_category,
                has_body=True,
                doc_comment=leading_comment(lines, line_start - 1),
                body=body[: profile.max_body_chars],
            )
        )
    return methods


def _parse_impl_deps(
    body_masked: str,
    class_name: str,
    file: SourceFile,
    profile: ProjectProfile,
    line_map: LineMap,
    offset: int,
) -> list[DepRec]:
    deps: list[DepRec] = []
    seen: set[tuple[str, str]] = set()

    def add(dep: str, kind: str, line: int) -> None:
        key = (dep, kind)
        if dep and dep != class_name and key not in seen and profile.is_own_class(dep):
            seen.add(key)
            deps.append(DepRec(class_name, dep, kind, file.module, file.rel_path, line))

    for match in SINGLETON_RE.finditer(body_masked):
        dep = match.group(1) or match.group(2)
        add(dep, "singleton", line_map.line_of(offset + match.start()))
    for match in INSTANTIATE_RE.finditer(body_masked):
        dep = match.group(1) or match.group(2)
        add(dep, "instantiate", line_map.line_of(offset + match.start()))
    for match in PROPERTY_RE.finditer(body_masked):
        prop = _property_from_decl(match.group(2))
        if prop:
            add(prop.get("type", ""), "property",
                line_map.line_of(offset + match.start()))
    return deps


def _parse_calls(
    body_masked: str,
    class_name: str,
    file: SourceFile,
    ranges: list[tuple[int, int, str]],
    line_map: LineMap,
    offset: int,
) -> list[CallRec]:
    calls: list[CallRec] = []
    seen: set[tuple[str, str, int]] = set()
    for match in OBJC_MESSAGE_RE.finditer(body_masked):
        target = match.group(1)
        method = match.group(2)
        line = line_map.line_of(offset + match.start())
        caller = _method_at_line(ranges, line)
        if target == "self":
            callee_class = class_name
            kind = "self"
            confidence = "medium"
        elif target == "super":
            callee_class = ""
            kind = "super"
            confidence = "low"
        else:
            callee_class = target
            kind = "message"
            confidence = "high" if target[:1].isupper() else "low"
        if not caller:
            continue
        key = (callee_class or target, method, line)
        if key in seen:
            continue
        seen.add(key)
        calls.append(
            CallRec(
                caller_class=class_name,
                caller_method=caller,
                callee_class=callee_class,
                callee_name=method,
                kind=kind,
                confidence=confidence,
                module=file.module,
                file_path=file.rel_path,
                line_number=line,
            )
        )

    # 嵌套消息：``[[TBRequestManager shared] get:...]`` 的外层方法名
    for match in NESTED_CALL_RE.finditer(body_masked):
        target, method = match.group(1), match.group(2)
        line = line_map.line_of(offset + match.start())
        caller = _method_at_line(ranges, line)
        if not caller:
            continue
        key = (target, method, line)
        if key in seen:
            continue
        seen.add(key)
        calls.append(
            CallRec(
                caller_class=class_name,
                caller_method=caller,
                callee_class=target,
                callee_name=method,
                kind="message",
                confidence="high",
                module=file.module,
                file_path=file.rel_path,
                line_number=line,
            )
        )
    return calls


def _parse_api_calls(
    body_comments: str,
    body_original: str,
    class_name: str,
    file: SourceFile,
    profile: ProjectProfile,
    ranges: list[tuple[int, int, str]],
    line_map: LineMap,
    offset: int,
) -> list[ApiCallRec]:
    calls: list[ApiCallRec] = []
    seen: set[tuple[str, str, int]] = set()
    managers = set(profile.http_managers) | set(
        name for name in re.findall(r"\b[A-Z]\w+\b", body_comments)
        if profile.is_api_manager(name)
    )

    def add(kind: str, url: str, method: str, response: str, line: int, ctx: str) -> None:
        if kind in {"http", "url"}:
            url = clean_url(url)
        key = (kind, url, line)
        if not url or key in seen:
            return
        seen.add(key)
        calls.append(
            ApiCallRec(
                class_name=class_name,
                method_name=_method_at_line(ranges, line),
                api_kind=kind,
                url_pattern=url[:300],
                http_method=method,
                response_type=response,
                context_snippet=ctx.strip()[:240],
                module=file.module,
                file_path=file.rel_path,
                line_number=line,
            )
        )

    for match in HTTP_CALL_RE.finditer(body_comments):
        manager = match.group(1)
        if manager not in managers and not profile.is_api_manager(manager):
            continue
        http_kind = match.group(2).upper()
        if http_kind not in _HTTP_VERBS:
            # requestWithMethod: 这类泛化入口交给 MANAGER_CALL_RE 记录
            continue
        line = line_map.line_of(offset + match.start())
        url = _clean_literal(match.group(3) or "") or (match.group(4) or "")
        suffix = body_comments[match.end() : match.end() + 600]
        resp = RESPONSE_TYPE_RE.search(suffix)
        add("http", clean_url(url), http_kind,
            resp.group(1) if resp else "", line, _line_snippet(line, line_map))

    for match in MANAGER_CALL_RE.finditer(body_comments):
        manager = match.group(1)
        method = match.group(2)
        if manager not in managers and not profile.is_api_manager(manager):
            continue
        if method in _SKIP_MANAGER_METHODS or method.startswith(("shared", "default", "standard")):
            continue
        line = line_map.line_of(offset + match.start())
        suffix = body_comments[match.end() : match.end() + 600]
        resp = RESPONSE_TYPE_RE.search(suffix)
        add("manager", f"{manager}.{method}", "", resp.group(1) if resp else "", line,
            _line_snippet(line, line_map))

    for match in URL_LITERAL_RE.finditer(body_comments):
        line = line_map.line_of(offset + match.start())
        add("url", clean_url(match.group(1)), "", "", line, _line_snippet(line, line_map))
    for match in URL_WITH_STRING_RE.finditer(body_comments):
        line = line_map.line_of(offset + match.start())
        add("url", clean_url(match.group(1)), "", "", line, _line_snippet(line, line_map))
    return calls


def _parse_notifications(
    body_comments: str,
    body_original: str,
    class_name: str,
    file: SourceFile,
    ranges: list[tuple[int, int, str]],
    line_map: LineMap,
    offset: int,
) -> list[NotificationRec]:
    adds: list[dict] = []
    wildcard_remove = False
    removed_names: set[str] = set()

    for match in REMOVE_OBSERVER_RE.finditer(body_comments):
        name = match.group(2)
        if name:
            removed_names.add(_clean_literal(name))
        else:
            wildcard_remove = True

    for match in ADD_OBSERVER_RE.finditer(body_comments):
        adds.append(
            {
                "selector": match.group(1),
                "name": _clean_literal(match.group(2)),
                "line": line_map.line_of(offset + match.start()),
                "kind": "selector",
            }
        )
    for match in ADD_OBSERVER_BLOCK_RE.finditer(body_comments):
        adds.append(
            {
                "selector": "(block)",
                "name": _clean_literal(match.group(1)),
                "line": line_map.line_of(offset + match.start()),
                "kind": "block",
            }
        )
    for match in RAC_OBSERVER_RE.finditer(body_comments):
        adds.append(
            {
                "selector": "(rac)",
                "name": _clean_literal(match.group(1)),
                "line": line_map.line_of(offset + match.start()),
                "kind": "rac",
            }
        )

    result: list[NotificationRec] = []
    for add in adds:
        name = add["name"]
        if not name:
            continue
        if add["kind"] == "rac":
            balanced = -1
        elif wildcard_remove or name in removed_names:
            balanced = 1
        elif add["kind"] == "block":
            balanced = -1
        else:
            balanced = 0
        result.append(
            NotificationRec(
                class_name=class_name,
                notification_name=name[:200],
                observer_selector=add["selector"],
                register_method=_method_at_line(ranges, add["line"]),
                register_line=add["line"],
                remove_method="",
                remove_line=0,
                file_path=file.rel_path,
                module=file.module,
                language="objc",
                is_balanced=balanced,
            )
        )
    return result


def _parse_navigation(
    body_comments: str,
    body_original: str,
    class_name: str,
    file: SourceFile,
    line_map: LineMap,
    offset: int,
) -> list[NavigationRec]:
    navs: list[NavigationRec] = []
    var_map: dict[str, str] = {}
    for match in VAR_CLASS_RE.finditer(body_comments):
        cls = match.group(3) or match.group(4)
        var_map[match.group(2)] = cls

    def resolve(arg: str) -> str:
        arg = arg.strip().strip("[]").strip()
        if arg in var_map:
            return var_map[arg]
        m = re.search(r"\b([A-Z]\w+)\s*(?:alloc|new|init)", arg)
        if m:
            return m.group(1)
        m = re.search(r"\b([A-Z]\w*ViewController)\b", arg)
        return m.group(1) if m else ""

    def add(target: str, kind: str, line: int, identifier: str = "") -> None:
        if not target:
            return
        navs.append(
            NavigationRec(
                from_class=class_name,
                to_class=target,
                kind=kind,
                identifier=identifier,
                module=file.module,
                file_path=file.rel_path,
                line_number=line,
            )
        )

    for match in PUSH_RE.finditer(body_comments):
        add(resolve(match.group(1)), "push", line_map.line_of(offset + match.start()))
    for match in PRESENT_RE.finditer(body_comments):
        add(resolve(match.group(1)), "present", line_map.line_of(offset + match.start()))
    for match in INIT_WITH_ROOT_RE.finditer(body_comments):
        add(resolve(match.group(1)), "root", line_map.line_of(offset + match.start()))
    for match in ROOT_VC_RE.finditer(body_comments):
        add(resolve(match.group(1)), "root", line_map.line_of(offset + match.start()))
    return navs


def _line_snippet(line: int, line_map: LineMap) -> str:
    return line_map.line_text(line).strip()


def parse_file(text: str, file: SourceFile, profile: ProjectProfile) -> ParseResult:
    if file.suffix == ".h":
        return parse_header(text, file, profile)
    return parse_implementation(text, file, profile)
