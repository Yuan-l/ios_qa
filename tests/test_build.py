#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建 / 增量 / 查询服务测试。"""

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
from ios_qa.eval import run_eval
from ios_qa.query import QueryService

HEADER = """\
#import <Foundation/Foundation.h>

@interface TBWidget : NSObject
- (void)reload;
@end
"""

IMPL = """\
#import "TBWidget.h"

@implementation TBWidget
- (void)reload {
    [[TBWidget shared] reload];
}
+ (instancetype)shared { return nil; }
@end
"""

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


class BuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "App").mkdir()
        (self.root / "App" / "TBWidget.h").write_text(HEADER, encoding="utf-8")
        (self.root / "App" / "TBWidget.m").write_text(IMPL, encoding="utf-8")
        (self.root / "App" / "TBHomeViewController.swift").write_text(
            SWIFT, encoding="utf-8"
        )
        self.db_path = self.root / "kb.db"
        self.profile = ProjectProfile(name="Demo", root=self.root, source_dirs=["."])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_full_then_incremental_is_idempotent(self) -> None:
        first = build(self.profile, self.db_path, full=True, verbose=False)
        self.assertEqual(first.files_total, 3)
        self.assertEqual(first.files_parsed, 3)
        self.assertEqual(first.errors, [])
        self.assertEqual(first.counts["classes"], 2)

        second = build(self.profile, self.db_path, verbose=False)
        self.assertEqual(second.files_parsed, 0)
        self.assertEqual(second.files_skipped, 3)
        self.assertEqual(second.counts["classes"], first.counts["classes"])
        self.assertEqual(second.counts["methods"], first.counts["methods"])

    def test_changed_and_deleted_files(self) -> None:
        build(self.profile, self.db_path, full=True, verbose=False)

        target = self.root / "App" / "TBHomeViewController.swift"
        target.write_text(SWIFT + "\n// changed\n", encoding="utf-8")
        (self.root / "App" / "TBWidget.h").unlink()

        report = build(self.profile, self.db_path, verbose=False)
        self.assertEqual(report.files_total, 2)
        self.assertEqual(report.files_parsed, 1)
        self.assertEqual(report.files_skipped, 1)
        self.assertEqual(report.files_deleted, 1)
        self.assertEqual(report.counts["files"], 2)

    def test_query_service_chinese_search(self) -> None:
        build(self.profile, self.db_path, full=True, verbose=False)
        with QueryService(self.profile, self.db_path) as service:
            found = service.find_feature("首页 数据", limit=5)
            self.assertTrue(found["results"])
            self.assertEqual(found["results"][0]["class_name"], "TBHomeViewController")

            methods = service.search_methods("首页 数据", limit=5)
            self.assertTrue(methods["results"])

            trace = service.trace_class("TBWidget")
            self.assertTrue(trace["found"])
            self.assertEqual(trace["superclass_chain"], ["NSObject"])

    def test_qa_roundtrip(self) -> None:
        build(self.profile, self.db_path, full=True, verbose=False)
        with QueryService(self.profile, self.db_path) as service:
            saved = service.save_qa(
                "首页怎么刷新？",
                "调用 TBWidget.shared.reload()",
                related_class="TBWidget",
                tags="首页,刷新",
            )
            self.assertTrue(saved["ok"])
            result = service.search_qa("首页 刷新")
            self.assertEqual(result["results"][0]["id"], saved["id"])


class RealRepoSmokeTest(unittest.TestCase):
    """对当前仓库构建一次，验证解析/行号/查询链路。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.profile = ProjectProfile.load()
        cls.db_path = Path(cls._tmp.name) / "real.db"
        cls.report = build(cls.profile, cls.db_path, full=True, verbose=False)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_scale_and_errors(self) -> None:
        self.assertEqual(self.report.errors, [])
        self.assertGreater(self.report.counts["files"], 300)
        self.assertGreater(self.report.counts["classes"], 250)
        self.assertGreater(self.report.counts["methods"], 1500)
        self.assertGreater(self.report.counts["calls"], 1000)

    def test_known_line_numbers(self) -> None:
        with QueryService(self.profile, self.db_path) as service:
            symbol = service.search_symbol("TBRequestManager", limit=1)
            item = symbol["results"][0]
            header_shared = next(
                m for m in item["methods"]
                if m["file_path"].endswith("TBRequestManager.h")
                and m["method_name"] == "shared"
            )
            self.assertEqual(header_shared["line_start"], 29)

            callers = service.find_callers("TBRequestManager")
            lines = {(c["file_path"], c["line"]) for c in callers["callers"]}
            self.assertIn(
                ("components/TBFinanceKit/TBFinanceKit/Classes/Network/TBFinanceHTTP.m", 74),
                lines,
            )

    def test_chinese_feature_search(self) -> None:
        with QueryService(self.profile, self.db_path) as service:
            result = service.find_feature("网络 请求", limit=10)
            names = [r["class_name"] for r in result["results"]]
            self.assertIn("TBRequestManager", names)

    def test_navigation_path(self) -> None:
        with QueryService(self.profile, self.db_path) as service:
            result = service.find_navigation("TBFinanceDetailViewController")
            self.assertTrue(result["found"])
            self.assertTrue(result["reachable"])
            self.assertIn("AppDelegate", result["roots"])
            hops = [
                (hop["from_class"], hop["to_class"], hop["kind"], hop["line_number"])
                for path in result["paths"]
                for hop in path["chain"]
            ]
            self.assertIn(
                ("AppDelegate", "TBFinanceHomeViewController", "instantiate", 48), hops
            )
            self.assertIn(
                (
                    "TBFinanceHomeViewController",
                    "TBFinanceDetailViewController",
                    "push",
                    793,
                ),
                hops,
            )

    def test_navigation_missing_class(self) -> None:
        with QueryService(self.profile, self.db_path) as service:
            result = service.find_navigation("TBNoSuchViewController")
            self.assertFalse(result["found"])
            self.assertFalse(result["reachable"])

    def test_notifications_unbalanced(self) -> None:
        with QueryService(self.profile, self.db_path) as service:
            result = service.find_notifications(balanced=0)
            self.assertGreaterEqual(result["summary"]["unbalanced"], 1)
            pairs = {
                (row["class_name"], row["notification_name"])
                for row in result["results"]
            }
            self.assertIn(("CameraViewController", "tbLanguageDidChange"), pairs)

    def test_synonym_expansion(self) -> None:
        with QueryService(self.profile, self.db_path) as service:
            result = service.find_feature("报价", limit=10)
            names = [item["class_name"] for item in result["results"]]
            self.assertIn("TBFinanceHomeViewController", names)
            pairs = {(item["from"], item["to"]) for item in result.get("synonyms", [])}
            self.assertIn(("报价", "行情"), pairs)

    def test_doctor_metrics(self) -> None:
        with QueryService(self.profile, self.db_path) as service:
            info = service.info()
            self.assertGreater(info["methods_with_body"], 0)
            self.assertEqual(info["warnings"], [])

    def test_golden_eval(self) -> None:
        report = run_eval(self.profile, self.db_path)
        self.assertEqual(report["failed"], 0, report)
        self.assertTrue(all(case["last_reviewed"] for case in report["cases"]))


class BuildCheckTest(unittest.TestCase):
    """`build --check`：构建后自动跑 eval，退出码语义要明确。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "App").mkdir()
        (self.root / "App" / "TBWidget.m").write_text(IMPL, encoding="utf-8")
        self.cases_path = self.root / "ios_qa" / "eval_cases.json"
        self.cases_path.parent.mkdir(parents=True)
        (self.cases_path.parent / "project.json").write_text(
            json.dumps({"name": "Demo", "source_dirs": ["."]}), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_cases(self, value: str) -> None:
        payload = {
            "version": 1,
            "cases": [
                {
                    "id": "smoke",
                    "type": "find_feature",
                    "args": {"query": "reload", "limit": 5},
                    "checks": [{"kind": "class_in_results", "value": value}],
                    "reason": "测试用",
                    "last_reviewed": "2026-11-15",
                }
            ],
        }
        self.cases_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def _run(self, *extra: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(
                ["--root", str(self.root), "build", "--quiet", *extra]
            )
        return code, buffer.getvalue()

    def test_check_passes(self) -> None:
        self._write_cases("TBWidget")
        code, output = self._run("--check")
        self.assertEqual(code, 0, output)
        self.assertIn("索引自检（eval）", output)
        self.assertIn("通过 1/1", output)

    def test_check_fails_with_exit_code_3(self) -> None:
        self._write_cases("NotExistingClass")
        code, output = self._run("--check")
        self.assertEqual(code, 3, output)
        self.assertIn("smoke", output)

    def test_check_skipped_without_cases(self) -> None:
        self.cases_path.unlink(missing_ok=True)
        code, output = self._run("--check")
        self.assertEqual(code, 0, output)
        self.assertIn("跳过 eval 自检", output)

    def test_no_check_does_not_run_eval(self) -> None:
        self._write_cases("NotExistingClass")
        code, output = self._run()
        self.assertEqual(code, 0, output)
        self.assertNotIn("索引自检", output)


if __name__ == "__main__":
    unittest.main()
