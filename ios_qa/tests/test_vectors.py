#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""向量层测试：文本格式 / 缓存 / Qdrant 构建 / 语义查询。

不下载模型：用确定性的假 embedder 走完整链路（Qdrant 本地模式 + 缓存 +
增量），真模型只在 ``python3 -m ios_qa vectors`` 时使用。
"""

from __future__ import annotations

import json
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
    EmbeddingCache,
    SparseVectorData,
    cache_key,
    document_text,
    pack_vector,
    query_text,
    snippet_text,
    unpack_vector,
)
from ios_qa.fingerprint import code_tokens
from ios_qa.query import QueryService
from ios_qa.vector_build import build_vectors, read_meta, vector_dir
from ios_qa.vector_store import VectorStore, point_id

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


class EmbeddingTextTest(unittest.TestCase):
    def test_document_query_snippet_prefixes(self) -> None:
        text = document_text("TBAlphaView", "loadData", "x" * 3000)
        self.assertTrue(text.startswith("search_document: 类:TBAlphaView 方法:loadData\n"))
        self.assertEqual(len(text.split("\n", 1)[1]), 2000)
        self.assertEqual(query_text(" 行情 "), "search_query: 行情")
        self.assertTrue(snippet_text("- (void)run {}").startswith("search_document: "))

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
}}
@end
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
        _write_view(self.root, "TBBetaView")
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
        self.assertEqual(
            store.find_method("TBAlphaView", "loadQuotesForCategory:")["id"],
            point_id("App/TBAlphaView.m", "loadQuotesForCategory:"),
        )
        store.close()

    def test_query_service_semantic_layer(self) -> None:
        _report, store = self._build(full=True)
        store.close()
        with mock.patch("ios_qa.query.DenseEmbedder", FakeDenseEmbedder), mock.patch(
            "ios_qa.query.SparseEmbedder", FakeSparseEmbedder
        ), QueryService(self.profile, self.db_path) as service:
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

        with mock.patch("ios_qa.query.DenseEmbedder", FakeDenseEmbedder), mock.patch(
            "ios_qa.query.SparseEmbedder", FakeSparseEmbedder
        ):
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
