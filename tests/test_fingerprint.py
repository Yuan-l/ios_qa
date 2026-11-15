#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""代码指纹测试：归一化规则、短方法过滤、重复聚类与 CLI 输出。"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from ios_qa import cli
from ios_qa.build import build
from ios_qa.config import ProjectProfile
from ios_qa.fingerprint import (
    BAND_SIZE,
    BANDS,
    MIN_TOKENS,
    band_keys,
    code_tokens,
    fingerprint,
    jaccard,
)
from ios_qa.query import QueryService

BODY = """\
- (void)setupLayout {
    self.backgroundColor = [UIColor whiteColor];
    self.layer.cornerRadius = 8.0;
    self.layer.masksToBounds = YES;
    self.label = [[UILabel alloc] initWithFrame:CGRectZero];
    self.label.textColor = [UIColor darkGrayColor];
    self.label.font = [UIFont systemFontOfSize:14.0];
    [self addSubview:self.label];
}
"""

RENAMED = """\
- (void)setupLayout {
    // 标题样式和上面那个类一样，只是变量名不同
    self.backgroundColor = [UIColor whiteColor];
    self.layer.cornerRadius = 8.0;
    self.layer.masksToBounds = YES;
    self.titleLabel = [[UILabel alloc] initWithFrame:CGRectZero];
    self.label.textColor = [UIColor darkGrayColor];
    self.label.font = [UIFont systemFontOfSize:14.0];
    [self addSubview:self.label];
}
"""

OTHER = """\
- (BOOL)application:(UIApplication *)app
        openURL:(NSURL *)url
        options:(NSDictionary *)options {
    if (![url.scheme isEqualToString:@"tb"]) { return NO; }
    NSDictionary *params = [self parseQuery:url.query];
    [[TBJumpRouter shared] open:params[@"page"] query:params];
    return YES;
}
"""


class CodeTokensTest(unittest.TestCase):
    def test_identifiers_split_on_camel_and_underscore(self) -> None:
        tokens = code_tokens("- (void)applyBeauty_filter:(TBImage *)img;", "objc")
        self.assertIn("beauty", tokens)
        self.assertIn("filter", tokens)
        self.assertIn("tb", tokens)
        self.assertIn("image", tokens)
        self.assertIn("img", tokens)

    def test_literals_and_comments_are_normalized(self) -> None:
        left = code_tokens('- (void)run { NSString *s = @"首页"; int n = 42; }', "objc")
        right = code_tokens(
            '- (void)run { /* 中文注释 */ NSString *t = @"home"; int m = 7; }', "objc"
        )
        self.assertEqual(left, right)

    def test_single_letter_segments_dropped(self) -> None:
        # 单字母循环变量改名不应改变 token 序列
        left = code_tokens("for (int i = 0; i < 3; i++) { [self step:i]; }", "objc")
        right = code_tokens("for (int k = 0; k < 3; k++) { [self step:k]; }", "objc")
        self.assertEqual(left, right)


class FingerprintTest(unittest.TestCase):
    def test_rename_local_keeps_fingerprint(self) -> None:
        base = fingerprint(BODY, "objc")
        renamed = fingerprint(RENAMED, "objc")
        self.assertIsNotNone(base)
        self.assertIsNotNone(renamed)
        self.assertGreaterEqual(jaccard(base.ids(), renamed.ids()), 0.8)
        self.assertNotEqual(base.norm_hash, renamed.norm_hash)

    def test_verbatim_copy_is_identical(self) -> None:
        left = fingerprint(BODY, "objc")
        right = fingerprint(BODY, "objc")
        self.assertEqual(left.norm_hash, right.norm_hash)
        self.assertEqual(left.signature, right.signature)
        self.assertEqual(jaccard(left.ids(), right.ids()), 1.0)

    def test_comments_and_literals_do_not_matter(self) -> None:
        noisy = BODY.replace(
            "self.layer.cornerRadius = 8.0;",
            "// 圆角\n    self.layer.cornerRadius = 12.0;",
        )
        self.assertEqual(
            fingerprint(BODY, "objc").norm_hash, fingerprint(noisy, "objc").norm_hash
        )

    def test_objc_string_literal_is_str(self) -> None:
        # @"..." 是字面量，不是标识符：文案改动不能让指纹脱钩
        left = code_tokens('- (void)run { [self show:@"首页"]; }', "objc")
        right = code_tokens('- (void)run { [self show:@"home"]; }', "objc")
        self.assertEqual(left, right)
        self.assertIn("str", left)

    def test_changed_callee_changes_fingerprint(self) -> None:
        other = BODY.replace("[self addSubview:self.label]", "[self insertSubview:self.label]")
        self.assertNotEqual(
            fingerprint(BODY, "objc").norm_hash, fingerprint(other, "objc").norm_hash
        )

    def test_short_method_returns_none(self) -> None:
        self.assertIsNone(fingerprint("- (void)dealloc { }", "objc"))
        self.assertIsNone(fingerprint("", "objc"))
        tokens = code_tokens(BODY, "objc")
        self.assertGreater(len(tokens), MIN_TOKENS)

    def test_unrelated_code_is_not_similar(self) -> None:
        left = fingerprint(BODY, "objc")
        right = fingerprint(OTHER, "objc")
        self.assertLess(jaccard(left.ids(), right.ids()), 0.3)

    def test_band_keys_cover_signature(self) -> None:
        fp = fingerprint(BODY, "objc")
        keys = band_keys(fp.signature)
        self.assertEqual(len(keys), BANDS)
        self.assertEqual([index for index, _ in keys], list(range(BANDS)))
        self.assertTrue(all(len(values) == BAND_SIZE for _, values in keys))


HEADER = """\
#import <UIKit/UIKit.h>

@interface {name} : UIView
@property (nonatomic, strong) UILabel *{label};
@end
"""

IMPL = """\
#import "{name}.h"

@implementation {name}
{body}
@end
"""


def _write_view(root: Path, name: str, label: str, body: str) -> None:
    (root / "App" / f"{name}.h").write_text(HEADER.format(name=name, label=label), "utf-8")
    (root / "App" / f"{name}.m").write_text(
        IMPL.format(name=name, body=body), "utf-8"
    )


class DuplicateDetectionTest(unittest.TestCase):
    """构建级：精确复制 + 改名近似复制要能聚类，相似查询要能对上。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "App").mkdir()
        _write_view(self.root, "TBAlphaView", "label", BODY)
        _write_view(self.root, "TBBetaView", "label", BODY)
        _write_view(self.root, "TBGammaView", "titleLabel", RENAMED)
        self.db_path = self.root / "kb.db"
        self.profile = ProjectProfile(name="Demo", root=self.root, source_dirs=["."])
        self.report = build(self.profile, self.db_path, full=True, verbose=False)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_fingerprints_written(self) -> None:
        self.assertEqual(self.report.fingerprints, 3)
        self.assertEqual(self.report.counts["code_fingerprints"], 3)

    def test_exact_cluster(self) -> None:
        with QueryService(self.profile, self.db_path) as service:
            data = service.find_duplicates(min_lines=6, min_similarity=0.95)
        self.assertEqual(data["summary"]["exact"], 1)
        cluster = next(c for c in data["clusters"] if c["kind"] == "exact")
        members = {f"{m['class_name']}.{m['method_name']}" for m in cluster["members"]}
        self.assertEqual(
            members, {"TBAlphaView.setupLayout", "TBBetaView.setupLayout"}
        )
        self.assertEqual(cluster["similarity"], 1.0)

    def test_near_cluster_includes_renamed_copy(self) -> None:
        with QueryService(self.profile, self.db_path) as service:
            data = service.find_duplicates(min_lines=6, min_similarity=0.8)
        self.assertEqual(data["summary"]["clusters"], 1)
        cluster = data["clusters"][0]
        self.assertEqual(cluster["size"], 3)
        self.assertEqual(cluster["kind"], "near")
        self.assertTrue(cluster["cross_module"] is False)

    def test_similar_code(self) -> None:
        with QueryService(self.profile, self.db_path) as service:
            data = service.find_similar_code("TBAlphaView.setupLayout", limit=5)
            missing = service.find_similar_code("NoSuchClass.noSuchMethod")
        self.assertTrue(data["found"])
        top = data["results"][0]
        self.assertEqual(top["class_name"], "TBBetaView")
        self.assertTrue(top["exact"])
        self.assertEqual(top["similarity"], 1.0)
        self.assertFalse(missing["found"])
        self.assertIn("hint", missing)

    def test_incremental_fingerprints_only_recompute_changed(self) -> None:
        second = build(self.profile, self.db_path, verbose=False)
        self.assertEqual(second.fingerprints, 0)

        (self.root / "App" / "TBBetaView.m").write_text(
            IMPL.format(name="TBBetaView", body=OTHER), "utf-8"
        )
        third = build(self.profile, self.db_path, verbose=False)
        self.assertEqual(third.fingerprints, 1)
        with QueryService(self.profile, self.db_path) as service:
            data = service.find_duplicates(min_lines=6, min_similarity=0.8)
        members = {
            f"{m['class_name']}.{m['method_name']}"
            for cluster in data["clusters"]
            for m in cluster["members"]
        }
        self.assertNotIn("TBBetaView.setupLayout", members)

    def test_cli_similar_and_duplicates(self) -> None:
        base = ["--root", str(self.root), "--db", str(self.db_path)]
        with contextlib.redirect_stdout(io.StringIO()) as buffer:
            code = cli.main(["similar", "TBAlphaView.setupLayout", *base])
        self.assertEqual(code, 0)
        self.assertIn("精确复制", buffer.getvalue())
        self.assertIn("TBBetaView.setupLayout", buffer.getvalue())

        with contextlib.redirect_stdout(io.StringIO()) as buffer:
            code = cli.main(["duplicates", "--min-lines", "6", *base, "--json"])
        self.assertEqual(code, 0)
        data = json.loads(buffer.getvalue())
        self.assertEqual(data["summary"]["clusters"], 1)
        self.assertEqual(data["clusters"][0]["size"], 3)

        with contextlib.redirect_stdout(io.StringIO()) as buffer:
            code = cli.main(["similar", "NoSuchClass.nope", *base])
        self.assertEqual(code, 0)
        self.assertIn("没有找到", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
