"""P4.1 评测数据集框架测试。

覆盖：
- 种子集可加载、条数 >= 20；
- schema 校验通过（validate_dataset 无错误）；
- 负例（has_answer=false）存在且 relevant_chunks 为空；
- 坏样本（缺字段 / 非法 JSON / 非法枚举 / 负例带定位）能被检出；
- resolve_relevant_chunks 骨架可调用（内存 SQLite 造 Chunk 记录 + 空库返回空）。
"""

import json

import pytest

from app.models import Chunk
from eval.dataset import (
    DEFAULT_SEED_PATH,
    load_dataset,
    resolve_relevant_chunks,
    validate_dataset,
)


# ---------- 种子集加载与规模 ----------

def test_seed_loads_and_has_enough_items():
    """种子集可加载且条数 >= 20。"""
    items = load_dataset()
    assert isinstance(items, list)
    assert len(items) >= 20
    assert all(isinstance(i, dict) for i in items)


def test_seed_validate_passes():
    """种子集 schema 校验通过（不抛异常）。"""
    items = load_dataset()
    validate_dataset(items)  # 不抛 ValueError 即通过


def test_seed_qa_ids_unique():
    """qa_id 全局唯一。"""
    items = load_dataset()
    ids = [i["qa_id"] for i in items]
    assert len(ids) == len(set(ids))


# ---------- 负例 ----------

def test_seed_has_negative_examples():
    """负例存在：has_answer=false、question_type=out_of_scope、relevant_chunks 为空。"""
    items = load_dataset()
    negatives = [i for i in items if i["has_answer"] is False]
    assert len(negatives) >= 2
    for item in negatives:
        assert item["question_type"] == "out_of_scope"
        assert item["relevant_chunks"] == []


def test_question_type_coverage():
    """正例覆盖多种问题类型。"""
    items = load_dataset()
    types = {i["question_type"] for i in items}
    assert {"factoid", "summary", "comparison", "method_detail",
            "experiment_data", "out_of_scope"} <= types


# ---------- 坏样本检出 ----------

def _valid_item(**overrides):
    """构造一条合法样本，可按需覆盖字段。"""
    item = {
        "qa_id": "test-001",
        "question": "测试问题？",
        "ground_truth": "测试答案要点。",
        "relevant_chunks": [{"paper_id": 1, "section": "Method", "keywords": ["CAFR"]}],
        "question_type": "factoid",
        "source": "demo_paper",
        "has_answer": True,
    }
    item.update(overrides)
    return item


def test_validate_detects_missing_field():
    """缺必填字段能被检出。"""
    item = _valid_item()
    del item["ground_truth"]
    with pytest.raises(ValueError, match="ground_truth"):
        validate_dataset([item])


def test_validate_detects_duplicate_qa_id():
    """qa_id 重复能被检出。"""
    with pytest.raises(ValueError, match="重复"):
        validate_dataset([_valid_item(), _valid_item()])


def test_validate_detects_illegal_enums():
    """非法 question_type / source 能被检出。"""
    with pytest.raises(ValueError, match="question_type"):
        validate_dataset([_valid_item(question_type="not_a_type")])
    with pytest.raises(ValueError, match="source"):
        validate_dataset([_valid_item(source="nowhere")])


def test_validate_detects_negative_with_chunks():
    """负例却标注了 relevant_chunks 能被检出。"""
    item = _valid_item(has_answer=False, question_type="out_of_scope")
    with pytest.raises(ValueError, match="负例"):
        validate_dataset([item])


def test_validate_detects_bad_locator():
    """定位对象缺 section 和 keywords 能被检出。"""
    item = _valid_item(relevant_chunks=[{"paper_id": 1}])
    with pytest.raises(ValueError, match="section 或 keywords"):
        validate_dataset([item])


def test_load_detects_invalid_json(tmp_path):
    """非法 JSON 行能被 load_dataset 检出（报错带行号）。"""
    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        json.dumps(_valid_item(), ensure_ascii=False) + "\n{not json}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="第 2 行"):
        load_dataset(bad)


def test_load_missing_file(tmp_path):
    """文件不存在抛 FileNotFoundError。"""
    with pytest.raises(FileNotFoundError):
        load_dataset(tmp_path / "nope.jsonl")


# ---------- resolve_relevant_chunks ----------

def test_resolve_empty_db_returns_empty(db):
    """空库返回空列表。"""
    entry = _valid_item()
    assert resolve_relevant_chunks(db, entry) == []


def test_resolve_negative_entry_returns_empty(db):
    """负例（无定位信息）直接返回空列表。"""
    entry = _valid_item(has_answer=False, question_type="out_of_scope",
                        relevant_chunks=[])
    assert resolve_relevant_chunks(db, entry) == []


def test_resolve_matches_chunks(db):
    """内存库造 Chunk 记录：按 section/keywords 命中并返回 p{paper_id}_c{i} 形式 id。"""
    db.add_all([
        Chunk(paper_id=1, chunk_index=0, section_title="Abstract",
              content="We propose ReCo-MIL for T-stage prediction."),
        Chunk(paper_id=1, chunk_index=1, section_title="3. Method",
              content="The CAFR module computes pairwise attention with a BiGRU aggregator."),
        Chunk(paper_id=1, chunk_index=2, section_title=None,
              content="Unrelated text without any keyword."),
        Chunk(paper_id=2, chunk_index=0, section_title="Method",
              content="CAFR in another paper should not be returned."),
    ])
    db.commit()

    entry = _valid_item(
        relevant_chunks=[{"paper_id": 1, "section": "Method", "keywords": ["CAFR"]}]
    )
    ids = resolve_relevant_chunks(db, entry)
    # 命中 paper_id=1 的 Method chunk；不命中无关 chunk 与其它论文 chunk
    assert "p1_c1" in ids
    assert "p1_c2" not in ids
    assert "p2_c0" not in ids
    # section_title 为 NULL 时回退到 content 匹配 section 字符串
    entry2 = _valid_item(relevant_chunks=[{"paper_id": 1, "section": "t-stage"}])
    assert resolve_relevant_chunks(db, entry2) == ["p1_c0"]


def test_default_seed_path_exists():
    """内置种子集文件存在。"""
    assert DEFAULT_SEED_PATH.exists()
