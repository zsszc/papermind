"""EmbeddingService / TextChunker 契约测试（Batch 7 / F5+F6）。

- F5：embed(texts, batch_size=N) 的 N 必须透传到 worker 的 encode
- F6：TextChunker 默认参数从 config 的 embedding.chunk_size/chunk_overlap 读取，
      显式传参优先，非法配置回退 512/50
"""

import numpy as np
import pytest

from app.core.config import config
from app.services.embedding import EmbeddingService, TextChunker


class _FakeModel:
    """记录 encode 收到的 batch_size，返回固定形状向量。"""

    last_batch_size = None

    def encode(self, texts, normalize_embeddings, convert_to_numpy, show_progress_bar, batch_size):
        _FakeModel.last_batch_size = batch_size
        return np.zeros((len(texts), 4))


class TestEmbedBatchSize:
    def test_batch_size_passed_through(self, monkeypatch):
        svc = EmbeddingService()  # 单例；worker 线程随首次实例化启动（daemon）
        monkeypatch.setattr(svc, "_load_model", lambda: _FakeModel())
        svc.embed(["alpha", "beta"], batch_size=3)
        assert _FakeModel.last_batch_size == 3

    def test_default_batch_size_unchanged(self, monkeypatch):
        svc = EmbeddingService()
        monkeypatch.setattr(svc, "_load_model", lambda: _FakeModel())
        svc.embed(["alpha"])
        assert _FakeModel.last_batch_size == 16  # embed 默认值


class TestChunkerConfig:
    def test_reads_config_values(self, monkeypatch):
        monkeypatch.setattr(
            config, "_config", {"embedding": {"chunk_size": 100, "chunk_overlap": 10}}
        )
        c = TextChunker()
        assert c.chunk_size == 100
        assert c.chunk_overlap == 10

    def test_explicit_args_win_over_config(self, monkeypatch):
        monkeypatch.setattr(config, "_config", {"embedding": {"chunk_size": 100}})
        c = TextChunker(chunk_size=300, chunk_overlap=30)
        assert c.chunk_size == 300
        assert c.chunk_overlap == 30

    def test_invalid_config_falls_back(self, monkeypatch):
        monkeypatch.setattr(
            config, "_config", {"embedding": {"chunk_size": "abc", "chunk_overlap": -1}}
        )
        c = TextChunker()
        assert c.chunk_size == 512
        assert c.chunk_overlap == 50

    def test_missing_config_uses_defaults(self, monkeypatch):
        monkeypatch.setattr(config, "_config", {})
        c = TextChunker()
        assert (c.chunk_size, c.chunk_overlap) == (512, 50)


class TestChunkerHardLimit:
    """Batch 22C：单个超长段落也必须遵守真实字符上限。"""

    def test_long_single_paragraph_prefers_sentence_boundaries(self):
        chunker = TextChunker(chunk_size=12, chunk_overlap=0)

        chunks = chunker.chunk_pages([{
            "page_number": 7,
            "text": "第一句话很短。第二句话也很短。第三句话结束。",
        }])

        assert len(chunks) >= 3
        assert all(0 < len(item["content"]) <= 12 for item in chunks)
        assert all(item["page_number"] == 7 for item in chunks)
        assert chunks[0]["content"].endswith("。")
        assert chunks[1]["content"].endswith("。")

    def test_sentence_boundary_wins_over_later_whitespace(self):
        chunker = TextChunker(chunk_size=20, chunk_overlap=0)

        chunks = chunker.chunk_pages([{
            "page_number": 8,
            "text": "0123456789; abcde fghijklmnop",
        }])

        assert chunks[0]["content"] == "0123456789;"

    def test_boundary_free_text_uses_fixed_window_with_bounded_overlap(self):
        chunker = TextChunker(chunk_size=10, chunk_overlap=2)

        chunks = chunker.chunk_pages([{"page_number": 1, "text": "x" * 35}])

        assert [len(item["content"]) for item in chunks] == [10, 10, 10, 10, 3]
        assert all(
            previous["content"][-2:] == current["content"][:2]
            for previous, current in zip(chunks, chunks[1:])
        )

    def test_join_separator_counts_toward_hard_limit(self):
        chunker = TextChunker(chunk_size=10, chunk_overlap=0)

        chunks = chunker.chunk_pages([{
            "page_number": 2,
            "text": "12345\n\n67890",
        }])

        assert [item["content"] for item in chunks] == ["12345", "67890"]
        assert all(item["token_count"] == len(item["content"]) for item in chunks)
        assert chunks[0]["page_end"] >= chunks[1]["page_start"]

    def test_overlap_larger_than_chunk_size_cannot_stall(self):
        chunker = TextChunker(chunk_size=8, chunk_overlap=50)

        chunks = chunker.chunk_pages([{"page_number": 3, "text": "z" * 20}])

        assert len(chunks) == 13
        assert all(0 < len(item["content"]) <= 8 for item in chunks)
        assert chunks[-1]["content"] == "z" * 8


class TestChunkerPageOffsets:
    """Batch 22D：正文 chunk 必须保留原始页文本半开坐标。"""

    def test_grouped_paragraph_has_page_envelope(self):
        text = "  first paragraph  \n\n   second paragraph   "
        chunks = TextChunker(chunk_size=100, chunk_overlap=0).chunk_pages([{
            "page_number": 4,
            "text": text,
        }])

        assert chunks == [{
            "content": "first paragraph\n\nsecond paragraph",
            "page_number": 4,
            "page_start": text.index("first"),
            "page_end": text.index("paragraph", text.index("second")) + len("paragraph"),
            "chunk_type": "paragraph",
            "token_count": len("first paragraph\n\nsecond paragraph"),
        }]

    def test_hard_split_overlap_offsets_match_original_text(self):
        text = "abcdefghijklmnopqrstuvwxyz"
        chunks = TextChunker(chunk_size=10, chunk_overlap=3).chunk_pages([{
            "page_number": 2,
            "text": text,
        }])

        assert [(row["page_start"], row["page_end"]) for row in chunks] == [
            (0, 10), (7, 17), (14, 24), (21, 26),
        ]
        assert all(
            row["content"] == text[row["page_start"]:row["page_end"]]
            for row in chunks
        )
