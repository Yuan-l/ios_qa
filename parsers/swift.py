#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Swift 解析器。

覆盖：
- class / struct / enum / protocol / extension / actor（含嵌套类型）
- 属性、方法、init/deinit、方法体
- 继承与协议关系、property/singleton/instantiate 依赖
- 类型调用 / 单例调用 / self 调用
- 网络调用与 URL 字面量
- NotificationCenter 注册/移除
- push / present / rootViewController 导航
"""

from __future__ import annotations

import re

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
    find_matching_brace,
    leading_comment,
    mask_code,
    mask_comments,
    normalize_type,
    split_top_level,
    ui_strings,
)

TYPE_RE = re.compile(
    r"(?m)^[ \t]*(?P<attrs>(?:@\w+(?:\([^)]*\))?\s+)*)"
    r"(?P<mods>(?:(?:public|private|fileprivate|internal|open|package|final|indirect|"
    r"nonisolated|distributed)\s+)*)"
    r"(?P<kind>class|struct|enum|protocol|extension|actor)\s+"
    r"(?!(?:func|var|let|subscript|init|deinit|case)\b)"
    r"(?P<name>[A-Za-z_]\w*)"
    r"(?P<generic>\s*<[^>{}\n]*>)?"
    r"(?P<inherit>[^{}]{0,400}?)\s*\{"
)

FUNC_RE = re.compile(
    r"(?m)^[ \t]*(?P<attrs>(?:@\w+(?:\([^)]*\))?\s+)*)"
    r"(?P<mods>(?:(?:public|private|fileprivate|internal|open|package|final|static|"
    r"class|override|mutating|nonmutating|convenience|required|nonisolated|"
    r"@objc|@MainActor|@discardableResult)\s+)*)"
    r"(?P<kind>func|init[?!]?|deinit)\b"
    r"(?P<rest>[^\n{]*)"
)

PROP_RE = re.compile(
    r"(?m)^[ \t]*(?P<attrs>(?:@\w+(?:\([^)]*\))?[ \t]+)*)"
    r"(?P<mods>(?:(?:public|private|fileprivate|internal|open|package|final|static|"
    r"class|override|weak|unowned|lazy|nonisolated)[ \t]+)*)"
    r"(?:var|let)[ \t]+(?P<name>[A-Za-z_]\w*)"
    r"(?:[ \t]*:[ \t]*(?P<type>[^\n{=]+))?"
    r"(?:[ \t]*=[ \t]*(?P<init>[A-Z]\w*)[ \t]*\()?"
)

MESSAGE_CALL_RE = re.compile(r"\b([A-Z]\w*)\.([A-Za-z_]\w*)\s*\(")
INIT_CALL_RE = re.compile(r"\b([A-Z]\w*)\s*\(")
SELF_CALL_RE = re.compile(r"\bself\.([A-Za-z_]\w*)\s*\(")
SUPER_CALL_RE = re.compile(r"\bsuper\.([A-Za-z_]\w*)\s*\(")
SHARED_CALL_RE = re.compile(r"\b([A-Z]\w*)\.shared(?:Instance)?\.([A-Za-z_]\w*)\s*\(")

MANAGER_METHOD_RE = re.compile(
    r"\b([A-Z]\w*)\s*\.\s*(?:shared(?:Instance)?\s*\.\s*)?([A-Za-z_]\w*)\s*\("
)
REQUEST_LITERAL_RE = re.compile(
    r"\brequest\s*\(\s*method\s*:\s*\"([A-Za-z]+)\"\s*,\s*urlString\s*:\s*\"([^\"]+)\""
)
URL_LITERAL_RE = re.compile(r'URL\s*\(\s*string\s*:\s*"([^"]+)"')
ADD_OBSERVER_SELECTOR_RE = re.compile(
    r"NotificationCenter\.default\.addObserver\s*\(\s*self\s*,\s*selector:\s*"
    r"#selector\s*\((.*?)\)\s*,\s*name:\s*([^,]+?)\s*,\s*object:"
)
ADD_OBSERVER_BLOCK_RE = re.compile(
    r"NotificationCenter\.default\.addObserver\s*\(\s*forName:\s*([^,]+?)\s*,\s*object:"
)
REMOVE_OBSERVER_RE = re.compile(
    r"NotificationCenter\.default\.removeObserver\s*\(\s*self\s*(?:,\s*name:\s*([^,)]+))?"
)
PUSH_CALL_RE = re.compile(r"\bpushViewController\s*\(")
PRESENT_CALL_RE = re.compile(r"\bpresent\s*\(")
ROOT_RE = re.compile(r"rootViewController\s*:\s*([A-Za-z_]\w*)")
ROOT_ASSIGN_RE = re.compile(r"rootViewController\s*=\s*([^;\n]+)")
STRING_URL_RE = re.compile(r'"(https?://[^"\s]+)"')
CONTAINER_TYPES = {
    "UINavigationController", "UITabBarController", "UISplitViewController",
    "UIPageViewController",
}
_CONTAINER_SUFFIXES = (
    "NavigationController", "TabBarController", "TabbarController",
    "SplitViewController",
)


def _is_container(name: str) -> bool:
    return name in CONTAINER_TYPES or name.endswith(_CONTAINER_SUFFIXES)


CONTAINER_APPEND_RE = re.compile(r"\bviewControllers\s*\??\s*\.\s*append\s*\(")
CONTAINER_ASSIGN_RE = re.compile(r"\bviewControllers\s*=\s*\[")
CONTAINER_SET_RE = re.compile(r"\bsetViewControllers\s*\(")

DECL_KEYWORDS = {
    "func", "var", "let", "subscript", "init", "deinit", "case", "return",
    "if", "guard", "switch", "for", "while", "defer", "do", "else", "throw",
    "throws", "try", "await", "as", "is", "in", "where",
}

_API_METHOD_PREFIXES = (
    "request", "get", "post", "put", "delete", "patch", "fetch", "load",
    "upload", "send", "query", "call", "report", "track",
)


def _clean_name(raw: str) -> str:
    name = raw.strip()
    if "." in name:
        name = name.rsplit(".", 1)[-1]
    return name.strip("`")


def _parse_inheritance(raw: str, kind: str) -> tuple[str, list[str]]:
    text = re.sub(r"\bwhere\b.*$", "", raw or "").strip()
    if text.startswith(":"):
        text = text[1:].strip()
    if not text:
        return "", []
    parts = [_clean_name(p) for p in split_top_level(text) if p.strip()]
    parts = [p for p in parts if p and not p.startswith("@")]
    if kind == "class" and parts:
        return parts[0], parts[1:]
    return "", parts


def _owner_type(offset: int, ranges: list[dict]) -> dict | None:
    best: dict | None = None
    for item in ranges:
        if item["start"] <= offset <= item["end"]:
            if best is None or (item["end"] - item["start"]) < (best["end"] - best["start"]):
                best = item
    return best


def _find_swift_body_open(masked: str, start: int, max_lines: int = 20) -> int:
    """只在签名所在逻辑行附近找 ``{``，避免把下一个函数的体误认成当前函数。"""
    i = start
    line_no = 0
    while i < len(masked) and line_no < max_lines:
        line_end = masked.find("\n", i)
        if line_end < 0:
            line_end = len(masked)
        line = masked[i:line_end]
        if line_no > 0:
            stripped = line.strip()
            if stripped.startswith(("func ", "init", "deinit", "var ", "let ",
                                    "case ", "class ", "struct ", "enum ",
                                    "protocol ", "extension ", "@", "}")):
                return -1
        brace = line.find("{")
        if brace >= 0:
            return i + brace
        i = line_end + 1
        line_no += 1
    return -1


def _method_at_line(ranges: list[tuple[int, int, str]], line: int) -> str:
    for start, end, name in ranges:
        if start <= line <= end:
            return name
    return ""


def _in_ranges(offset: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= offset <= end for start, end in ranges)


def _enclosed(
    text: str, index: int, open_ch: str = "(", close_ch: str = ")", max_len: int = 4000
) -> str:
    """返回 ``index`` 处括号内的文本（不含外层括号）；不配对时尽量取到上限。"""
    if index < 0 or index >= len(text) or text[index] != open_ch:
        return ""
    depth = 0
    end = min(len(text), index + max_len)
    for i in range(index, end):
        ch = text[i]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[index + 1:i]
    return text[index + 1:end]


def _property_target(raw: str) -> str:
    """从属性类型里取页面/协议类名：``id<FooDelegate>`` / ``Foo``。"""
    text = (raw or "").strip()
    if not text or "->" in text:
        return ""
    match = re.search(r"<\s*([A-Z]\w*)\s*>", text)
    if match:
        return match.group(1)
    match = re.fullmatch(r"([A-Z]\w*)", text)
    return match.group(1) if match else ""



def parse_swift(text: str, file: SourceFile, profile: ProjectProfile) -> ParseResult:
    result = ParseResult()
    masked = mask_code(text, "swift")
    comments = mask_comments(text, "swift")
    line_map = LineMap(text)
    lines = text.splitlines()
    rel = file.rel_path

    # ---------- 1. 类型声明 ----------
    type_ranges: list[dict] = []
    for match in TYPE_RE.finditer(masked):
        kind = match.group("kind")
        name = match.group("name")
        if name in DECL_KEYWORDS:
            continue
        open_brace = masked.find("{", match.start(), match.end() + 1)
        if open_brace < 0:
            continue
        close_brace = find_matching_brace(masked, open_brace)
        if close_brace < 0:
            continue
        line_start = line_map.line_of(match.start())
        line_end = line_map.line_of(close_brace)
        inherit_raw = match.group("inherit").strip()
        superclass, protocols = _parse_inheritance(inherit_raw, kind)
        extension_of = name if kind == "extension" else ""
        symbol_key = make_symbol_key(rel, line_start, kind, name)
        symbol = SymbolRec(
            symbol_key=symbol_key,
            class_name=name,
            kind=kind,
            language="swift",
            module=file.module,
            file_path=rel,
            line_start=line_start,
            line_end=line_end,
            superclass=superclass,
            protocols=protocols,
            doc_comment=leading_comment(lines, line_start - 1),
            interface_text=text[match.start():close_brace + 1][:8000],
            extension_of=extension_of,
            literals=ui_strings(comments[open_brace:close_brace], "swift"),
        )
        result.symbols.append(symbol)
        type_ranges.append(
            {
                "start": match.start(),
                "end": close_brace,
                "symbol": symbol,
                "open": open_brace,
                "name": name,
                "kind": kind,
            }
        )
        if superclass:
            result.deps.append(
                DepRec(name, superclass, "superclass", file.module, rel, line_start)
            )
        for proto in protocols:
            result.deps.append(
                DepRec(name, proto, "protocol", file.module, rel, line_start)
            )

    # ---------- 2. 方法 ----------
    method_ranges: list[tuple[int, int, str]] = []
    method_intervals: list[tuple[int, int]] = []
    for match in FUNC_RE.finditer(masked):
        kind_token = match.group("kind")
        if kind_token.startswith("init"):
            method_name = "init"
        elif kind_token == "deinit":
            method_name = "deinit"
        else:
            rest = match.group("rest").strip()
            name_match = re.match(r"([A-Za-z_]\w*|==|!=|<=|>=|&&|\|\||[+\-*/%<>]=?)", rest)
            method_name = name_match.group(1) if name_match else ""
        if not method_name:
            continue
        owner = _owner_type(match.start(), type_ranges)
        if owner is None:
            continue
        # 跳过类型声明自身的名字误命中（class Foo vs class func）
        open_brace = _find_swift_body_open(masked, match.start())
        close_brace = find_matching_brace(masked, open_brace) if open_brace >= 0 else -1
        line_start = line_map.line_of(match.start())
        if close_brace >= 0:
            line_end = line_map.line_of(close_brace)
            body = text[open_brace + 1: close_brace]
            has_body = True
        else:
            # 协议要求 / 抽象声明
            if owner["kind"] != "protocol":
                continue
            line_end = line_start
            body = ""
            has_body = False
        if has_body:
            inner_lines = max(0, line_end - line_start - 1)
            if inner_lines < profile.min_method_body_lines:
                continue
        symbol: SymbolRec = owner["symbol"]
        method = MethodRec(
            symbol_key=symbol.symbol_key,
            class_name=owner["name"],
            method_name=method_name,
            signature=masked[match.start():match.end()].strip(),
            language="swift",
            module=file.module,
            file_path=rel,
            line_start=line_start,
            line_end=line_end,
            is_class_method=("static" in match.group("mods") or "class" in match.group("mods")),
            is_extension=(owner["kind"] == "extension"),
            has_body=has_body,
            doc_comment=leading_comment(lines, line_start - 1),
            body=body[: profile.max_body_chars],
        )
        result.methods.append(method)
        if has_body:
            method_ranges.append((line_start, line_end, method_name))
            method_intervals.append((match.start(), close_brace))

    # ---------- 3. 属性 ----------
    for match in PROP_RE.finditer(masked):
        if _in_ranges(match.start(), method_intervals):
            continue
        owner = _owner_type(match.start(), type_ranges)
        if owner is None:
            continue
        prop_type = normalize_type(match.group("type") or match.group("init") or "")
        if not prop_type:
            continue
        prop = {"name": match.group("name"), "type": prop_type}
        symbol: SymbolRec = owner["symbol"]
        if prop not in symbol.properties:
            symbol.properties.append(prop)
        if profile.is_own_class(prop_type):
            result.deps.append(
                DepRec(
                    symbol.class_name,
                    prop_type,
                    "property",
                    file.module,
                    rel,
                    line_map.line_of(match.start()),
                )
            )

    # ---------- 4. 依赖 / 调用 / API / 通知 / 导航 ----------
    # 属性可能声明在主类、调用发生在同文件的 extension 里，按类名合并后再解析调用
    props_by_class: dict[str, list[dict]] = {}
    for owner in type_ranges:
        bucket = props_by_class.setdefault(owner["symbol"].class_name, [])
        for prop in owner["symbol"].properties:
            if prop not in bucket:
                bucket.append(prop)

    for owner in type_ranges:
        symbol = owner["symbol"]
        body_masked = masked[owner["open"] + 1: owner["end"]]
        body_comments = comments[owner["open"] + 1: owner["end"]]
        body_original = text[owner["open"] + 1: owner["end"]]
        owner_offset = owner["open"] + 1

        result.deps.extend(
            _parse_deps(body_masked, symbol.class_name, file, profile, owner_offset, line_map)
        )
        result.calls.extend(
            _parse_calls(
                body_masked,
                symbol.class_name,
                file,
                method_ranges,
                line_map,
                owner_offset,
                props_by_class.get(symbol.class_name, []),
            )
        )
        result.api_calls.extend(
            _parse_api_calls(
                body_comments,
                body_original,
                symbol.class_name,
                file,
                profile,
                method_ranges,
                line_map,
                owner_offset,
            )
        )
        result.notifications.extend(
            _parse_notifications(
                body_comments,
                symbol.class_name,
                file,
                method_ranges,
                line_map,
                owner_offset,
            )
        )
        result.navigation.extend(
            _parse_navigation(
                body_comments,
                symbol.class_name,
                file,
                line_map,
                owner_offset,
            )
        )

    # ---------- 5. 入口 ----------
    if re.search(r"(?m)^[ \t]*@main\b", masked):
        result.entrypoints.append(
            EntrypointRec(
                kind="app",
                class_name="",
                identifier="@main",
                source="swift",
                module=file.module,
                file_path=rel,
                line_number=line_map.line_of(masked.find("@main")),
                confidence="high",
            )
        )
    for owner in type_ranges:
        symbol = owner["symbol"]
        if "UIApplicationDelegate" in symbol.protocols:
            result.entrypoints.append(
                EntrypointRec(
                    kind="scene" if "UISceneDelegate" in symbol.protocols else "app",
                    class_name=symbol.class_name,
                    identifier="UIApplicationDelegate",
                    source="swift",
                    module=file.module,
                    file_path=rel,
                    line_number=symbol.line_start,
                    confidence="high",
                )
            )
    return result


def _parse_deps(
    body_masked: str,
    class_name: str,
    file: SourceFile,
    profile: ProjectProfile,
    offset: int,
    line_map: LineMap,
) -> list[DepRec]:
    deps: list[DepRec] = []
    seen: set[tuple[str, str]] = set()

    def add(dep: str, kind: str, position: int) -> None:
        if not dep or dep == class_name or not profile.is_own_class(dep):
            return
        key = (dep, kind)
        if key in seen:
            return
        seen.add(key)
        deps.append(
            DepRec(class_name, dep, kind, file.module, file.rel_path,
                   line_map.line_of(offset + position))
        )

    for match in SHARED_CALL_RE.finditer(body_masked):
        add(match.group(1), "singleton", match.start())
    for match in INIT_CALL_RE.finditer(body_masked):
        add(match.group(1), "instantiate", match.start())
    for match in re.finditer(r":\s*([A-Z]\w+)[?!!]?", body_masked):
        add(match.group(1), "type_ref", match.start())
    return deps


def _parse_calls(
    body_masked: str,
    class_name: str,
    file: SourceFile,
    method_ranges: list[tuple[int, int, str]],
    line_map: LineMap,
    offset: int,
    properties: list[dict] | None = None,
) -> list[CallRec]:
    calls: list[CallRec] = []
    seen: set[tuple[str, str, int]] = set()

    def add(callee: str, name: str, kind: str, confidence: str, position: int) -> None:
        line = line_map.line_of(offset + position)
        caller = _method_at_line(method_ranges, line)
        if not caller or not name:
            return
        key = (callee, name, line)
        if key in seen:
            return
        seen.add(key)
        calls.append(
            CallRec(
                caller_class=class_name,
                caller_method=caller,
                callee_class=callee,
                callee_name=name,
                kind=kind,
                confidence=confidence,
                module=file.module,
                file_path=file.rel_path,
                line_number=line,
            )
        )

    for match in SHARED_CALL_RE.finditer(body_masked):
        add(match.group(1), match.group(2), "singleton", "high", match.start())
    for match in MESSAGE_CALL_RE.finditer(body_masked):
        target, name = match.group(1), match.group(2)
        if target in {"URL", "String", "Int", "Double", "Data", "Date", "IndexPath",
                      "NotificationCenter", "DispatchQueue", "CGRect", "CGSize"}:
            continue
        add(target, name, "static", "high", match.start())
    for match in SELF_CALL_RE.finditer(body_masked):
        add(class_name, match.group(1), "self", "medium", match.start())
    for match in SUPER_CALL_RE.finditer(body_masked):
        add("", match.group(1), "super", "low", match.start())
    for match in INIT_CALL_RE.finditer(body_masked):
        target = match.group(1)
        if target in {"URL", "String", "Int", "Double", "Data", "Date", "IndexPath",
                      "NotificationCenter", "DispatchQueue", "CGRect", "CGSize"}:
            continue
        add(target, "init", "init", "high", match.start())

    # 协议/委托调用：``self.delegate?.foo()`` -> 协议名
    delegate_props: dict[str, str] = {}
    block_props: list[str] = []
    for prop in properties or []:
        name = (prop.get("name") or "").strip()
        prop_type = prop.get("type") or ""
        if not name:
            continue
        if "->" in prop_type or prop_type == "block":
            block_props.append(name)
        elif "delegate" in name.lower():
            target = _property_target(prop_type)
            if target:
                delegate_props[name] = target

    for name, target in delegate_props.items():
        pattern = re.compile(
            rf"\b(?:self\s*\.\s*)?{re.escape(name)}\s*\??\s*\.\s*"
            r"([A-Za-z_]\w*)\s*\("
        )
        for match in pattern.finditer(body_masked):
            add(target, match.group(1), "delegate", "low", match.start())

    # block 属性调用：``self.onSelect(index)``
    for name in block_props:
        pattern = re.compile(rf"\b(?:self\s*\.\s*)?{re.escape(name)}\s*\??\s*\(")
        for match in pattern.finditer(body_masked):
            add(class_name, name, "block", "low", match.start())
    return calls


def _parse_api_calls(
    body_comments: str,
    body_original: str,
    class_name: str,
    file: SourceFile,
    profile: ProjectProfile,
    method_ranges: list[tuple[int, int, str]],
    line_map: LineMap,
    offset: int,
) -> list[ApiCallRec]:
    calls: list[ApiCallRec] = []
    seen: set[tuple[str, str, int]] = set()
    managers = set(profile.http_managers)

    def add(kind: str, url: str, method: str, position: int, ctx: str) -> None:
        if kind in {"http", "url"}:
            url = clean_url(url)
        line = line_map.line_of(offset + position)
        key = (kind, url, line)
        if not url or key in seen:
            return
        seen.add(key)
        calls.append(
            ApiCallRec(
                class_name=class_name,
                method_name=_method_at_line(method_ranges, line),
                api_kind=kind,
                url_pattern=url[:300],
                http_method=method,
                response_type="",
                context_snippet=ctx.strip()[:240],
                module=file.module,
                file_path=file.rel_path,
                line_number=line,
            )
        )

    for match in REQUEST_LITERAL_RE.finditer(body_comments):
        add("http", match.group(2), match.group(1).upper(), match.start(),
            _line_text(line_map, offset + match.start()))

    for match in MANAGER_METHOD_RE.finditer(body_comments):
        manager, method = match.group(1), match.group(2)
        if manager not in managers and not profile.is_api_manager(manager):
            continue
        if method in {"init", "shared", "sharedInstance"}:
            continue
        if not method.lower().startswith(_API_METHOD_PREFIXES):
            continue
        add("manager", f"{manager}.{method}", "",
            match.start(), _line_text(line_map, offset + match.start()))

    for match in URL_LITERAL_RE.finditer(body_comments):
        add("url", match.group(1), "", match.start(),
            _line_text(line_map, offset + match.start()))

    for match in STRING_URL_RE.finditer(body_comments):
        add("url", match.group(1), "", match.start(),
            _line_text(line_map, offset + match.start()))
    return calls


def _line_text(line_map: LineMap, offset: int) -> str:
    line = line_map.line_of(offset)
    return line_map.line_text(line).strip()


def _parse_notifications(
    body_comments: str,
    class_name: str,
    file: SourceFile,
    method_ranges: list[tuple[int, int, str]],
    line_map: LineMap,
    offset: int,
) -> list[NotificationRec]:
    wildcard_remove = False
    removed_names: set[str] = set()
    for match in REMOVE_OBSERVER_RE.finditer(body_comments):
        name = match.group(1)
        if name:
            removed_names.add(_clean_notification_name(name))
        else:
            wildcard_remove = True

    adds: list[dict] = []
    for match in ADD_OBSERVER_SELECTOR_RE.finditer(body_comments):
        adds.append(
            {
                "selector": match.group(1),
                "name": _clean_notification_name(match.group(2)),
                "pos": match.start(),
                "kind": "selector",
            }
        )
    for match in ADD_OBSERVER_BLOCK_RE.finditer(body_comments):
        adds.append(
            {
                "selector": "(block)",
                "name": _clean_notification_name(match.group(1)),
                "pos": match.start(),
                "kind": "block",
            }
        )

    out: list[NotificationRec] = []
    for add in adds:
        name = add["name"]
        if not name:
            continue
        if add["kind"] == "block":
            balanced = -1
        elif wildcard_remove or name in removed_names:
            balanced = 1
        else:
            balanced = 0
        line = line_map.line_of(offset + add["pos"])
        out.append(
            NotificationRec(
                class_name=class_name,
                notification_name=name[:200],
                observer_selector=add["selector"],
                register_method=_method_at_line(method_ranges, line),
                register_line=line,
                remove_method="",
                remove_line=0,
                file_path=file.rel_path,
                module=file.module,
                language="swift",
                is_balanced=balanced,
            )
        )
    return out


def _clean_notification_name(raw: str) -> str:
    name = raw.strip()
    name = name.strip("\"'")
    if name.startswith("NSNotification.Name."):
        name = name[len("NSNotification.Name."):]
    if name.startswith("."):
        name = name[1:]
    return name.strip()


def _parse_navigation(
    body_comments: str,
    class_name: str,
    file: SourceFile,
    line_map: LineMap,
    offset: int,
) -> list[NavigationRec]:
    navs: list[NavigationRec] = []
    var_map: dict[str, str] = {}

    def resolve_page(expr: str) -> str:
        """把表达式收敛成页面类名，优先取局部变量，其次取构造器里的类。"""
        expr = (expr or "").strip()
        if not expr:
            return ""
        for ident in re.findall(r"[A-Za-z_]\w*", expr):
            if ident in var_map:
                return var_map[ident]
        for match in re.finditer(r"\b([A-Z]\w*)\s*(?:<[^>]*>)?\s*\(", expr):
            name = match.group(1)
            if not _is_container(name):
                return name
        return ""

    # ``let vc = Foo()`` / ``let nav = UINavigationController(rootViewController: vc)``
    for match in re.finditer(
        r"\b(?:let|var)\s+(\w+)\s*=\s*([A-Z]\w*)\s*(?:<[^>]*>)?\s*\(",
        body_comments,
    ):
        var, cls = match.group(1), match.group(2)
        open_paren = body_comments.find("(", match.start(2))
        args = _enclosed(body_comments, open_paren) if open_paren >= 0 else ""
        if _is_container(cls):
            var_map[var] = resolve_page(args) or cls
        else:
            var_map[var] = cls

    def add(target: str, kind: str, position: int) -> None:
        if not target or _is_container(target):
            return
        navs.append(
            NavigationRec(
                from_class=class_name,
                to_class=target,
                kind=kind,
                identifier="",
                module=file.module,
                file_path=file.rel_path,
                line_number=line_map.line_of(offset + position),
            )
        )

    for match in PUSH_CALL_RE.finditer(body_comments):
        add(resolve_page(_enclosed(body_comments, match.end() - 1)), "push", match.start())
    for match in PRESENT_CALL_RE.finditer(body_comments):
        add(resolve_page(_enclosed(body_comments, match.end() - 1)), "present", match.start())
    for match in ROOT_RE.finditer(body_comments):
        add(resolve_page(match.group(1)), "root", match.start())
    for match in ROOT_ASSIGN_RE.finditer(body_comments):
        add(resolve_page(match.group(1)), "root", match.start())

    # tab 容器：``viewControllers?.append(Self.navigationController(with: vc))``
    for match in CONTAINER_APPEND_RE.finditer(body_comments):
        args = _enclosed(body_comments, match.end() - 1)
        add(resolve_page(args), "tab", match.start())

    # tab 容器：``viewControllers = [cameraNav, weatherNav]``
    for match in CONTAINER_ASSIGN_RE.finditer(body_comments):
        bracket = body_comments.find("[", match.start(), match.end())
        items = _enclosed(body_comments, bracket, "[", "]") if bracket >= 0 else ""
        for item in split_top_level(items):
            add(resolve_page(item), "tab", match.start())

    # ``setViewControllers([vc1, vc2], animated:)``
    for match in CONTAINER_SET_RE.finditer(body_comments):
        args = _enclosed(body_comments, match.end() - 1)
        bracket = args.find("[")
        if bracket < 0:
            continue
        for item in split_top_level(_enclosed(args, bracket, "[", "]")):
            add(resolve_page(item), "tab", match.start())
    return navs
