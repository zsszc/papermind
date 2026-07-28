"""eval.metrics 单元测试：手算已知答案 + 边界行为。

本测试模块只导入 eval.metrics（纯标准库），
不触发 Embedding 模型加载、不调用 LLM、不连数据库。
"""

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from eval.metrics import (
    citation_coverage,
    contains_refusal,
    keyword_hit_rate,
    mrr,
    ndcg_at_k,
    recall_at_k,
    split_ground_truth_keywords,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# recall_at_k
# ---------------------------------------------------------------------------

class TestRecallAtK:
    """retrieved=[a,b,c,d,e]，relevant=[b,d,x]（3 个期望）的手算用例。"""

    RETRIEVED = ["a", "b", "c", "d", "e"]
    RELEVANT = ["b", "d", "x"]

    def test_full_k(self):
        # 前 5 位含 b、d → 2/3
        assert recall_at_k(self.RETRIEVED, self.RELEVANT, 5) == pytest.approx(2 / 3)

    def test_k2(self):
        # 前 2 位 [a,b] 仅含 b → 1/3
        assert recall_at_k(self.RETRIEVED, self.RELEVANT, 2) == pytest.approx(1 / 3)

    def test_k1_miss(self):
        # 前 1 位 [a] 无命中 → 0
        assert recall_at_k(self.RETRIEVED, self.RELEVANT, 1) == 0.0

    def test_k_larger_than_list(self):
        # k 超过检索列表长度时按实际长度截断 → 2/3
        assert recall_at_k(self.RETRIEVED, self.RELEVANT, 100) == pytest.approx(2 / 3)

    def test_empty_relevant(self):
        assert recall_at_k(self.RETRIEVED, [], 5) == 0.0

    def test_empty_retrieved(self):
        assert recall_at_k([], self.RELEVANT, 5) == 0.0

    def test_zero_and_negative_k(self):
        assert recall_at_k(self.RETRIEVED, self.RELEVANT, 0) == 0.0
        assert recall_at_k(self.RETRIEVED, self.RELEVANT, -1) == 0.0


# ---------------------------------------------------------------------------
# mrr
# ---------------------------------------------------------------------------

class TestMRR:
    RETRIEVED = ["a", "b", "c"]

    def test_hit_at_rank2(self):
        assert mrr(self.RETRIEVED, ["b"]) == pytest.approx(1 / 2)

    def test_first_hit_wins(self):
        # 期望 {b,c}，b 在第 2 位先命中 → 1/2
        assert mrr(self.RETRIEVED, ["c", "b"]) == pytest.approx(1 / 2)

    def test_hit_at_rank1(self):
        assert mrr(self.RETRIEVED, ["a"]) == 1.0

    def test_no_hit(self):
        assert mrr(self.RETRIEVED, ["x"]) == 0.0

    def test_empty(self):
        assert mrr([], ["a"]) == 0.0
        assert mrr(self.RETRIEVED, []) == 0.0
        assert mrr([], []) == 0.0


# ---------------------------------------------------------------------------
# ndcg_at_k
# ---------------------------------------------------------------------------

class TestNDCGAtK:
    RETRIEVED = ["a", "b", "c", "d"]
    RELEVANT = ["b", "d"]

    def test_known_value_k4(self):
        # DCG  = 1/log2(3) + 1/log2(5)   （b 在第 2 位，d 在第 4 位）
        # IDCG = 1/log2(2) + 1/log2(3)   （理想排序 b、d 占据前两位）
        expected = (1 / math.log2(3) + 1 / math.log2(5)) / (1 + 1 / math.log2(3))
        assert ndcg_at_k(self.RETRIEVED, self.RELEVANT, 4) == pytest.approx(expected)

    def test_known_value_k2(self):
        # 只看前 2 位 [a,b]：DCG = 1/log2(3)；IDCG 不变（min(2,2)=2 个理想位）
        expected = (1 / math.log2(3)) / (1 + 1 / math.log2(3))
        assert ndcg_at_k(self.RETRIEVED, self.RELEVANT, 2) == pytest.approx(expected)

    def test_perfect_ranking_is_one(self):
        assert ndcg_at_k(["b", "d", "a"], self.RELEVANT, 2) == pytest.approx(1.0)

    def test_all_miss_is_zero(self):
        assert ndcg_at_k(["x", "y"], self.RELEVANT, 2) == 0.0

    def test_empty_and_zero_k(self):
        assert ndcg_at_k([], self.RELEVANT, 5) == 0.0
        assert ndcg_at_k(self.RETRIEVED, [], 5) == 0.0
        assert ndcg_at_k(self.RETRIEVED, self.RELEVANT, 0) == 0.0


# ---------------------------------------------------------------------------
# citation_coverage
# ---------------------------------------------------------------------------

class TestCitationCoverage:
    def test_known_value(self):
        # 引用 3 个，其中 p1_c2 命中期望 {p1_c2, p1_c3} → 1/2
        cov = citation_coverage(["p1_c1", "p1_c2", "p9_c9"], ["p1_c2", "p1_c3"])
        assert cov == pytest.approx(1 / 2)

    def test_full_coverage(self):
        assert citation_coverage(["p1_c2", "p1_c3"], ["p1_c2", "p1_c3"]) == 1.0

    def test_empty_relevant(self):
        assert citation_coverage(["p1_c2"], []) == 0.0

    def test_empty_citations(self):
        assert citation_coverage([], ["p1_c2"]) == 0.0

    def test_duplicate_citations_dedup(self):
        # 重复引用去重后仍只算一次命中
        assert citation_coverage(["p1_c2", "p1_c2"], ["p1_c2", "p1_c3"]) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# keyword_hit_rate / split_ground_truth_keywords / contains_refusal
# ---------------------------------------------------------------------------

class TestKeywordHitRate:
    GT = "肿瘤、背景抑制；BiGRU"  # 切分为 [肿瘤, 背景抑制, BiGRU]

    def test_known_value(self):
        # 答案只含 BiGRU（"抑制背景" 不等于要点 "背景抑制"）→ 1/3
        assert keyword_hit_rate("该方法用 BiGRU 抑制背景", self.GT) == pytest.approx(1 / 3)

    def test_ascii_case_insensitive(self):
        assert keyword_hit_rate("用了 bigru 网络", self.GT) == pytest.approx(1 / 3)

    def test_full_hit(self):
        answer = "肿瘤区域通过背景抑制与 BiGRU 聚合"
        assert keyword_hit_rate(answer, self.GT) == 1.0

    def test_list_input(self):
        # 支持直接传入预切分的要点列表
        assert keyword_hit_rate("含 BiGRU", ["肿瘤", "BiGRU"]) == pytest.approx(0.5)

    def test_empty(self):
        assert keyword_hit_rate("", self.GT) == 0.0
        assert keyword_hit_rate("任意答案", "") == 0.0
        assert keyword_hit_rate("任意答案", []) == 0.0

    def test_split_mixed_punctuation(self):
        # 中英文顿号/逗号/分号混合切分，空项被剔除
        assert split_ground_truth_keywords("a、b，c;d；e、") == ["a", "b", "c", "d", "e"]
        assert split_ground_truth_keywords("") == []
        assert split_ground_truth_keywords([" x ", "", "y"]) == ["x", "y"]


class TestContainsRefusal:
    def test_refusal_detected(self):
        assert contains_refusal("抱歉，根据现有资料我不知道答案。")
        assert contains_refusal("资料中没有相关信息，无法回答。")

    def test_normal_answer(self):
        assert not contains_refusal("ReCo-MIL 包含三个阶段。")

    def test_empty(self):
        assert not contains_refusal("")


# ---------------------------------------------------------------------------
# 模块纯净性：导入 eval.metrics 不得拉入模型 / LLM / app 依赖
# ---------------------------------------------------------------------------

def test_metrics_module_is_pure_stdlib():
    """在干净子进程中导入 eval.metrics，断言不加载重量级模块。"""
    code = (
        "import sys, json; import eval.metrics; "
        "print(json.dumps(sorted(m for m in sys.modules "
        "if m.split('.')[0] in {'chromadb', 'sentence_transformers', 'torch', "
        "'openai', 'app'})))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND_DIR, capture_output=True, text=True, timeout=60, check=True,
    )
    assert json.loads(out.stdout) == []
