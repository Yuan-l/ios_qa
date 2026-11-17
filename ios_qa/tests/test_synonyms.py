#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""索引期同义词标签与查询期扩展的单元测试。"""

from __future__ import annotations

import unittest

from ios_qa.synonyms import expand_query, match_tags

GROUPS = [
    ["行情", "报价", "quote", "股票"],
    ["性能", "卡顿", "掉帧", "jank"],
    ["崩溃", "crash"],
]


class MatchTagsTest(unittest.TestCase):
    def test_returns_terms_missing_from_text(self) -> None:
        self.assertEqual(
            match_tags("行情页面加载", GROUPS), ["报价", "quote", "股票"]
        )

    def test_matches_english_class_name_case_insensitively(self) -> None:
        self.assertEqual(match_tags("TBCrashReporter", GROUPS), ["崩溃"])

    def test_dedupes_groups_and_respects_max_groups(self) -> None:
        text = "TBCrashReporter 行情"
        self.assertEqual(
            match_tags(text, GROUPS), ["报价", "quote", "股票", "崩溃"]
        )
        self.assertEqual(match_tags(text, GROUPS, max_groups=1), ["报价", "quote", "股票"])

    def test_respects_max_terms(self) -> None:
        self.assertEqual(
            match_tags("行情", GROUPS, max_terms=2), ["报价", "quote"]
        )

    def test_empty_inputs(self) -> None:
        self.assertEqual(match_tags("", GROUPS), [])
        self.assertEqual(match_tags("行情", []), [])


class ExpandQueryTest(unittest.TestCase):
    def test_expands_alias_to_group_terms(self) -> None:
        expansions = expand_query("行情页面怎么加载", GROUPS, limit=2)
        pairs = [(item["from"], item["to"]) for item in expansions]
        self.assertEqual(pairs, [("行情", "报价"), ("行情", "quote")])
        self.assertEqual(expansions[0]["query"], "报价页面怎么加载")

    def test_no_match_returns_empty(self) -> None:
        self.assertEqual(expand_query("完全无关的问题", GROUPS), [])


if __name__ == "__main__":
    unittest.main()
