#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 适配层测试：任何 MCP 客户端看到的工具集合与返回结构。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from ios_qa.build import build
from ios_qa.config import ProjectProfile
from ios_qa import mcp_server

SWIFT = """\
import UIKit

final class TBHomeViewController: UIViewController {
    override func viewDidLoad() {
        super.viewDidLoad()
        // 首页数据加载
        TBWidget.shared.reload()
    }
}
"""

OBJC = """\
#import <Foundation/Foundation.h>
@interface TBWidget : NSObject
@end
@implementation TBWidget
+ (instancetype)shared { return nil; }
- (void)reload { }
@end
"""

EXPECTED_TOOLS = {
    "kb_info", "find_feature", "search_symbol", "trace_class", "search_methods",
    "find_callers", "impact_analysis", "read_source", "list_pages",
    "list_module", "search_qa", "save_qa", "find_navigation", "find_notifications",
}


class McpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        (root / "App").mkdir()
        (root / "App" / "TBHomeViewController.swift").write_text(SWIFT, encoding="utf-8")
        (root / "App" / "TBWidget.m").write_text(OBJC, encoding="utf-8")
        profile = ProjectProfile(name="Demo", root=root, source_dirs=["."])
        db_path = root / "kb.db"
        build(profile, db_path, full=True, verbose=False)
        mcp_server.configure(profile, db_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_tool_set(self) -> None:
        async def run() -> set[str]:
            tools = await mcp_server.mcp.list_tools()
            return {tool.name for tool in tools}

        self.assertEqual(asyncio.run(run()), EXPECTED_TOOLS)

    def test_call_find_feature(self) -> None:
        async def run() -> str:
            result = await mcp_server.mcp.call_tool(
                "find_feature", {"query": "首页 数据", "limit": 3}
            )
            return str(result)

        self.assertIn("TBHomeViewController", asyncio.run(run()))

    def test_read_source_returns_lines(self) -> None:
        async def run() -> str:
            result = await mcp_server.mcp.call_tool(
                "read_source", {"file_path": "App/TBWidget.m", "focus": "reload"}
            )
            return str(result)

        output = asyncio.run(run())
        self.assertIn("reload", output)
        self.assertIn("content", output)

    def test_call_find_navigation(self) -> None:
        async def run() -> str:
            result = await mcp_server.mcp.call_tool(
                "find_navigation", {"target": "TBHomeViewController"}
            )
            return str(result)

        output = asyncio.run(run())
        self.assertIn("reachable", output)

    def test_call_find_notifications(self) -> None:
        async def run() -> str:
            result = await mcp_server.mcp.call_tool("find_notifications", {})
            return str(result)

        output = asyncio.run(run())
        self.assertIn("summary", output)


if __name__ == "__main__":
    unittest.main()
