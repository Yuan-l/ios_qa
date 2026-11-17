#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""golden 断言：排序 + 间隔，以及索引文本契约绑定。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ios_qa.config import ProjectProfile
from ios_qa.eval import _check, run_eval


def _vectors_payload(rows: list[tuple[str, str, float]]) -> dict:
    return {
        "available": True,
        "results": [
            {"class_name": cls, "method_name": method, "score": score,
             "similarity": score}
            for cls, method, score in rows
        ],
    }


class VectorTopMarginTest(unittest.TestCase):
    """min_margin：只断言「比第二名高出多少」，不断言模型相关的绝对值。"""

    def setUp(self) -> None:
        self.data = _vectors_payload(
            [("MainCameraEditViewController", "saveImageToTempDirectory", 0.8034),
             ("ImageEditorViewController", "image", 0.6978)]
        )

    def _check(self, spec: dict) -> tuple[bool, str]:
        return _check(self.data, spec)

    def test_margin_passes_without_absolute_threshold(self) -> None:
        ok, detail = self._check(
            {
                "kind": "vector_top",
                "value": "MainCameraEditViewController.saveImageToTempDirectory",
                "min_margin": 0.05,
            }
        )
        self.assertTrue(ok, detail)
        self.assertIn("margin=0.1056", detail)

    def test_margin_fails_when_second_is_too_close(self) -> None:
        ok, detail = self._check(
            {
                "kind": "vector_top",
                "value": "MainCameraEditViewController.saveImageToTempDirectory",
                "min_margin": 0.2,
            }
        )
        self.assertFalse(ok)
        self.assertIn("min_margin=0.2", detail)

    def test_top_value_must_match(self) -> None:
        ok, _ = self._check(
            {"kind": "vector_top", "value": "ImageEditorViewController.image"}
        )
        self.assertFalse(ok)

    def test_single_result_margin_is_its_own_score(self) -> None:
        data = _vectors_payload([("TBAlphaView", "reload", 0.91)])
        ok, detail = _check(
            data, {"kind": "similar_top", "value": "TBAlphaView.reload",
                   "min_margin": 0.8}
        )
        self.assertTrue(ok, detail)
        self.assertIn("margin=0.9100", detail)

    def test_no_margin_keeps_old_semantics(self) -> None:
        ok, detail = self._check(
            {
                "kind": "vector_top",
                "value": "MainCameraEditViewController.saveImageToTempDirectory",
                "min_score": 0.9,
            }
        )
        self.assertFalse(ok, detail)


class _FakeService:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self) -> "_FakeService":
        return self

    def __exit__(self, *args) -> bool:
        return False

    def _vector_status(self) -> tuple[bool, str]:
        return True, ""

    def vector_search(self, query: str, limit: int = 5, module: str = "") -> dict:
        return {"query": query, "available": True, "results": []}


class TemplateBindingTest(unittest.TestCase):
    """索引文本契约变了，向量类用例要转「待复核」，不能静默判红/判绿。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".ios_qa").mkdir()
        self.cases_path = self.root / ".ios_qa" / "eval_cases.json"
        self.profile = ProjectProfile.load(root=self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_cases(self, template: str | None) -> None:
        payload = {
            "version": 1,
            "cases": [
                {
                    "id": "vector-only",
                    "type": "vector_search",
                    "requires": "vectors",
                    "args": {"query": "行情"},
                    "checks": [{"kind": "vector_available"}],
                    "reason": "fixture",
                    "last_reviewed": "2026-11-18",
                }
            ],
        }
        if template is not None:
            payload["template_id"] = template
        self.cases_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def _run(self, built: str):
        with mock.patch("ios_qa.eval.QueryService", _FakeService), mock.patch(
            "ios_qa.eval.read_meta", return_value={"template_id": built}
        ):
            return run_eval(self.profile, self.root / "qa.db")

    def test_stale_template_marks_needs_review(self) -> None:
        self._write_cases("old-template")
        report = self._run("new-template")
        self.assertEqual(report["needs_review"], 1)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["passed"], 0)
        self.assertIn("需人工复核", report["cases"][0]["review_reason"])

    def test_matching_template_runs_normally(self) -> None:
        self._write_cases("same-template")
        report = self._run("same-template")
        self.assertEqual(report["needs_review"], 0)
        self.assertEqual(report["passed"], 1)
        self.assertEqual(report["template_warning"], "")

    def test_missing_template_id_only_warns(self) -> None:
        self._write_cases(None)
        report = self._run("new-template")
        self.assertEqual(report["passed"], 1)
        self.assertIn("未绑定 template_id", report["template_warning"])


if __name__ == "__main__":
    unittest.main()
