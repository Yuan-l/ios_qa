#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检索文本归一化测试。"""

from __future__ import annotations

import unittest

from ios_qa.textnorm import build_search_text, fts_expr, like_pattern, snippet, tokenize


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


if __name__ == "__main__":
    unittest.main()
