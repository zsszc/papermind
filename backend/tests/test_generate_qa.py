"""eval.generate_qa 单元测试。

全部通过 fake call_llm 注入 LLM 返回值，绝不触发真实 Kimi API 调用。
覆盖：
- parse_llm_json：裸 JSON / ```json 围栏 / 首尾杂质 / 半截 JSON 报错；
- generate_for_paper：happy path schema 与种子集一致（经 normalize_for_validation
  后可被 validate_dataset 接受）、审稿标记、JSON 解析失败重试、全部失败降级为空、
  摘录未命中原文的条目被过滤、LLM 生成 out_of_scope 被过滤；
- generate_cross_paper：跨论文 comparison 需两篇均可定位；
- generate_all：内存库端到端写 JSONL，逐行合法、汇总正确。
"""

import json

import pytest

from app.models import Chunk, Paper
from eval import generate_qa
from eval.dataset import validate_dataset


# ---------------------------------------------------------------------------
# 公共测试数据
# ---------------------------------------------------------------------------

PAPER_TEXT = (
    "We propose PAMIL, a prototype attention-based multiple instance learning "
    "framework for whole slide image classification. PAMIL achieves 0.912 AUC "
    "on the CAMELYON16 dataset, outperforming AttentionMIL which achieves "
    "0.872 AUC. The prototype attention module aggregates instance features "
    "into bag-level representations. We use Adam optimizer with learning rate "
    "1e-4 and cosine decay schedule."
)


def _fake_paper(pid: int = 4, title: str = "PAMIL: Prototype Attention MIL"):
    """构造不挂 session 的 Paper 对象。"""
    return Paper(id=pid, title=title, abstract=None,
                 file_path=f"papers/p{pid}.pdf", filename=f"p{pid}.pdf")


def _fake_chunks(pid: int = 4, text: str = PAPER_TEXT):
    return [Chunk(paper_id=pid, chunk_index=0, content=text)]


def test_build_material_samples_beginning_middle_and_end_under_budget():
    chunks = [
        Chunk(paper_id=4, chunk_index=index, content=(f"MARKER-{index} " + "x" * 500))
        for index in range(9)
    ]

    material = generate_qa.build_material(_fake_paper(), chunks, budget=900)

    assert "MARKER-0" in material
    assert "MARKER-4" in material
    assert "MARKER-8" in material
    assert len(material) <= 900


def _valid_payload() -> str:
    """一条合法的双条目 LLM 输出（摘录均逐字命中 PAPER_TEXT）。"""
    return json.dumps({"items": [
        {
            "question": "PAMIL 在 CAMELYON16 上的 AUC 是多少？",
            "question_type": "experiment_data",
            "ground_truth": "0.912 AUC、优于 AttentionMIL 的 0.872",
            "excerpts": ["achieves 0.912 AUC", "on the CAMELYON16 dataset"],
        },
        {
            "question": "PAMIL 使用的优化器与学习率策略是什么？",
            "question_type": "factoid",
            "ground_truth": "Adam 优化器、学习率 1e-4、cosine decay",
            "excerpts": ["Adam optimizer with learning rate", "cosine decay schedule"],
        },
    ]}, ensure_ascii=False)


def _assert_schema_compatible(items):
    """审稿标记之外的字段必须与种子集 schema 完全一致。"""
    assert items, "应至少产出一条候选"
    for it in items:
        assert it["source"] == "llm_generated"
        assert it["reviewed"] is False
        assert it["has_answer"] is True
    validate_dataset([generate_qa.normalize_for_validation(it) for it in items])


# ---------------------------------------------------------------------------
# parse_llm_json
# ---------------------------------------------------------------------------

def test_parse_plain_json():
    payload = generate_qa.parse_llm_json('{"items": [{"a": 1}]}')
    assert payload == {"items": [{"a": 1}]}


def test_parse_fenced_json():
    payload = generate_qa.parse_llm_json(
        '这是结果：\n```json\n{"items": []}\n```\n以上。')
    assert payload == {"items": []}


def test_parse_top_level_list():
    payload = generate_qa.parse_llm_json('[{"question": "q"}]')
    assert payload == [{"question": "q"}]


def test_parse_truncated_json_raises():
    """半截 JSON 必须报错（调用方重试），绝不能静默通过。"""
    with pytest.raises(ValueError):
        generate_qa.parse_llm_json('{"items": [{"question": " incomplete')


def test_parse_garbage_raises():
    with pytest.raises(ValueError):
        generate_qa.parse_llm_json("完全不是 JSON 的回答")


def test_parse_llm_error_string_raises():
    """llm_service 失败时返回的错误串应触发解析失败 -> 走重试。"""
    with pytest.raises(ValueError):
        generate_qa.parse_llm_json("[调用 LLM 出错: Kimi API 响应超时]")


# ---------------------------------------------------------------------------
# generate_for_paper
# ---------------------------------------------------------------------------

def test_generate_for_paper_happy_path():
    calls = []

    def fake_llm(messages):
        calls.append(messages)
        return _valid_payload()

    items, error = generate_qa.generate_for_paper(
        _fake_paper(), _fake_chunks(), per_paper=2, call_llm=fake_llm)
    assert error == ""
    assert len(items) == 2
    assert len(calls) == 1
    assert [it["qa_id"] for it in items] == ["gen-p04-001", "gen-p04-002"]
    # 摘录逐字命中原文，被保留进 keywords 定位
    assert items[0]["relevant_chunks"] == [
        {"paper_id": 4, "keywords": ["achieves 0.912 AUC",
                                     "on the CAMELYON16 dataset"]}]
    _assert_schema_compatible(items)


def test_generate_for_paper_retries_on_bad_json():
    """第一次返回垃圾，第二次返回合法 JSON -> 成功且恰好调用 2 次。"""
    responses = iter(["not json at all", _valid_payload()])
    calls = []

    def fake_llm(messages):
        calls.append(messages)
        return next(responses)

    items, error = generate_qa.generate_for_paper(
        _fake_paper(), _fake_chunks(), per_paper=2, call_llm=fake_llm)
    assert error == ""
    assert len(items) == 2
    assert len(calls) == 2


def test_generate_for_paper_all_attempts_fail():
    """持续返回垃圾 -> 返回空列表与错误信息，不抛异常。"""
    calls = []

    def fake_llm(messages):
        calls.append(messages)
        return "garbage"

    items, error = generate_qa.generate_for_paper(
        _fake_paper(), _fake_chunks(), max_attempts=3, call_llm=fake_llm)
    assert items == []
    assert "第 3 次" in error
    assert len(calls) == 3


def test_generate_for_paper_llm_exception_retries():
    """LLM 调用抛异常同样触发重试，全部失败则降级为空。"""
    def fake_llm(messages):
        raise RuntimeError("boom")

    items, error = generate_qa.generate_for_paper(
        _fake_paper(), _fake_chunks(), max_attempts=2, call_llm=fake_llm)
    assert items == []
    assert "boom" in error


def test_unverified_excerpts_dropped():
    """摘录不在原文中的条目被整条过滤；合法条目保留。"""
    payload = json.dumps({"items": [
        {
            "question": " hallucinated 题？",
            "question_type": "factoid",
            "ground_truth": "编造的要点",
            "excerpts": ["this excerpt does not exist anywhere"],
        },
        {
            "question": "PAMIL 的核心模块是什么？",
            "question_type": "method_detail",
            "ground_truth": "prototype attention 模块、聚合实例特征",
            "excerpts": ["prototype attention module aggregates"],
        },
    ]}, ensure_ascii=False)

    items, error = generate_qa.generate_for_paper(
        _fake_paper(), _fake_chunks(), call_llm=lambda m: payload)
    assert error == ""
    assert len(items) == 1
    assert items[0]["question_type"] == "method_detail"
    _assert_schema_compatible(items)


def test_out_of_scope_type_filtered():
    """LLM 不允许生成负例：out_of_scope 条目被过滤。"""
    payload = json.dumps({"items": [
        {
            "question": "本文在 TCGA 上表现如何？",
            "question_type": "out_of_scope",
            "ground_truth": "未报告",
            "excerpts": ["achieves 0.912 AUC"],
        },
        {
            "question": "PAMIL 在 CAMELYON16 上的 AUC 是多少？",
            "question_type": "experiment_data",
            "ground_truth": "0.912 AUC",
            "excerpts": ["achieves 0.912 AUC"],
        },
    ]}, ensure_ascii=False)

    items, _ = generate_qa.generate_for_paper(
        _fake_paper(), _fake_chunks(), call_llm=lambda m: payload)
    assert len(items) == 1
    assert items[0]["question_type"] == "experiment_data"


def test_all_items_filtered_triggers_retry_then_fail():
    """条目全部被过滤时视为失败并重试，最终降级为空（不写半截结果）。"""
    payload = json.dumps({"items": [
        {
            "question": "q",
            "question_type": "factoid",
            "ground_truth": "g",
            "excerpts": ["not in corpus at all"],
        }
    ]})
    calls = []

    def fake_llm(messages):
        calls.append(messages)
        return payload

    items, error = generate_qa.generate_for_paper(
        _fake_paper(), _fake_chunks(), max_attempts=2, call_llm=fake_llm)
    assert items == []
    assert "全部被过滤" in error
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# generate_cross_paper
# ---------------------------------------------------------------------------

TEXT_B = (
    "MiCo adopts context-aware clustering for whole slide images and reaches "
    "0.895 AUC on CAMELYON16 without prototype learning."
)


def test_cross_paper_requires_two_locatable_papers():
    payload = json.dumps({"items": [
        {
            "question": "PAMIL 与 MiCo 的技术路线有何差异？",
            "question_type": "comparison",
            "ground_truth": "PAMIL 用原型注意力、MiCo 用上下文聚类",
            "locators": [
                {"paper_id": 4, "excerpts": ["prototype attention-based multiple"]},
                {"paper_id": 10, "excerpts": ["context-aware clustering for whole"]},
            ],
        },
        {
            # 第二篇摘录未命中 -> 整条被丢弃
            "question": "坏题？",
            "question_type": "comparison",
            "ground_truth": "g",
            "locators": [
                {"paper_id": 4, "excerpts": ["prototype attention-based multiple"]},
                {"paper_id": 10, "excerpts": ["nonexistent excerpt here"]},
            ],
        },
    ]}, ensure_ascii=False)

    pairs = [(_fake_paper(4, "PAMIL"), _fake_chunks(4, PAPER_TEXT)),
             (_fake_paper(10, "MiCo"), _fake_chunks(10, TEXT_B))]
    items, error = generate_qa.generate_cross_paper(pairs, call_llm=lambda m: payload)
    assert error == ""
    assert len(items) == 1
    assert items[0]["question_type"] == "comparison"
    assert len(items[0]["relevant_chunks"]) == 2
    _assert_schema_compatible(items)


def test_cross_paper_non_comparison_filtered():
    payload = json.dumps({"items": [
        {
            "question": "q",
            "question_type": "factoid",  # 跨论文只接受 comparison
            "ground_truth": "g",
            "locators": [
                {"paper_id": 4, "excerpts": ["prototype attention-based multiple"]},
                {"paper_id": 10, "excerpts": ["context-aware clustering for whole"]},
            ],
        }
    ]})
    pairs = [(_fake_paper(4, "PAMIL"), _fake_chunks(4, PAPER_TEXT)),
             (_fake_paper(10, "MiCo"), _fake_chunks(10, TEXT_B))]
    items, error = generate_qa.generate_cross_paper(
        pairs, max_attempts=1, call_llm=lambda m: payload)
    assert items == []
    assert "全部被过滤" in error


# ---------------------------------------------------------------------------
# generate_all（内存库端到端，不写真实候选集文件）
# ---------------------------------------------------------------------------

def test_generate_all_end_to_end(db, tmp_path):
    paper = Paper(id=4, title="PAMIL", abstract=None,
                  file_path="papers/p4.pdf", filename="p4.pdf", processed="done")
    db.add(paper)
    db.add(Chunk(paper_id=4, chunk_index=0, content=PAPER_TEXT))
    db.commit()

    cross_payload = json.dumps({"items": [
        {
            "question": "q cross",
            "question_type": "comparison",
            "ground_truth": "g",
            "locators": [
                {"paper_id": 4, "excerpts": ["prototype attention-based multiple"]},
                {"paper_id": 5, "excerpts": ["whatever"]},
            ],
        }
    ]})
    # 只有 id=4 一篇 -> 跨论文题不生成； responses 只喂单篇生成
    out = tmp_path / "qa_candidates.jsonl"
    summary = generate_qa.generate_all(
        db, paper_ids=[4, 999], per_paper=2, output_path=out,
        include_cross=True, call_llm=lambda m: _valid_payload())

    assert summary["total"] == 2
    assert summary["n_ok"] == 1
    assert summary["n_fail"] == 0  # id=999 不存在，跳过而非失败
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    items = [json.loads(line) for line in lines]  # 每行都是完整合法 JSON
    _assert_schema_compatible(items)


def test_generate_all_single_failure_does_not_block(db, tmp_path):
    """一篇失败不阻塞后续论文。"""
    for pid, text in ((4, PAPER_TEXT), (5, TEXT_B)):
        db.add(Paper(id=pid, title=f"P{pid}", abstract=None,
                     file_path=f"p{pid}.pdf", filename=f"p{pid}.pdf",
                     processed="done"))
        db.add(Chunk(paper_id=pid, chunk_index=0, content=text))
    db.commit()

    def fake_llm(messages):
        # 素材含 PAPER_TEXT 的是 id=4 -> 返回垃圾模拟失败
        if "PAMIL" in messages[-1]["content"] and "paper_id=" not in messages[-1]["content"]:
            return "garbage"
        if "paper_id=" in messages[-1]["content"]:
            return json.dumps({"items": []})  # 跨论文空
        return json.dumps({"items": [
            {
                "question": "MiCo 的 AUC？",
                "question_type": "experiment_data",
                "ground_truth": "0.895 AUC",
                "excerpts": ["reaches 0.895 AUC"],
            }
        ]}, ensure_ascii=False)

    out = tmp_path / "out.jsonl"
    summary = generate_qa.generate_all(
        db, paper_ids=[4, 5], per_paper=2, output_path=out,
        include_cross=False, max_attempts=2, call_llm=fake_llm)

    assert summary["n_ok"] == 1
    assert summary["n_fail"] == 1
    assert summary["total"] == 1
    fail = [s for s in summary["per_paper"] if not s["ok"]]
    assert fail[0]["paper_id"] == 4


class TestResume:
    """--resume 断点续跑契约（该功能先于测试实现，按宪法第 5 条补救特征化测试）。"""

    @staticmethod
    def _seed_two_papers(db):
        for pid in (4, 5):
            db.add(Paper(id=pid, title=f"P{pid}", abstract=None,
                         file_path=f"p{pid}.pdf", filename=f"p{pid}.pdf",
                         processed="done"))
            db.add(Chunk(paper_id=pid, chunk_index=0, content=PAPER_TEXT))
        db.commit()

    @staticmethod
    def _existing_line(pid: int) -> str:
        return json.dumps({
            "qa_id": f"gen-p{pid:02d}-001", "question": "旧题", "ground_truth": "g",
            "relevant_chunks": [{"paper_id": pid, "keywords": ["x"]}],
            "question_type": "factoid", "source": "llm_generated",
            "has_answer": True, "reviewed": False,
        }, ensure_ascii=False)

    def test_skips_completed_papers_and_appends(self, db, tmp_path):
        """已完成论文跳过生成；旧行原样保留在文件头部（追加模式）。"""
        self._seed_two_papers(db)
        out = tmp_path / "out.jsonl"
        out.write_text(self._existing_line(4) + "\n", encoding="utf-8")

        summary = generate_qa.generate_all(
            db, paper_ids=[4, 5], per_paper=2, output_path=out,
            include_cross=False, call_llm=lambda m: _valid_payload(), resume=True)

        lines = out.read_text(encoding="utf-8").strip().splitlines()
        assert lines[0] == self._existing_line(4)
        new_items = [json.loads(l) for l in lines[1:]]
        assert all(it["qa_id"].startswith("gen-p05-") for it in new_items)
        assert summary["total"] == len(new_items) == 2

    def test_skips_completed_cross(self, db, tmp_path):
        """已有 comparison 条目时跨论文题不再生成（qa_id 为 gen-cross- 前缀）。"""
        self._seed_two_papers(db)
        out = tmp_path / "out.jsonl"
        cross_line = json.dumps({
            "qa_id": "gen-cross-001", "question": "旧跨论文题", "ground_truth": "g",
            "relevant_chunks": [{"paper_id": 4, "keywords": ["x"]},
                                {"paper_id": 5, "keywords": ["y"]}],
            "question_type": "comparison", "source": "llm_generated",
            "has_answer": True, "reviewed": False,
        }, ensure_ascii=False)
        out.write_text(cross_line + "\n", encoding="utf-8")

        def spy_llm(messages):
            if "paper_id=" in messages[-1]["content"]:  # 跨论文 prompt 特征
                raise AssertionError("跨论文题已完成，不应再次调用 LLM")
            return _valid_payload()

        summary = generate_qa.generate_all(
            db, paper_ids=[4, 5], per_paper=1, output_path=out,
            include_cross=True, call_llm=spy_llm, resume=True)

        lines = out.read_text(encoding="utf-8").strip().splitlines()
        assert lines[0] == cross_line
        assert summary["type_counts"].get("comparison", 0) == 0

    def test_all_done_produces_zero_and_no_llm_call(self, db, tmp_path):
        """全部完成时 total=0、不调用 LLM、文件不被截断。"""
        self._seed_two_papers(db)
        out = tmp_path / "out.jsonl"
        out.write_text(
            self._existing_line(4) + "\n" + self._existing_line(5) + "\n",
            encoding="utf-8")

        calls = []
        summary = generate_qa.generate_all(
            db, paper_ids=[4, 5], per_paper=2, output_path=out,
            include_cross=False,
            call_llm=lambda m: calls.append(1) or _valid_payload(),
            resume=True)

        assert summary["total"] == 0
        assert summary["n_ok"] == 0
        assert not calls
        assert len(out.read_text(encoding="utf-8").strip().splitlines()) == 2

    def test_no_resume_overwrites(self, db, tmp_path):
        """不带 resume 时整文件重写（旧行被覆盖）。"""
        self._seed_two_papers(db)
        out = tmp_path / "out.jsonl"
        out.write_text(self._existing_line(4) + "\n", encoding="utf-8")

        generate_qa.generate_all(
            db, paper_ids=[4], per_paper=2, output_path=out,
            include_cross=False, call_llm=lambda m: _valid_payload(), resume=False)

        questions = [json.loads(l)["question"]
                     for l in out.read_text(encoding="utf-8").strip().splitlines()]
        assert "旧题" not in questions
        assert len(questions) == 2
