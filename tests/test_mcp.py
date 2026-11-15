#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 适配层测试：任何 MCP 客户端看到的工具集合与返回结构。"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from ios_qa.build import build
from ios_qa.config import ProjectProfile
from ios_qa.query import QueryService
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
    "find_similar_code", "find_duplicates",
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

    def test_call_trace_class_with_module(self) -> None:
        async def run() -> str:
            result = await mcp_server.mcp.call_tool(
                "trace_class",
                {"class_name": "TBHomeViewController", "module": "App", "limit": 10},
            )
            return str(result)

        output = asyncio.run(run())
        self.assertIn("selected_module", output)
        self.assertIn("ambiguous", output)

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

    def test_call_find_similar_code(self) -> None:
        async def run() -> str:
            result = await mcp_server.mcp.call_tool(
                "find_similar_code", {"target": "TBWidget.reload"}
            )
            return str(result)

        output = asyncio.run(run())
        self.assertIn("found", output)

    def test_call_find_duplicates(self) -> None:
        async def run() -> str:
            result = await mcp_server.mcp.call_tool(
                "find_duplicates", {"min_lines": 3, "min_similarity": 0.8}
            )
            return str(result)

        output = asyncio.run(run())
        self.assertIn("clusters", output)
        self.assertIn("summary", output)


class McpClientAcceptanceTest(unittest.TestCase):
    """P2-c：两个独立客户端走真实 stdio 子进程，数据与查询层一致。"""

    @classmethod
    def setUpClass(cls) -> None:
        from ios_qa import mcp_smoke

        cls.smoke = mcp_smoke
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        (cls.root / "App").mkdir()
        (cls.root / "App" / "TBHomeViewController.swift").write_text(SWIFT, encoding="utf-8")
        (cls.root / "App" / "TBWidget.m").write_text(OBJC, encoding="utf-8")
        cls.profile = ProjectProfile(name="Demo", root=cls.root, source_dirs=["."])
        cls.db_path = cls.root / "kb.db"
        build(cls.profile, cls.db_path, full=True, verbose=False)
        cls.cases = mcp_smoke.build_cases(cls.db_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_cases_cover_every_tool(self) -> None:
        names = [case["tool"] for case in self.cases]
        self.assertEqual(set(names), EXPECTED_TOOLS)
        # 写路径不参与数据比对，避免污染索引
        skipped = [case["tool"] for case in self.cases if not case["compare"]]
        self.assertEqual(skipped, ["save_qa"])

    def test_raw_jsonrpc_client(self) -> None:
        report = self.smoke.run_raw(self.profile, self.db_path, self.cases)
        self.assertTrue(report.ok, [c.to_dict() for c in report.checks if not c.ok])
        self.assertEqual(set(report.tools), EXPECTED_TOOLS)

        case = next(c for c in self.cases if c["tool"] == "find_feature")
        with QueryService(self.profile, self.db_path) as service:
            expected = self.smoke.canonical(
                service.find_feature(case["args"]["query"], limit=case["args"]["limit"])
            )
        actual = self.smoke.canonical(report.results["find_feature"])
        self.assertEqual(self.smoke._diff(expected, actual), "")

    def test_sdk_client_matches_raw_client(self) -> None:
        cases = [case for case in self.cases if case["tool"] in {"kb_info", "find_feature"}]
        raw = self.smoke.run_raw(self.profile, self.db_path, cases)
        sdk = self.smoke.run_sdk(self.profile, self.db_path, cases)
        self.assertTrue(raw.ok, [c.to_dict() for c in raw.checks if not c.ok])
        self.assertTrue(sdk.ok, [c.to_dict() for c in sdk.checks if not c.ok])
        self.assertEqual(set(raw.tools), set(sdk.tools))
        for tool in ("kb_info", "find_feature"):
            self.assertEqual(
                self.smoke._diff(
                    self.smoke.canonical(raw.results[tool]),
                    self.smoke.canonical(sdk.results[tool]),
                ),
                "",
                tool,
            )

    def test_write_tool_roundtrip_through_client(self) -> None:
        with self.smoke.RawStdioClient(
            self.smoke.server_command(self.profile, self.db_path),
            cwd=self.root,
            env=self.smoke.client_env(),
        ) as client:
            client.initialize()
            saved = client.call_tool(
                "save_qa", {"question": "首页怎么刷新？", "answer": "调用 reload"}
            )
            self.assertTrue(saved.get("ok"))
            found = client.call_tool("search_qa", {"query": "首页 刷新"})
            self.assertTrue(
                any("首页" in row["question"] for row in found["results"])
            )

    def test_cli_exit_codes(self) -> None:
        from ios_qa import cli

        with contextlib.redirect_stdout(io.StringIO()) as buffer:
            code = cli.main(
                [
                    "mcp-smoke", "--root", str(self.root), "--db", str(self.db_path),
                    "--client", "raw", "--only", "kb_info", "--json",
                ]
            )
        self.assertEqual(code, 0)
        report = json.loads(buffer.getvalue())
        self.assertTrue(report["ok"])

        with contextlib.redirect_stdout(io.StringIO()) as buffer:
            code = cli.main(
                [
                    "mcp-smoke", "--root", str(self.root),
                    "--db", str(self.root / "missing.db"),
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("索引不存在", buffer.getvalue())


class McpSmokeHelperTest(unittest.TestCase):
    """验收器自身也要可信：差异定位与报文解析不能静默放过问题。"""

    def test_diff_locates_first_mismatch(self) -> None:
        from ios_qa.mcp_smoke import _diff

        self.assertEqual(_diff({"a": 1}, {"a": 1}), "")
        self.assertIn("缺少字段", _diff({"a": 1}, {}))
        self.assertIn("多出字段", _diff({}, {"a": 1}))
        self.assertIn("长度", _diff([1, 2], [1]))
        self.assertIn("a.b", _diff({"a": {"b": 1}}, {"a": {"b": 2}}))
        self.assertIn("[0]", _diff([1], ["1"]))

    def test_extract_payload(self) -> None:
        from ios_qa.mcp_smoke import SmokeError, extract_payload

        payload = extract_payload({"content": [{"type": "text", "text": '{"ok": true}'}]})
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(
            extract_payload({"structuredContent": {"x": 1}}), {"x": 1}
        )
        with self.assertRaises(SmokeError):
            extract_payload({"content": [{"type": "text", "text": "not json"}]})


if __name__ == "__main__":
    unittest.main()
