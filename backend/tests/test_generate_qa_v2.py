"""eval.generate_qa_v2 单元测试（Batch 22L T2）。

全部通过注入 fake call_llm / page_loader，绝不触发真实 Kimi API 与真实 PDF 解析。
覆盖：
- validate_evidence_quote：指定页 0 次/≥2 次拒绝、跨页不唯一拒绝、长度 10-200 边界、
  页码非法拒绝；
- validate_candidate：paper_uid / split 归属一致性；
- load_frozen_splits：冻结制品 schema 校验；
- map_uids_to_papers：DOI / sha256 双路稳定 UID 映射、缺失计数；
- build_items_from_payload：schema 构造与逐条过滤计数；
- generate_for_paper：JSON 失败重试与降级；
- generate_all：mock LLM 端到端 3 条/篇、O_EXCL 排他创建、--resume 断点续跑、
  --limit、失败不阻塞、缺失论文计数。
"""

import json
import os
import stat

import pytest

from app.models import Paper
from eval import generate_qa_v2
from eval.dataset import validate_dataset

# ---------------------------------------------------------------------------
# 公共测试数据
# ---------------------------------------------------------------------------

PAGE1_TEXT = (
    "We propose PAMIL, a prototype attention multiple instance learning framework "
    "for whole slide image classification. The prototype attention module aggregates "
    "instance features into bag-level representations with learnable prototypes."
)
PAGE2_TEXT = (
    "PAMIL achieves 0.912 AUC on the CAMELYON16 dataset, outperforming AttentionMIL "
    "which achieves 0.872 AUC. We use Adam optimizer with learning rate 1e-4."
)
PAGE3_TEXT = (
    "In conclusion, PAMIL improves interpretability and accuracy for computational "
    "pathology. Future work includes extension to multi-cohort validation studies."
)

QUOTE_FACTOID = "PAMIL achieves 0.912 AUC on the CAMELYON16 dataset"      # 第 2 页
QUOTE_METHOD = "The prototype attention module aggregates"                # 第 1 页
QUOTE_SUMMARY = "PAMIL improves interpretability and accuracy"            # 第 3 页


def _pages():
    return [
        {"page_number": 1, "text": PAGE1_TEXT},
        {"page_number": 2, "text": PAGE2_TEXT},
        {"page_number": 3, "text": PAGE3_TEXT},
    ]


def _page_texts():
    return [p["text"] for p in _pages()]


def _valid_payload() -> str:
    """三条合法 LLM 输出：factoid/method_detail/summary，证据均逐字唯一命中指定页。"""
    return json.dumps({"items": [
        {
            "question": "PAMIL 在 CAMELYON16 上的 AUC 是多少？",
            "question_type": "factoid",
            "answer": "0.912 AUC、优于 AttentionMIL 的 0.872",
            "evidence_quote": QUOTE_FACTOID,
            "evidence_page": 2,
        },
        {
            "question": "PAMIL 的核心模块是什么？",
            "question_type": "method_detail",
            "answer": "prototype attention 模块、聚合实例特征",
            "evidence_quote": QUOTE_METHOD,
            "evidence_page": 1,
        },
        {
            "question": "PAMIL 的结论与意义是什么？",
            "question_type": "summary",
            "answer": "提升可解释性与准确性、面向计算病理",
            "evidence_quote": QUOTE_SUMMARY,
            "evidence_page": 3,
        },
    ]}, ensure_ascii=False)


def _write_splits(path, rows):
    artifact = {
        "split_schema": "private-benchmark-v2-paper-splits-v1",
        "assignments": rows,
        "paper_counts": {},
    }
    path.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
    return path


def _two_paper_splits(tmp_path):
    return _write_splits(tmp_path / "splits.json", [
        {"paper_uid": "doi:10.1/alpha", "pdf_sha256": "a" * 64, "split": "train"},
        {"paper_uid": "doi:10.1/beta", "pdf_sha256": "b" * 64, "split": "dev"},
    ])


def _seed_two_doi_papers(db):
    for pid, doi in ((1, "10.1/alpha"), (2, "10.1/beta")):
        db.add(Paper(id=pid, title=f"Paper {pid}", abstract=None, doi=doi,
                     file_path=f"papers/p{pid}.pdf", filename=f"p{pid}.pdf",
                     processed="done"))
    db.commit()


def _assert_candidate_schema(item):
    """候选条目：README schema 字段 + v2 附加字段齐备。"""
    for field in ("qa_id", "question", "ground_truth", "question_type",
                  "source", "has_answer"):
        assert field in item, f"缺少 schema 字段 {field}"
    assert item["source"] == "llm_generated"
    assert item["has_answer"] is True
    assert item["reviewed"] is False
    assert item["split"] in {"train", "dev", "holdout"}
    assert item["paper_uid"].startswith(("doi:", "sha256:"))
    assert item["evidence_quote"] == item["relevant_evidence"][0]["quote"]
    assert item["relevant_evidence"][0]["paper_uid"] == item["paper_uid"]
    assert isinstance(item["evidence_page"], int) and item["evidence_page"] >= 1


# ---------------------------------------------------------------------------
# validate_evidence_quote：证据唯一校验器
# ---------------------------------------------------------------------------

class TestValidateEvidenceQuote:
    def test_happy_path_returns_stripped_quote(self):
        quote = generate_qa_v2.validate_evidence_quote(
            "  " + QUOTE_METHOD + "  ", _page_texts(), 1)
        assert quote == QUOTE_METHOD

    def test_zero_occurrence_rejected(self):
        with pytest.raises(ValueError, match="未命中"):
            generate_qa_v2.validate_evidence_quote(
                "this sentence does not exist", _page_texts(), 1)

    def test_two_occurrences_on_same_page_rejected(self):
        pages = ["abc " + QUOTE_METHOD + " def " + QUOTE_METHOD + " ghi"]
        with pytest.raises(ValueError, match="多次"):
            generate_qa_v2.validate_evidence_quote(QUOTE_METHOD, pages, 1)

    def test_cross_page_contamination_rejected(self):
        """跨页不串：quote 同时出现在其他页时，即使指定页唯一也拒绝。"""
        pages = [PAGE1_TEXT, PAGE2_TEXT + " " + QUOTE_METHOD]
        with pytest.raises(ValueError, match="跨页"):
            generate_qa_v2.validate_evidence_quote(QUOTE_METHOD, pages, 1)

    def test_quote_on_other_page_only_rejected(self):
        """quote 只在第 2 页而声明第 1 页 -> 指定页 0 次命中，拒绝。"""
        with pytest.raises(ValueError, match="未命中"):
            generate_qa_v2.validate_evidence_quote(QUOTE_FACTOID, _page_texts(), 1)

    def test_length_lower_bound(self):
        with pytest.raises(ValueError, match="长度"):
            generate_qa_v2.validate_evidence_quote("x" * 9, _page_texts(), 1)
        pages = ["y" * 5 + "x" * 10 + "y" * 5]
        assert generate_qa_v2.validate_evidence_quote("x" * 10, pages, 1) == "x" * 10

    def test_length_upper_bound(self):
        long_quote = "z" * 201
        pages = [long_quote]
        with pytest.raises(ValueError, match="长度"):
            generate_qa_v2.validate_evidence_quote(long_quote, pages, 1)
        ok_quote = "z" * 200
        assert generate_qa_v2.validate_evidence_quote(ok_quote, [ok_quote], 1) == ok_quote

    @pytest.mark.parametrize("bad_page", [0, -1, 4, "1", 1.5, True, None])
    def test_invalid_page_rejected(self, bad_page):
        with pytest.raises(ValueError, match="页"):
            generate_qa_v2.validate_evidence_quote(QUOTE_METHOD, _page_texts(), bad_page)

    def test_non_string_quote_rejected(self):
        with pytest.raises(ValueError):
            generate_qa_v2.validate_evidence_quote(None, _page_texts(), 1)


# ---------------------------------------------------------------------------
# validate_candidate：paper 归属与 split 一致性
# ---------------------------------------------------------------------------

class TestValidateCandidate:
    def _item(self):
        return {"paper_uid": "doi:10.1/alpha", "split": "train"}

    def test_consistent_passes(self):
        assert generate_qa_v2.validate_candidate(
            self._item(), expected_uid="doi:10.1/alpha", expected_split="train") is None

    def test_paper_uid_mismatch_rejected(self):
        with pytest.raises(ValueError, match="归属"):
            generate_qa_v2.validate_candidate(
                self._item(), expected_uid="doi:10.1/beta", expected_split="train")

    def test_split_mismatch_rejected(self):
        with pytest.raises(ValueError, match="split"):
            generate_qa_v2.validate_candidate(
                self._item(), expected_uid="doi:10.1/alpha", expected_split="dev")


# ---------------------------------------------------------------------------
# load_frozen_splits：冻结制品读取
# ---------------------------------------------------------------------------

class TestLoadFrozenSplits:
    def test_happy_path(self, tmp_path):
        path = _two_paper_splits(tmp_path)
        rows = generate_qa_v2.load_frozen_splits(path)
        assert [r["paper_uid"] for r in rows] == ["doi:10.1/alpha", "doi:10.1/beta"]

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            generate_qa_v2.load_frozen_splits(tmp_path / "nope.json")

    def test_wrong_schema_rejected(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"split_schema": "other", "assignments": []}))
        with pytest.raises(ValueError, match="schema"):
            generate_qa_v2.load_frozen_splits(path)

    def test_invalid_split_value_rejected(self, tmp_path):
        path = _write_splits(tmp_path / "bad.json", [
            {"paper_uid": "doi:10.1/a", "pdf_sha256": "a" * 64, "split": "test"},
        ])
        with pytest.raises(ValueError, match="split"):
            generate_qa_v2.load_frozen_splits(path)


# ---------------------------------------------------------------------------
# map_uids_to_papers：paper_uid -> DB Paper 映射
# ---------------------------------------------------------------------------

class TestMapUidsToPapers:
    def test_doi_mapping_and_missing(self, db, tmp_path):
        _seed_two_doi_papers(db)
        mapping, missing = generate_qa_v2.map_uids_to_papers(
            db, tmp_path, {"doi:10.1/alpha", "doi:10.1/ghost"})
        assert mapping["doi:10.1/alpha"].id == 1
        assert missing == ["doi:10.1/ghost"]

    def test_sha256_mapping(self, db, tmp_path):
        import hashlib
        papers_dir = tmp_path / "papers"
        papers_dir.mkdir()
        payload = b"%PDF-1.7 fake bytes for uid"
        (papers_dir / "p9.pdf").write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        db.add(Paper(id=9, title="NoDoi", abstract=None, doi=None,
                     file_path="papers/p9.pdf", filename="p9.pdf", processed="done"))
        db.commit()
        mapping, missing = generate_qa_v2.map_uids_to_papers(
            db, tmp_path, {f"sha256:{digest}"})
        assert mapping[f"sha256:{digest}"].id == 9
        assert missing == []

    def test_paper_without_source_file_skipped(self, db, tmp_path):
        """无 DOI 且源文件缺失的论文无法构造 UID，跳过而非抛异常。"""
        db.add(Paper(id=5, title="Missing", abstract=None, doi=None,
                     file_path="papers/gone.pdf", filename="gone.pdf",
                     processed="done"))
        db.commit()
        mapping, missing = generate_qa_v2.map_uids_to_papers(
            db, tmp_path, {"doi:10.1/alpha"})
        assert mapping == {}
        assert missing == ["doi:10.1/alpha"]


# ---------------------------------------------------------------------------
# build_items_from_payload：条目构造与逐条过滤
# ---------------------------------------------------------------------------

class TestBuildItemsFromPayload:
    def test_valid_payload_produces_schema_items(self):
        payload = generate_qa_v2.parse_llm_json(_valid_payload())
        items, rejected = generate_qa_v2.build_items_from_payload(
            payload, paper_uid="doi:10.1/alpha", split="train",
            page_texts=_page_texts(), qa_id_prefix="gen2-p01")
        assert rejected == 0
        assert len(items) == 3
        assert [it["question_type"] for it in items] == [
            "factoid", "method_detail", "summary"]
        assert [it["qa_id"] for it in items] == [
            "gen2-p01-001", "gen2-p01-002", "gen2-p01-003"]
        for it in items:
            _assert_candidate_schema(it)
        # 审稿后 schema 与数据集校验器兼容（quote ≥ 20 字符）
        validate_dataset(
            [generate_qa_v2.normalize_for_validation(it) for it in items])

    def test_disallowed_question_type_filtered(self):
        payload = {"items": [{
            "question": "q", "question_type": "comparison",  # v2 只允许三种类型
            "answer": "a", "evidence_quote": QUOTE_METHOD, "evidence_page": 1,
        }]}
        items, rejected = generate_qa_v2.build_items_from_payload(
            payload, paper_uid="doi:10.1/alpha", split="train",
            page_texts=_page_texts(), qa_id_prefix="gen2-p01")
        assert items == [] and rejected == 1

    def test_duplicate_quote_filtered_and_counted(self):
        pages = [PAGE1_TEXT + " " + QUOTE_METHOD, PAGE2_TEXT, PAGE3_TEXT]
        payload = {"items": [{
            "question": "q", "question_type": "factoid",
            "answer": "a", "evidence_quote": QUOTE_METHOD, "evidence_page": 1,
        }]}
        items, rejected = generate_qa_v2.build_items_from_payload(
            payload, paper_uid="doi:10.1/alpha", split="train",
            page_texts=pages, qa_id_prefix="gen2-p01")
        assert items == [] and rejected == 1

    def test_missing_answer_filtered(self):
        payload = {"items": [{
            "question": "q", "question_type": "factoid",
            "answer": "  ", "evidence_quote": QUOTE_METHOD, "evidence_page": 1,
        }]}
        items, rejected = generate_qa_v2.build_items_from_payload(
            payload, paper_uid="doi:10.1/alpha", split="train",
            page_texts=_page_texts(), qa_id_prefix="gen2-p01")
        assert items == [] and rejected == 1

    def test_qa_id_dense_after_rejection(self):
        """被拒条目不占 qa_id 序号（与 Phase A 一致的稠密编号），保留条目编号连续。"""
        payload = {"items": [
            {"question": "bad", "question_type": "factoid", "answer": "a",
             "evidence_quote": "nonexistent quote", "evidence_page": 1},
            {"question": "good", "question_type": "factoid", "answer": "a",
             "evidence_quote": QUOTE_METHOD, "evidence_page": 1},
        ]}
        items, rejected = generate_qa_v2.build_items_from_payload(
            payload, paper_uid="doi:10.1/alpha", split="train",
            page_texts=_page_texts(), qa_id_prefix="gen2-p01")
        assert rejected == 1
        assert [it["qa_id"] for it in items] == ["gen2-p01-001"]


# ---------------------------------------------------------------------------
# generate_for_paper：重试与降级
# ---------------------------------------------------------------------------

class TestGenerateForPaper:
    def _paper(self, pid=1):
        return Paper(id=pid, title="PAMIL", abstract=None,
                     file_path=f"papers/p{pid}.pdf", filename=f"p{pid}.pdf")

    def test_retry_on_bad_json_then_success(self):
        # 逐条生成（Kimi 多条长 JSON 易空响应的实测对策）：
        # 第 1 条首次坏 JSON、重试成功；第 2/3 条各一次成功 → 共 4 次调用
        responses = iter(["not json", _valid_payload(), _valid_payload(), _valid_payload()])
        calls = []
        items, error, rejected = generate_qa_v2.generate_for_paper(
            self._paper(), _pages(), paper_uid="doi:10.1/alpha", split="train",
            call_llm=lambda m: (calls.append(m), next(responses))[1])
        assert error == "" and len(items) == 3 and len(calls) == 4

    def test_all_attempts_fail_degrades_empty(self):
        items, error, rejected = generate_qa_v2.generate_for_paper(
            self._paper(), _pages(), paper_uid="doi:10.1/alpha", split="train",
            max_attempts=2, call_llm=lambda m: "garbage")
        assert items == [] and "第 2 次" in error

    def test_prompt_asks_rotation_and_unique_short_quote(self):
        """逐条生成模式下，每次调用各要求一种轮换类型（三次调用覆盖三型）。"""
        seen = []
        generate_qa_v2.generate_for_paper(
            self._paper(), _pages(), paper_uid="doi:10.1/alpha", split="train",
            call_llm=lambda m: (seen.append(m), _valid_payload())[1])
        assert len(seen) == 3
        for call, qtype in zip(seen, ("factoid", "method_detail", "summary")):
            user = call[-1]["content"]
            assert qtype in user
            assert "逐字" in user and "独特" in user and "evidence_page" in user
            assert "【第 1 页】" in user  # 素材带 1-based 页码标签


# ---------------------------------------------------------------------------
# generate_all：端到端（mock LLM + 注入 page_loader）
# ---------------------------------------------------------------------------

class TestGenerateAll:
    def _run(self, db, tmp_path, **overrides):
        splits = overrides.pop("splits_path", None) or _two_paper_splits(tmp_path)
        out = overrides.pop("output_path", None) or tmp_path / "qa_v2_candidates.jsonl"
        kwargs = dict(
            splits_path=splits, output_path=out, runtime_root=tmp_path,
            call_llm=lambda m: _valid_payload(),
            page_loader=lambda paper: _pages(),
        )
        kwargs.update(overrides)
        return generate_qa_v2.generate_all(db, **kwargs), out

    def test_end_to_end_three_items_per_paper(self, db, tmp_path):
        _seed_two_doi_papers(db)
        summary, out = self._run(db, tmp_path)

        assert summary["total"] == 6
        assert summary["n_ok"] == 2 and summary["n_fail"] == 0
        assert summary["type_counts"] == {
            "factoid": 2, "method_detail": 2, "summary": 2}
        lines = out.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 6
        items = [json.loads(line) for line in lines]
        for it in items:
            _assert_candidate_schema(it)
        # train/dev 归属与冻结制品一致
        assert {it["split"] for it in items if it["paper_uid"] == "doi:10.1/alpha"} == {"train"}
        assert {it["split"] for it in items if it["paper_uid"] == "doi:10.1/beta"} == {"dev"}
        validate_dataset(
            [generate_qa_v2.normalize_for_validation(it) for it in items])
        # 排他创建的私有候选集权限为 0600
        assert stat.S_IMODE(os.stat(out).st_mode) == 0o600

    def test_exclusive_create_refuses_existing_output(self, db, tmp_path):
        _seed_two_doi_papers(db)
        out = tmp_path / "qa_v2_candidates.jsonl"
        out.write_text("existing\n", encoding="utf-8")
        with pytest.raises(FileExistsError, match="resume"):
            self._run(db, tmp_path, output_path=out)

    def test_resume_appends_and_skips_done(self, db, tmp_path):
        _seed_two_doi_papers(db)
        out = tmp_path / "qa_v2_candidates.jsonl"
        done_line = json.dumps({
            "qa_id": "gen2-p01-001", "question": "旧题", "ground_truth": "g",
            "relevant_evidence": [{"paper_uid": "doi:10.1/alpha",
                                   "quote": "x" * 30}],
            "question_type": "factoid", "source": "llm_generated",
            "has_answer": True, "reviewed": False, "split": "train",
            "paper_uid": "doi:10.1/alpha",
            "evidence_quote": "x" * 30, "evidence_page": 1,
        }, ensure_ascii=False)
        out.write_text(done_line + "\n", encoding="utf-8")
        out.chmod(0o600)

        summary, _ = self._run(db, tmp_path, output_path=out, resume=True)

        lines = out.read_text(encoding="utf-8").strip().splitlines()
        assert lines[0] == done_line  # 旧行原样保留
        items = [json.loads(l) for l in lines]
        alpha = [it for it in items if it["paper_uid"] == "doi:10.1/alpha"]
        beta = [it for it in items if it["paper_uid"] == "doi:10.1/beta"]
        assert {it["question_type"] for it in alpha} == {
            "factoid", "method_detail", "summary"
        }
        assert len(alpha) == 3 and len(beta) == 3
        assert len({it["qa_id"] for it in items}) == 6
        assert summary["total"] == 5 and summary["n_skipped"] == 0

    def test_resume_all_done_makes_no_llm_call(self, db, tmp_path):
        _seed_two_doi_papers(db)
        out = tmp_path / "qa_v2_candidates.jsonl"
        lines = ""
        for uid in ("doi:10.1/alpha", "doi:10.1/beta"):
            for qtype in generate_qa_v2.QUESTION_TYPE_ROTATION:
                lines += json.dumps({
                    "qa_id": f"old-{uid}-{qtype}",
                    "paper_uid": uid,
                    "split": "train" if uid.endswith("alpha") else "dev",
                    "question_type": qtype,
                }) + "\n"
        out.write_text(lines, encoding="utf-8")
        out.chmod(0o600)
        calls = []
        summary, _ = self._run(
            db, tmp_path, output_path=out, resume=True,
            call_llm=lambda m: (calls.append(m), _valid_payload())[1])
        assert summary["total"] == 0 and not calls
        assert len(out.read_text(encoding="utf-8").strip().splitlines()) == 6

    def test_resume_rejects_corrupt_jsonl(self, db, tmp_path):
        _seed_two_doi_papers(db)
        out = tmp_path / "qa_v2_candidates.jsonl"
        out.write_text("{not-json}\n", encoding="utf-8")
        out.chmod(0o600)
        with pytest.raises(ValueError, match="JSONL"):
            self._run(db, tmp_path, output_path=out, resume=True)

    def test_resume_rejects_insecure_permissions_and_symlink(self, db, tmp_path):
        _seed_two_doi_papers(db)
        out = tmp_path / "qa_v2_candidates.jsonl"
        out.write_text("{}\n", encoding="utf-8")
        out.chmod(0o644)
        with pytest.raises(PermissionError, match="0600"):
            self._run(db, tmp_path, output_path=out, resume=True)

        target = tmp_path / "target.jsonl"
        target.write_text("{}\n", encoding="utf-8")
        target.chmod(0o600)
        link = tmp_path / "link.jsonl"
        link.symlink_to(target)
        with pytest.raises(ValueError, match="符号链接"):
            self._run(db, tmp_path, output_path=link, resume=True)

    def test_limit_processes_subset(self, db, tmp_path):
        _seed_two_doi_papers(db)
        summary, out = self._run(db, tmp_path, limit=1)
        assert summary["total"] == 3
        assert len(out.read_text(encoding="utf-8").strip().splitlines()) == 3

    def test_single_paper_failure_not_blocking(self, db, tmp_path):
        _seed_two_doi_papers(db)

        def fake_llm(messages):
            if "Paper 1" in messages[-1]["content"]:
                return "garbage"
            return _valid_payload()

        summary, out = self._run(
            db, tmp_path, call_llm=fake_llm, max_attempts=2)
        assert summary["n_ok"] == 1 and summary["n_fail"] == 1
        assert summary["total"] == 3

    def test_missing_paper_counted_not_blocking(self, db, tmp_path):
        _seed_two_doi_papers(db)
        splits = _write_splits(tmp_path / "splits.json", [
            {"paper_uid": "doi:10.1/alpha", "pdf_sha256": "a" * 64, "split": "train"},
            {"paper_uid": "doi:10.1/ghost", "pdf_sha256": "c" * 64, "split": "dev"},
        ])
        summary, _ = self._run(db, tmp_path, splits_path=splits)
        assert summary["n_missing"] == 1
        assert summary["total"] == 3

    def test_rejected_items_counted(self, db, tmp_path):
        _seed_two_doi_papers(db)
        payload = json.dumps({"items": [
            {"question": "q", "question_type": "factoid", "answer": "a",
             "evidence_quote": QUOTE_METHOD, "evidence_page": 1},
            {"question": "bad", "question_type": "factoid", "answer": "a",
             "evidence_quote": "not in any page", "evidence_page": 1},
        ]}, ensure_ascii=False)
        summary, _ = self._run(
            db, tmp_path, call_llm=lambda m: payload)
        # 逐条生成：坏证据条目每次调用都被拒；factoid 槽位 1 次成功（拒 1），
        # method_detail/summary 槽位类型不匹配各重试 3 次（各拒 3）→ 每篇 1+3+3=7，两篇 14
        assert summary["rejected"] == 14
        assert summary["total"] == 2


# ---------------------------------------------------------------------------
# CLI 参数解析
# ---------------------------------------------------------------------------

def test_build_parser_parameters():
    args = generate_qa_v2.build_parser().parse_args([])
    assert args.resume is False and args.limit is None
    assert args.confirm_content_egress is False
    assert args.splits.endswith("benchmark_v2_splits.json")
    assert args.output.endswith("qa_v2_candidates.jsonl")
    args = generate_qa_v2.build_parser().parse_args([
        "--splits", "a.json", "--output", "b.jsonl", "--resume", "--limit", "2",
        "--confirm-content-egress"])
    assert args.splits == "a.json" and args.output == "b.jsonl"
    assert args.resume is True and args.limit == 2
    assert args.confirm_content_egress is True
