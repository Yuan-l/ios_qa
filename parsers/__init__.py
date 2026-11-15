#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""解析器分发。"""

from __future__ import annotations

from ..config import ProjectProfile
from ..discover import SourceFile
from ..model import ParseResult
from . import objc, swift, ui


def read_text(file: SourceFile) -> str:
    try:
        return file.path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def parse_file(file: SourceFile, profile: ProjectProfile, text: str | None = None) -> ParseResult:
    if text is None:
        text = read_text(file)
    if not text:
        return ParseResult()
    if file.language == "objc":
        header_text = ""
        if file.suffix in {".m", ".mm"}:
            # 头文件里的 block / delegate 属性决定调用能否落到具体协议
            header = file.path.with_suffix(".h")
            if header.is_file():
                try:
                    header_text = header.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    header_text = ""
        return objc.parse_file(text, file, profile, header_text=header_text)
    if file.language == "swift":
        return swift.parse_swift(text, file, profile)
    if file.language == "ui":
        return ui.parse_ui(text, file, profile)
    return ParseResult()
