#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""向量层测试：文本格式 / 缓存 / Qdrant 构建 / 语义查询。

不下载模型：用确定性的假 embedder 走完整链路（Qdrant 本地模式 + 缓存 +
增量），真模型只在 ``python3 -m ios_qa vectors`` 时使用。
"""

from __future__ import annotations

import json
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ios_qa import cli
from ios_qa.build import build
from ios_qa.config import ProjectProfile
from ios_qa.embedding import (
    EMBED_DIM,
    QUERY_INSTRUCTION,
    EmbeddingCache,
    SparseVectorData,
    cache_key,
    document_text,
    get_profile,
    pack_vector,
    query_text,
    snippet_text,
    unpack_vector,
)
from ios_qa.fingerprint import code_tokens
from ios_qa.query import QueryService
from ios_qa.vector_build import build_vectors, method_document, read_meta, vector_dir
from ios_qa.vector_store import BODY_VECTOR, DENSE_VECTOR, VectorStore, point_id

try:
    import qdrant_client  # noqa: F401

    HAS_QDRANT = True
except ImportError:  # pragma: no cover - 可选依赖
    HAS_QDRANT = False

FAKE_DIM = 8


def fake_vector(text: str, dim: int = FAKE_DIM) -> list[float]:
    """按 token 词袋散列到 dim 维，语义相近的文本向量也相近。"""
    values = [0.0] * dim
    for token in code_tokens(text, "objc"):
        values[hash(token) % dim] += 1.0
    norm = sum(value * value for value in values) ** 0.5
    return [value / norm for value in values] if norm else values


class FakeDenseEmbedder:
    model = "fake-dense"
    dim = FAKE_DIM

    def __init__(self, *args, **kwargs) -> None:
        pass

    def embed_documents(self, texts: list[str]):
        return [fake_vector(text) for text in texts], 0

    def embed_query(self, text: str):
        return fake_vector(text)


class FakeSparseEmbedder:
    model = "fake-sparse"

    def __init__(self, *args, **kwargs) -> None:
        pass

    def embed_documents(self, texts: list[str]):
        vectors = []
        for text in texts:
            tokens = code_tokens(text, "objc")
            indices = sorted({hash(token) % 100000 for token in tokens})
            vectors.append(
                SparseVectorData(indices, [1.0 for _ in indices])
            )
        return vectors, 0

    def embed_query(self, text: str):
        tokens = code_tokens(text, "objc")
        indices = sorted({hash(token) % 100000 for token in tokens})
        return SparseVectorData(indices, [1.0 for _ in indices])


class FakeReranker:
    def __init__(self, score: float = 0.9, *args, cache=None, **kwargs) -> None:
        self.score = score

    def scores(self, query: str, documents: list[str]) -> list[float]:
        return [self.score for _ in documents]


def fake_vector_layer():
    """查询侧换成假 embedder，同时对齐 ``_vector_status`` 的模型契约。"""
    return mock.patch.multiple(
        "ios_qa.query",
        DenseEmbedder=FakeDenseEmbedder,
        SparseEmbedder=FakeSparseEmbedder,
        EMBED_MODEL=FakeDenseEmbedder.model,
        EMBED_DIM=FakeDenseEmbedder.dim,
    )


class EmbeddingTextTest(unittest.TestCase):
    def test_default_profile_is_qwen3(self) -> None:
        text = document_text("TBAlphaView", "loadData", "x" * 3000)
        self.assertEqual(text, "类:TBAlphaView 方法:loadData\n" + "x" * 2000)
        self.assertEqual(
            query_text(" 行情 "),
            "Instruct: " + QUERY_INSTRUCTION + "\nQuery: 行情",
        )
        self.assertEqual(snippet_text("- (void)run {}"), "- (void)run {}")

    def test_bge_profile_prefixes(self) -> None:
        profile = get_profile("bge-small-zh")
        text = profile.document_text("TBAlphaView", "loadData", "x" * 3000)
        self.assertTrue(text.startswith("search_document: 类:TBAlphaView 方法:loadData\n"))
        self.assertEqual(len(text.split("\n", 1)[1]), 2000)
        self.assertEqual(profile.query_text(" 行情 "), "search_query: 行情")
        self.assertTrue(profile.snippet_text("- (void)run {}").startswith("search_document: "))

    def test_document_text_carries_tags_and_comment(self) -> None:
        text = document_text(
            "TBCrashReporter",
            "handleException:",
            "report();",
            comment="MARK: - Actions\n崩溃上报入口",
            tags=["崩溃", "crash"],
        )
        self.assertIn("业务标签:崩溃 crash", text)
        self.assertIn("崩溃上报入口", text)
        self.assertNotIn("MARK:", text)

    def test_empty_comment_and_tags_keep_prefix_clean(self) -> None:
        text = document_text("TBAlphaView", "loadData", "body")
        self.assertEqual(text, "类:TBAlphaView 方法:loadData\nbody")

    def test_unknown_profile_rejected(self) -> None:
        with self.assertRaises(ValueError):
            get_profile("no-such-model")

    def test_cache_key_and_pack(self) -> None:
        self.assertEqual(cache_key("m", "t"), cache_key("m", "t"))
        self.assertNotEqual(cache_key("m", "t"), cache_key("m", "u"))
        blob = pack_vector([1.5, -2.0, 0.25])
        self.assertEqual(unpack_vector(blob, 3), [1.5, -2.0, 0.25])
        self.assertEqual(len(struct.unpack("<3f", blob)), 3)


class EmbeddingCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "embedding_cache.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_dense_and_sparse_roundtrip(self) -> None:
        with EmbeddingCache(self.path) as cache:
            cache.put_dense([("k1", [0.5] * EMBED_DIM)])
            cache.put_sparse(
                [("k2", SparseVectorData([1, 4, 9], [0.2, 0.3, 0.5]))]
            )
        with EmbeddingCache(self.path) as cache:
            dense = cache.get_dense(["k1", "missing"])
            sparse = cache.get_sparse(["k2"])
        self.assertEqual(len(dense["k1"]), EMBED_DIM)
        self.assertNotIn("missing", dense)
        self.assertEqual(sparse["k2"].indices, [1, 4, 9])
        self.assertAlmostEqual(sparse["k2"].values[2], 0.5, places=5)

    def test_rerank_cache_roundtrip(self) -> None:
        with EmbeddingCache(self.path) as cache:
            cache.put_rerank([("k1", 0.42)])
        with EmbeddingCache(self.path) as cache:
            self.assertEqual(cache.get_rerank(["k1", "missing"]), {"k1": 0.42})


HEADER = """\
#import <UIKit/UIKit.h>

@interface {name} : UIView
@property (nonatomic, strong) UITableView *tableView;
@end
"""

IMPL = """\
#import "{name}.h"

@implementation {name}
- (void)loadQuotesForCategory:(NSString *)category {{
    NSArray *items = [self.service fetchQuotesForCategory:category];
    self.items = items;
    [self.tableView reloadData];
    [self updateEmptyState:items.count];
    [self trackEvent:@"finance.home.load"];
{body}
}}
@end
"""


SWIFT_OVERLOAD = """\
import Foundation

final class TBDualInit: NSObject {
    var value = 0
    var label = ""
    var extra = true

    init(value: Int, label: String) {
        self.value = value
        self.label = label
        self.extra = value > 0
        super.init()
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        self.value = try container.decode(Int.self)
        self.label = try container.decode(String.self)
        self.extra = false
        super.init()
    }
}
"""


def _write_view(root: Path, name: str, body: str = "") -> Path:
    (root / "App" / f"{name}.h").write_text(
        HEADER.format(name=name), encoding="utf-8"
    )
    path = root / "App" / f"{name}.m"
    path.write_text(IMPL.format(name=name, body=body), encoding="utf-8")
    return path


@unittest.skipUnless(HAS_QDRANT, "需要 qdrant-client")
class VectorBuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "App").mkdir()
        self.alpha = _write_view(self.root, "TBAlphaView")
        # Beta 用一段独有语句，避免两个方法的方法体完全相同（片段自查会并列）
        _write_view(self.root, "TBBetaView", body="    [self.betaOnlySink consume];")
        self.db_path = self.root / "kb.db"
        self.profile = ProjectProfile(name="Demo", root=self.root, source_dirs=["."])
        build(self.profile, self.db_path, full=True, verbose=False)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _build(self, **kwargs):
        store = VectorStore(vector_dir(self.profile, self.db_path), dim=FAKE_DIM)
        cache = EmbeddingCache(self.root / "embedding_cache.db")
        try:
            report = build_vectors(
                self.profile,
                self.db_path,
                cache=cache,
                store=store,
                dense_embedder=FakeDenseEmbedder(),
                sparse_embedder=FakeSparseEmbedder(),
                **kwargs,
            )
        finally:
            cache.close()
        return report, store

    def test_template_change_forces_full_rebuild(self) -> None:
        """模板变了但源码没变：增量构建不能因为 sha256 相同就跳过重算。"""
        first, store = self._build(full=True)
        store.close()
        self.assertTrue(first.template_id)
        with mock.patch(
            "ios_qa.vector_build.template_id", return_value="changed-template"
        ):
            second, store = self._build()
        try:
            self.assertEqual(second.mode, "full")
            self.assertEqual(second.template_id, "changed-template")
            self.assertGreater(second.files_changed, 0)
            self.assertGreater(second.points_written, 0)
        finally:
            store.close()

    def test_full_build_writes_both_dense_channels(self) -> None:
        """片段通道和文档通道都是命名向量，meta 与 collection 都要记录。"""
        report, store = self._build(full=True)
        try:
            self.assertEqual(report.vectors, [DENSE_VECTOR, BODY_VECTOR])
            meta = read_meta(self.profile, self.db_path)
            self.assertEqual(meta["vectors"], [DENSE_VECTOR, BODY_VECTOR])
            self.assertEqual(store.vector_names(), [DENSE_VECTOR, BODY_VECTOR])
            point = store.find_method("TBAlphaView", "loadQuotesForCategory:")
            self.assertTrue(point["dense"])
            self.assertTrue(point["dense_body"])
            self.assertEqual(report.body_embedded, 2)
        finally:
            store.close()

    def test_snippet_self_lookup_is_near_one(self) -> None:
        """同构的片段通道：贴一段已索引的 body，第一名是它自己且分数≈1。"""
        _report, store = self._build(full=True)
        store.close()
        with fake_vector_layer(), QueryService(self.profile, self.db_path) as service:
            row = service.conn.execute(
                "SELECT body FROM methods WHERE class_name='TBAlphaView' "
                "AND method_name='loadQuotesForCategory:'"
            ).fetchone()
            data = service.search_similar_snippet(
                row["body"], limit=3, min_similarity=0.0
            )
        self.assertTrue(data["available"], data.get("hint"))
        self.assertTrue(data["results"])
        top = data["results"][0]
        self.assertEqual(
            (top["class_name"], top["method_name"]),
            ("TBAlphaView", "loadQuotesForCategory:"),
        )
        self.assertGreater(top["similarity"], 0.99)

    def test_snippet_and_similar_code_use_body_channel(self) -> None:
        """路由不变量：片段/以方法找相似走 dense_body，自然语言检索走 dense。"""
        _report, store = self._build(full=True)
        store.close()
        calls: list[str] = []
        base = VectorStore

        class RecordingStore(base):  # type: ignore[misc, valid-type]
            def search_dense(self, vector, limit, module="", using=DENSE_VECTOR):
                calls.append(using)
                return super().search_dense(vector, limit, module, using=using)

        with fake_vector_layer(), mock.patch(
            "ios_qa.vector_store.VectorStore", RecordingStore
        ), QueryService(self.profile, self.db_path) as service:
            service.vector_search("load quotes category", limit=3)
            natural_language_calls = list(calls)
            service.find_similar_code(
                "TBAlphaView.loadQuotesForCategory:", mode="semantic", limit=3
            )
            service.search_similar_snippet(
                "- (void)run { [self.tableView reloadData]; }", limit=3
            )
        # 自然语言检索走 hybrid（dense + sparse RRF），不碰片段通道
        self.assertEqual(natural_language_calls, [])
        self.assertEqual(calls, [BODY_VECTOR, BODY_VECTOR])

    def test_vector_status_rejects_layout_without_body_channel(self) -> None:
        """旧索引只有 dense：要给出重建提示，而不是让 Qdrant 抛未知向量名。"""
        _report, store = self._build(full=True)
        store.close()
        meta_path = vector_dir(self.profile, self.db_path) / "build_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.pop("vectors", None)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        with fake_vector_layer(), QueryService(self.profile, self.db_path) as service:
            available, hint = service._vector_status()
        self.assertFalse(available)
        self.assertIn(BODY_VECTOR, hint)
        self.assertIn("vectors", hint)

    def test_method_document_uses_synonyms_and_comment(self) -> None:
        groups = [["崩溃", "crash"], ["网络", "请求", "http", "api"]]
        row = {
            "class_name": "TBCrashReporter",
            "method_name": "handleException:",
            "module": "Monitor",
            "doc_comment": "MARK: - Actions\nexception 上报入口",
            "body": "report();",
        }
        text = method_document(row, groups)
        self.assertIn("业务标签:崩溃", text)
        self.assertNotIn("网络", text)
        self.assertIn("exception 上报入口", text)
        self.assertNotIn("MARK:", text)

    def test_method_document_skips_tags_already_in_text(self) -> None:
        groups = [["崩溃", "crash"]]
        row = {
            "class_name": "TBCrashReporter",
            "method_name": "handleException:",
            "module": "Monitor",
            "doc_comment": "崩溃上报入口",
            "body": "report();",
        }
        self.assertNotIn("业务标签", method_document(row, groups))

    def test_full_build_writes_points_and_meta(self) -> None:
        report, store = self._build(full=True)
        try:
            self.assertEqual(report.methods_selected, 2)
            self.assertEqual(report.points_written, 2)
            self.assertEqual(store.count(), 2)
            meta = read_meta(self.profile, self.db_path)
            self.assertEqual(meta["method_count"], 2)
            self.assertEqual(meta["dim"], FAKE_DIM)
            self.assertTrue((vector_dir(self.profile, self.db_path) / "file_hashes.json").exists())

            payload = store.find_method("TBAlphaView", "loadQuotesForCategory:")["payload"]
            self.assertEqual(payload["module"], "App")
            self.assertEqual(payload["language"], "objc")
            self.assertGreater(payload["line_end"], payload["line_start"])
            self.assertIn("fetchQuotes", payload["body_preview"])
        finally:
            store.close()

    def test_incremental_only_reindexes_changed_files(self) -> None:
        report, store = self._build(full=True)
        store.close()
        second, store = self._build()
        try:
            self.assertEqual(second.files_changed, 0)
            self.assertEqual(second.points_written, 0)
            self.assertEqual(second.collection_points, 2)
        finally:
            store.close()

        self.alpha.write_text(
            self.alpha.read_text(encoding="utf-8").replace(
                "finance.home.load", "finance.home.refresh"
            ),
            encoding="utf-8",
        )
        build(self.profile, self.db_path, verbose=False)
        third, store = self._build()
        try:
            self.assertEqual(third.files_changed, 1)
            self.assertEqual(third.points_written, 1)
            self.assertEqual(third.collection_points, 2)
        finally:
            store.close()

    def test_deleted_file_points_removed(self) -> None:
        report, store = self._build(full=True)
        store.close()
        (self.root / "App" / "TBBetaView.m").unlink()
        (self.root / "App" / "TBBetaView.h").unlink()
        build(self.profile, self.db_path, verbose=False)
        fourth, store = self._build()
        try:
            # .h 与 .m 都是索引文件，删掉一对就少两条
            self.assertEqual(fourth.files_deleted, 2)
            self.assertEqual(store.count(), 1)
            self.assertIsNone(store.find_method("TBBetaView", "loadQuotesForCategory:"))
        finally:
            store.close()

    def test_vector_store_hybrid_and_filter(self) -> None:
        _report, store = self._build(full=True)
        try:
            dense = fake_vector("load quotes category")
            sparse = FakeSparseEmbedder().embed_query("load quotes category")
            hits = store.search_hybrid(dense, sparse, limit=5)
            self.assertTrue(hits)
            self.assertEqual(hits[0]["fusion"], "rrf")
            filtered = store.search_dense(dense, limit=5, module="Nope")
            self.assertEqual(filtered, [])
            self.assertEqual(
                store.search_dense(dense, limit=5, module="App")[0]["module"], "App"
            )
        finally:
            store.close()

    def test_points_are_idempotent(self) -> None:
        _report, store = self._build(full=True)
        point = store.find_method("TBAlphaView", "loadQuotesForCategory:")
        self.assertEqual(
            point["id"],
            point_id(
                "App/TBAlphaView.m",
                "loadQuotesForCategory:",
                point["payload"]["line_start"],
            ),
        )
        store.close()

    def test_overloaded_methods_get_distinct_points(self) -> None:
        """同一个文件里的同名重载不能共用一个 point。

        历史缺陷：point_id 只用 ``file:method``，Swift 的 ``init`` 与
        ``init(from:)`` 都叫 ``init``，后写入的覆盖前一个，导致 356 字符的
        init 根本不在索引里（查自身 0.59）。
        """
        (self.root / "App" / "TBDualInit.swift").write_text(
            SWIFT_OVERLOAD, encoding="utf-8"
        )
        build(self.profile, self.db_path, verbose=False)
        report, store = self._build(full=True)
        try:
            db = sqlite3.connect(self.db_path)
            try:
                lines = [
                    row[0]
                    for row in db.execute(
                        "SELECT line_start FROM methods "
                        "WHERE class_name='TBDualInit' AND method_name='init' "
                        "ORDER BY line_start"
                    )
                ]
            finally:
                db.close()
            self.assertEqual(len(lines), 2, lines)
            for line in lines:
                point = store.find_method("TBDualInit", "init", line)
                self.assertIsNotNone(point, line)
                self.assertEqual(point["payload"]["line_start"], line)
            # 不给行号时稳定取最靠前的一条
            first = store.find_method("TBDualInit", "init")
            self.assertEqual(first["payload"]["line_start"], lines[0])
            # 两个 init 各自占一个 point：2 个 View 方法 + 2 个 init
            self.assertEqual(report.methods_selected, 4)
        finally:
            store.close()

    def test_query_service_semantic_layer(self) -> None:
        _report, store = self._build(full=True)
        store.close()
        with fake_vector_layer(), QueryService(self.profile, self.db_path) as service:
            search = service.vector_search("load quotes category", limit=5)
            self.assertTrue(search["available"])
            self.assertEqual(search["fusion"], "rrf")
            self.assertTrue(search["results"])

            similar = service.find_similar_code(
                "TBAlphaView.loadQuotesForCategory:", mode="semantic", limit=5
            )
            self.assertTrue(similar["available"])
            self.assertEqual(similar["resolved"]["class_name"], "TBAlphaView")
            self.assertTrue(similar["results"])
            self.assertIn("advice", similar["results"][0])

            snippet = service.search_similar_snippet(
                "- (void)loadQuotes { [self.service fetchQuotesForCategory:category]; "
                "[self.tableView reloadData]; }",
                limit=5,
                min_similarity=0.1,
            )
            self.assertTrue(snippet["available"])
            self.assertTrue(snippet["results"])
            self.assertIn("source_code", snippet["results"][0])
            self.assertIn("fetchQuotesForCategory", snippet["results"][0]["source_code"])

    def test_hybrid_find_feature_merges_by_class(self) -> None:
        _report, store = self._build(full=True)
        store.close()
        with fake_vector_layer(), QueryService(self.profile, self.db_path) as service:
            data = service.hybrid_find_feature(
                "load quotes category", limit=5, rerank=False
            )
        self.assertTrue(data["available"])
        self.assertTrue(data["vector_available"])
        self.assertTrue(data["results"])
        scores = [row["score"] for row in data["results"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(
            any("vector" in row["sources"] for row in data["results"]),
            data["results"],
        )
        for row in data["results"]:
            if "vector" in row["sources"]:
                self.assertTrue(row["vector_method"]["method_name"])
                break

    def test_hybrid_rerank_sets_method_level_score(self) -> None:
        _report, store = self._build(full=True)
        store.close()
        with fake_vector_layer(), mock.patch(
            "ios_qa.query.CrossEncoderReranker", FakeReranker
        ), QueryService(self.profile, self.db_path) as service:
            data = service.hybrid_find_feature("load quotes category", limit=5)
        self.assertTrue(data["reranked"])
        alpha = next(
            row for row in data["results"] if row["class_name"] == "TBAlphaView"
        )
        self.assertEqual(alpha["rerank_method"]["method_name"], "loadQuotesForCategory:")
        scores = [row["score"] for row in data["results"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_rerank_pairs_dedupe_and_have_methods(self) -> None:
        _report, store = self._build(full=True)
        store.close()
        with fake_vector_layer(), QueryService(self.profile, self.db_path) as service:
            pairs = service._rerank_pairs("load quotes", [], "")
            self.assertTrue(pairs)
            keys = {(pair["class_name"], pair["method_name"]) for pair in pairs}
            self.assertEqual(len(keys), len(pairs))
            document = service._rerank_document(pairs[0])
            self.assertIn(pairs[0]["class_name"], document)
            self.assertIn(pairs[0]["method_name"], document)

    def test_rerank_documents_are_built_from_the_pairs(self) -> None:
        """不变量：精排输入必须由候选自身构造。

        历史缺陷：用「类 + 向量最近的方法」当 document，向量最近的方法可能是
        无关实现，CE 于是给错的对象打分（行情页选中 tb_updateMonitorScreen）。
        这里断言 CE 收到的每一份文档，前两行是真实存在的 (类, 方法)，且文档里
        带的是这个方法自己的方法体。
        """
        class RecordingReranker(FakeReranker):
            documents: list[str] = []

            def scores(self, query: str, documents: list[str]) -> list[float]:
                RecordingReranker.documents = list(documents)
                return super().scores(query, documents)

        _report, store = self._build(full=True)
        store.close()
        with fake_vector_layer(), mock.patch(
            "ios_qa.query.CrossEncoderReranker", RecordingReranker
        ), QueryService(self.profile, self.db_path) as service:
            data = service.hybrid_find_feature("load quotes", limit=5)
            recorded = RecordingReranker.documents
            self.assertTrue(recorded)
            for document in recorded:
                first, second = document.splitlines()[:2]
                row = service.conn.execute(
                    "SELECT body FROM methods WHERE class_name=? AND method_name=? "
                    "ORDER BY line_start LIMIT 1",
                    (first, second),
                ).fetchone()
                self.assertIsNotNone(row, f"精排文档头不是候选: {first}.{second}")
                body = (row["body"] or "").strip()
                if body:
                    # 文档必须带上这个方法自己的方法体，而不是同类里别的方法
                    self.assertIn(body.splitlines()[0].strip(), document)
            ranked = [row for row in data["results"] if row.get("rerank_method")]
            self.assertTrue(ranked)
            recorded_keys = {tuple(doc.splitlines()[:2]) for doc in recorded}
            for row in ranked:
                self.assertIn(
                    (row["class_name"], row["rerank_method"]["method_name"]),
                    recorded_keys,
                )

    def test_vector_status_rejects_model_mismatch(self) -> None:
        _report, store = self._build(full=True)
        store.close()
        with mock.patch("ios_qa.query.EMBED_MODEL", "other/model"), QueryService(
            self.profile, self.db_path
        ) as service:
            data = service.vector_search("load quotes")
        self.assertFalse(data["available"])
        self.assertIn("重新运行", data["hint"])

    def test_vector_status_detects_stale_source(self) -> None:
        _report, store = self._build(full=True)
        store.close()
        self.alpha.write_text(
            self.alpha.read_text(encoding="utf-8") + "\n// changed\n",
            encoding="utf-8",
        )
        build(self.profile, self.db_path, verbose=False)
        with fake_vector_layer(), QueryService(self.profile, self.db_path) as service:
            data = service.vector_search("load quotes")
        self.assertFalse(data["available"])
        self.assertIn("变更", data["hint"])

    def test_unavailable_message_when_no_vectors(self) -> None:
        with QueryService(self.profile, self.db_path) as service:
            data = service.vector_search("行情")
        self.assertFalse(data["available"])
        self.assertIn("vectors", data["hint"])

    def test_cli_vectors_and_semantic(self) -> None:
        import contextlib
        import io

        base = ["--root", str(self.root), "--db", str(self.db_path)]
        with mock.patch(
            "ios_qa.vector_build.DenseEmbedder", FakeDenseEmbedder
        ), mock.patch("ios_qa.vector_build.SparseEmbedder", FakeSparseEmbedder):
            with contextlib.redirect_stdout(io.StringIO()) as buffer:
                code = cli.main(["vectors", "--full", *base])
        self.assertEqual(code, 0)
        self.assertIn("points=2", buffer.getvalue())

        with fake_vector_layer():
            with contextlib.redirect_stdout(io.StringIO()) as buffer:
                code = cli.main(["semantic", "load quotes", *base])
            self.assertEqual(code, 0)
            self.assertIn("TBAlphaView", buffer.getvalue())

            with contextlib.redirect_stdout(io.StringIO()) as buffer:
                code = cli.main(
                    ["similar", "TBAlphaView.loadQuotesForCategory:", "--mode", "semantic", *base]
                )
            self.assertEqual(code, 0)
            self.assertIn("语义近邻", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
