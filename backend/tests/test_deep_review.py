"""Deep Review 服务（Phase F F1）单元测试。

覆盖 spec 3.1 三段行为契约：
- plan：LLM 拆 3-5 个子问题（硬上限 5），解析失败 / LLM 出错抛 DeepReviewError；
- execute：子问题独立检索 + LLM 生成，本地引用传递；单点失败降级为
  「该子问题检索不足」标记，绝不抛出、不阻塞其他子问题；
- synthesize：汇总结构化综述（引言/分节/结论），各子答案局部 [^n^] 引用
  全局重编号后送入 LLM，Review.citations 为全局引用表。
全程 mock llm_service 与向量库，不发起真实 LLM / embedding 调用。
"""

import pytest

from app.models import Chunk, Paper
from app.services import deep_review
from app.services.deep_review import (
    INSUFFICIENT_NOTICE,
    DeepReviewError,
    SubAnswer,
    SubQuestion,
)


class _FakeStore:
    """假向量库：available 可控，search 返回固定片段或抛异常，记录调用参数。"""

    def __init__(self, chunks=None, available=True, exc=None):
        self._chunks = chunks or []
        self._available = available
        self._exc = exc
        self.search_calls = []

    def available(self):
        return self._available

    def search(self, query, top_k, filters):
        self.search_calls.append({"query": query, "top_k": top_k, "filters": filters})
        if self._exc:
            raise self._exc
        return self._chunks


class _FakeLLM:
    """假 LLM：按队列依次返回固定响应，记录每次收到的 messages 与 json_mode。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.json_modes = []

    async def chat_completion(
        self, messages, json_mode=False, timeout=None, trace_metadata=None
    ):
        self.calls.append(messages)
        self.json_modes.append(json_mode)
        return self._responses.pop(0) if self._responses else ""


def _patch_deps(monkeypatch, store=None, llm=None):
    """替换 deep_review 的向量库与 LLM 单例为假实现。"""
    if store is not None:
        monkeypatch.setattr(deep_review, "get_vector_store", lambda: store)
    if llm is not None:
        monkeypatch.setattr(deep_review, "llm_service", llm)


def _chunk(cid, title="结直肠癌T分期研究"):
    """一条带完整引用字段的检索片段。"""
    return {
        "chunk_id": cid,
        "paper_id": 1,
        "title": title,
        "authors": "张三",
        "year": 2024,
        "page_number": 3,
        "content": f"片段{ cid }内容：多实例学习在病理图像上的应用……",
    }


class TestPlan:
    """plan(topic, n_papers?) -> List[SubQuestion]：LLM 拆 3-5 子问题，硬上限 5。"""

    @pytest.mark.asyncio
    async def test_parses_questions_from_json(self, monkeypatch):
        """mock LLM 返回 4 个子问题 JSON → 解析为 1-based 有序 SubQuestion 列表。"""
        llm = _FakeLLM(['{"questions": ["子问题甲", "子问题乙", "子问题丙", "子问题丁"]}'])
        _patch_deps(monkeypatch, llm=llm)

        questions = await deep_review.plan("多实例学习综述")

        assert [q.index for q in questions] == [1, 2, 3, 4]
        assert [q.question for q in questions] == [
            "子问题甲", "子问题乙", "子问题丙", "子问题丁",
        ]
        # plan 走 json_mode 约束输出（解析失败的兜底依赖结构化输出）
        assert llm.json_modes == [True]
        # topic 必须进入发给 LLM 的消息
        assert any("多实例学习综述" in m.get("content", "") for m in llm.calls[0])

    @pytest.mark.asyncio
    async def test_caps_at_five(self, monkeypatch):
        """LLM 返回 7 个子问题 → 硬上限截断为 5（spec §7 长任务耗时控制）。"""
        llm = _FakeLLM(['{"questions": ["q1","q2","q3","q4","q5","q6","q7"]}'])
        _patch_deps(monkeypatch, llm=llm)

        questions = await deep_review.plan("主题")

        assert len(questions) == 5
        assert [q.question for q in questions] == ["q1", "q2", "q3", "q4", "q5"]

    @pytest.mark.asyncio
    async def test_accepts_bare_json_list(self, monkeypatch):
        """LLM 直接返回 JSON 数组（无 questions 键）也能解析。"""
        llm = _FakeLLM(['["甲", "乙", "丙"]'])
        _patch_deps(monkeypatch, llm=llm)

        questions = await deep_review.plan("主题")

        assert [q.question for q in questions] == ["甲", "乙", "丙"]

    @pytest.mark.asyncio
    async def test_llm_error_raises(self, monkeypatch):
        """LLM 带内错误串（[调用 LLM 出错: ...]）→ plan 抛 DeepReviewError。"""
        llm = _FakeLLM(["[调用 LLM 出错: Kimi API 响应超时，请稍后重试。]"])
        _patch_deps(monkeypatch, llm=llm)

        with pytest.raises(DeepReviewError):
            await deep_review.plan("主题")

    @pytest.mark.asyncio
    async def test_invalid_json_raises(self, monkeypatch):
        """LLM 返回非 JSON 文本 → plan 抛 DeepReviewError。"""
        llm = _FakeLLM(["我觉得可以分成这几个方面来讨论"])
        _patch_deps(monkeypatch, llm=llm)

        with pytest.raises(DeepReviewError):
            await deep_review.plan("主题")

    @pytest.mark.asyncio
    async def test_empty_questions_raises(self, monkeypatch):
        """LLM 返回空子问题列表 → plan 抛 DeepReviewError（无法继续执行）。"""
        llm = _FakeLLM(['{"questions": []}'])
        _patch_deps(monkeypatch, llm=llm)

        with pytest.raises(DeepReviewError):
            await deep_review.plan("主题")


class TestExecute:
    """execute(sub_question) -> SubAnswer：检索 + 生成，带本地引用；失败降级不抛出。"""

    @pytest.mark.asyncio
    async def test_answer_and_citation_passthrough(self, db, monkeypatch):
        """LLM 答案原文返回（含 [^n^] 标记），检索片段原样挂在 SubAnswer.chunks。"""
        chunks = [_chunk("p1_c0"), _chunk("p1_c1")]
        store, llm = _FakeStore(chunks=chunks), _FakeLLM(["答案正文[^1^][^2^]"])
        _patch_deps(monkeypatch, store=store, llm=llm)

        result = await deep_review.execute(
            SubQuestion(index=1, question="MIL 方法有哪些？"), db=db
        )

        assert result.ok is True
        assert result.answer == "答案正文[^1^][^2^]"
        assert result.chunks == chunks          # 本地引用传递（不丢、不换序）
        assert result.question == "MIL 方法有哪些？"
        assert result.error is None

    @pytest.mark.asyncio
    async def test_independent_retrieval_per_subquestion(self, db, monkeypatch):
        """每个子问题走 shared hybrid，语义候选池与主对话一致为 2*top_k。"""
        store = _FakeStore(chunks=[_chunk("p1_c0")])
        llm = _FakeLLM(["答案一[^1^]", "答案二[^1^]"])
        _patch_deps(monkeypatch, store=store, llm=llm)

        await deep_review.execute(SubQuestion(index=1, question="子问题A"), db=db)
        await deep_review.execute(SubQuestion(index=2, question="子问题B"), db=db)

        assert [c["query"] for c in store.search_calls] == ["子问题A", "子问题B"]
        assert all(c["top_k"] == 10 for c in store.search_calls)

    @pytest.mark.asyncio
    async def test_keyword_fallback_still_answers_with_local_evidence(
        self, db, monkeypatch
    ):
        """Embedding 不可用时，共享管线应使用同范围 BM25，而非误报检索不足。"""
        db.add(Paper(
            id=7,
            title="fallback paper",
            filename="fallback.pdf",
            file_path="papers/fallback.pdf",
            year=2024,
        ))
        db.add(Chunk(
            paper_id=7,
            chunk_index=0,
            content="fallbackanchor precise local evidence",
            chunk_type="result",
        ))
        db.commit()
        store = _FakeStore(available=False)
        llm = _FakeLLM(["基于本地证据的答案[^1^]"])
        _patch_deps(monkeypatch, store=store, llm=llm)

        result = await deep_review.execute(
            SubQuestion(index=1, question="fallbackanchor"), db=db
        )

        assert result.ok is True
        assert [item["chunk_id"] for item in result.chunks] == ["p7_c0"]
        assert len(llm.calls) == 1

    @pytest.mark.asyncio
    async def test_prompt_uses_rag_citation_format(self, db, monkeypatch):
        """发给 LLM 的上下文沿用 build_rag_prompt 引用格式（[i] 编号 + [^i^] 标注约定）。"""
        store = _FakeStore(chunks=[_chunk("p1_c0")])
        llm = _FakeLLM(["答案[^1^]"])
        _patch_deps(monkeypatch, store=store, llm=llm)

        await deep_review.execute(SubQuestion(index=1, question="子问题A"), db=db)

        sent = "\n".join(m.get("content", "") for m in llm.calls[0])
        assert "[1] 结直肠癌T分期研究" in sent    # build_rag_prompt 的片段头格式
        assert "[^i^]" in sent or "[^1^]" in sent  # 引用标注约定进入 prompt

    @pytest.mark.asyncio
    async def test_retrieval_exception_degrades(self, db, monkeypatch):
        """检索抛异常 → 不抛出，ok=False，答案为「该子问题检索不足」标记。"""
        store = _FakeStore(exc=RuntimeError("chromadb 连接失败"))
        llm = _FakeLLM(["不应被用到"])
        _patch_deps(monkeypatch, store=store, llm=llm)

        result = await deep_review.execute(
            SubQuestion(index=1, question="子问题A"), db=db
        )

        assert result.ok is False
        assert INSUFFICIENT_NOTICE in result.answer
        assert result.chunks == []
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_llm_error_string_degrades(self, db, monkeypatch):
        """LLM 返回带内错误串 → 同样降级为检索不足标记（不抛出）。"""
        store = _FakeStore(chunks=[_chunk("p1_c0")])
        llm = _FakeLLM(["[调用 LLM 出错: Kimi API 当前负载过高或请求频繁，请稍后再试。]"])
        _patch_deps(monkeypatch, store=store, llm=llm)

        result = await deep_review.execute(
            SubQuestion(index=1, question="子问题A"), db=db
        )

        assert result.ok is False
        assert INSUFFICIENT_NOTICE in result.answer

    @pytest.mark.asyncio
    async def test_zero_chunks_marks_insufficient_without_llm_call(
        self, db, monkeypatch
    ):
        """零检索片段 → 跳过 LLM 调用（避免无依据编造），直接标记检索不足。"""
        store = _FakeStore(chunks=[])
        llm = _FakeLLM(["不应被用到"])
        _patch_deps(monkeypatch, store=store, llm=llm)

        result = await deep_review.execute(
            SubQuestion(index=1, question="子问题A"), db=db
        )

        assert result.ok is False
        assert INSUFFICIENT_NOTICE in result.answer
        assert llm.calls == []                    # 零检索不发起 LLM 调用

    @pytest.mark.asyncio
    async def test_store_unavailable_degrades(self, db, monkeypatch):
        """向量库不可用（embedding 未就绪）→ 降级标记，不抛异常。"""
        store = _FakeStore(available=False)
        llm = _FakeLLM(["不应被用到"])
        _patch_deps(monkeypatch, store=store, llm=llm)

        result = await deep_review.execute(
            SubQuestion(index=1, question="子问题A"), db=db
        )

        assert result.ok is False
        assert INSUFFICIENT_NOTICE in result.answer
        assert llm.calls == []


class TestSynthesize:
    """synthesize(topic, sub_answers) -> Review：结构化汇总，[^n^] 引用全局保留。"""

    def _sub_answers(self):
        return [
            SubAnswer(
                index=1, question="Q1", answer="答案一[^1^][^2^]",
                chunks=[_chunk("p1_c0"), _chunk("p1_c1")], ok=True,
            ),
            SubAnswer(
                index=2, question="Q2", answer="答案二[^1^]",
                chunks=[_chunk("p2_c0", title="ViT 在 WSI 的应用")], ok=True,
            ),
        ]

    @pytest.mark.asyncio
    async def test_structure_and_citation_retention(self, monkeypatch):
        """LLM 汇总文本原文进 Review.content（保留 [^n^]），citations 为全局有序引用表。"""
        final_text = "## 引言\n……[^1^]\n## 分节\n……[^3^]\n## 结论\n……"
        llm = _FakeLLM([final_text])
        _patch_deps(monkeypatch, llm=llm)

        review = await deep_review.synthesize("多实例学习综述", self._sub_answers())

        assert review.topic == "多实例学习综述"
        assert review.content == final_text        # 引用标记原样保留，不被剥离
        # 全局引用表：按子答案顺序聚合（sa1 的 2 条 + sa2 的 1 条）
        assert [c["chunk_id"] for c in review.citations] == ["p1_c0", "p1_c1", "p2_c0"]
        assert len(review.sub_answers) == 2

    @pytest.mark.asyncio
    async def test_renumbers_local_markers_in_prompt(self, monkeypatch):
        """各子答案的局部 [^n^] 在送入 LLM 前重编号为全局编号，避免跨节撞号。"""
        llm = _FakeLLM(["综述全文"])
        _patch_deps(monkeypatch, llm=llm)

        await deep_review.synthesize("主题", self._sub_answers())

        sent = "\n".join(m.get("content", "") for m in llm.calls[0])
        assert "答案一[^1^][^2^]" in sent           # sa1 局部编号 == 全局编号（偏移 0）
        assert "答案二[^3^]" in sent               # sa2 局部 [^1^] → 全局 [^3^]（偏移 2）
        assert "答案二[^1^]" not in sent

    @pytest.mark.asyncio
    async def test_failed_subanswer_marked_in_prompt(self, monkeypatch):
        """失败的子答案（检索不足标记）原样进入汇总上下文，LLM 可感知该节缺口。"""
        sub_answers = self._sub_answers() + [
            SubAnswer(
                index=3, question="Q3", answer=INSUFFICIENT_NOTICE,
                chunks=[], ok=False, error="chromadb 连接失败",
            ),
        ]
        llm = _FakeLLM(["综述全文"])
        _patch_deps(monkeypatch, llm=llm)

        await deep_review.synthesize("主题", sub_answers)

        sent = "\n".join(m.get("content", "") for m in llm.calls[0])
        assert INSUFFICIENT_NOTICE in sent

    @pytest.mark.asyncio
    async def test_llm_error_raises(self, monkeypatch):
        """汇总 LLM 出错 → 抛 DeepReviewError（与 plan 失败同一处理路径，由路由层转错误事件）。"""
        llm = _FakeLLM(["[调用 LLM 出错: Kimi 账户额度不足或已被冻结，请登录 Moonshot 控制台检查账单与额度。]"])
        _patch_deps(monkeypatch, llm=llm)

        with pytest.raises(DeepReviewError):
            await deep_review.synthesize("主题", self._sub_answers())


class TestThreeStageChain:
    """plan → execute × N → synthesize 全链路（全 mock）：单点失败不阻塞整体。"""

    @pytest.mark.asyncio
    async def test_chain_with_one_failed_subquestion(self, db, monkeypatch):
        """plan 拆 2 题；第 1 题检索抛异常降级，第 2 题正常；汇总仍产出 Review。"""
        plan_json = '{"questions": ["子问题A", "子问题B"]}'
        answer_b = "子问题B答案[^1^]"
        final_text = "## 引言\n综述……[^1^]\n## 结论\n……"
        llm = _FakeLLM([plan_json, answer_b, final_text])
        monkeypatch.setattr(deep_review, "llm_service", llm)

        # 第一次检索抛异常（子问题A 失败），第二次返回片段（子问题B 正常）
        store = _FakeStore(exc=RuntimeError("chromadb 连接失败"))
        monkeypatch.setattr(deep_review, "get_vector_store", lambda: store)

        questions = await deep_review.plan("多实例学习综述")
        assert len(questions) == 2

        sub_answers = []
        for q in questions:
            # 第 2 题前解除检索故障，模拟单点失败不阻塞
            if q.index == 2:
                store._exc = None
                store._chunks = [_chunk("p1_c0")]
            sub_answers.append(await deep_review.execute(q, db=db))

        assert sub_answers[0].ok is False
        assert INSUFFICIENT_NOTICE in sub_answers[0].answer
        assert sub_answers[1].ok is True

        review = await deep_review.synthesize("多实例学习综述", sub_answers)
        assert review.content == final_text
        assert [c["chunk_id"] for c in review.citations] == ["p1_c0"]
