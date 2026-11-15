#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检索文本归一化测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ios_qa.config import ProjectProfile
from ios_qa.textnorm import build_search_text, fts_expr, like_pattern, snippet, tokenize
from ios_qa.synonyms import expand_query, load_groups


class TokenizeTest(unittest.TestCase):
    def test_chinese_single_and_bigram(self) -> None:
        tokens = tokenize("首页数据")
        for expected in ("首", "页", "数", "据", "首页", "页数", "数据"):
            self.assertIn(expected, tokens)

    def test_camel_case_split(self) -> None:
        tokens = tokenize("TDURLSessionNetworkClient")
        self.assertIn("td", tokens)
        self.assertIn("url", tokens)
        self.assertIn("session", tokens)
        self.assertIn("network", tokens)
        self.assertIn("client", tokens)

    def test_mixed_chinese_and_identifier(self) -> None:
        tokens = tokenize("TBRequestManager 网络请求")
        self.assertIn("tb", tokens)
        self.assertIn("request", tokens)
        self.assertIn("网络", tokens)
        self.assertIn("请求", tokens)

    def test_search_text_dedup_and_limit(self) -> None:
        text = build_search_text("首页 首页", "首页数据", limit=100)
        words = text.split()
        self.assertEqual(len(words), len(set(words)))
        self.assertLessEqual(sum(len(w) + 1 for w in words), 100)


class FtsExprTest(unittest.TestCase):
    def test_quotes_tokens(self) -> None:
        expr = fts_expr("首页 数据", "AND")
        self.assertIsNotNone(expr)
        assert expr is not None
        self.assertIn('"首页"', expr)
        self.assertIn('"数据"', expr)
        self.assertIn(" AND ", expr)

    def test_empty_query(self) -> None:
        self.assertIsNone(fts_expr("   "))

    def test_like_pattern_escapes(self) -> None:
        self.assertEqual(like_pattern("100%_a"), "%100\\%\\_a%")


class SnippetTest(unittest.TestCase):
    def test_returns_context(self) -> None:
        text = "前缀" * 40 + "目标数据" + "后缀" * 40
        result = snippet(text, "数据", width=20)
        self.assertIn("数据", result)
        self.assertLessEqual(len(result), 30)


class SynonymTest(unittest.TestCase):
    GROUPS = [
        ["行情", "报价", "quote"],
        ["崩溃", "crash"],
    ]

    def test_expands_to_other_terms(self) -> None:
        result = expand_query("报价 页面", self.GROUPS)
        targets = {item["to"] for item in result}
        self.assertIn("行情", targets)
        self.assertIn("quote", targets)
        self.assertTrue(all(item["query"] != "报价 页面" for item in result))

    def test_no_match_returns_empty(self) -> None:
        self.assertEqual(expand_query("摄像头", self.GROUPS), [])

    def test_limit_caps_expansions(self) -> None:
        result = expand_query("报价 崩溃", self.GROUPS, limit=2)
        self.assertEqual(len(result), 2)

    def test_default_groups_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / ".ios_qa"
            config.mkdir()
            (config / "synonyms.json").write_text(
                json.dumps({"groups": [["行情", "报价", "quote"]]}),
                encoding="utf-8",
            )
            profile = ProjectProfile.load(root=root)
            groups = load_groups(profile)
            self.assertTrue(any("行情" in group for group in groups))

    def test_missing_groups_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = ProjectProfile.load(root=Path(tmp))
            self.assertEqual(load_groups(profile), [])


if __name__ == "__main__":
    unittest.main()
