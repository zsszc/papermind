# RAG 评测数据集

本目录存放 PaperMind RAG 评测用的 QA 数据集（P4 评测体系的数据层）。

## 公开可复现基准

`qa_public_v1.jsonl` 对应 `eval/fixtures/rag_public_v1.json`，二者均为 PaperMind
原创合成内容（CC0），不包含用户论文。公开集使用稳定 evidence qrels：

```json
{
  "relevant_evidence": [
    {
      "paper_uid": "doi:10.5555/papermind.alpha-mil",
      "quote": "长度至少 20 字符且在目标论文中唯一命中的逐字证据"
    }
  ]
}
```

运行命令：

```bash
cd backend
env -u PYTHONPATH venv/bin/python -m eval.run \
  --fixture eval/fixtures/rag_public_v1.json \
  --dataset eval/dataset/qa_public_v1.jsonl \
  --keyword-only --lexical-profile bm25 --threshold 0.85
```

fixture 模式只使用内存 SQLite，不连接 `data/papers.db`，也不加载模型或调用 LLM。
当前公开集用于 CI 链路正确性与回归门禁；私人真实库仍需单独评测，二者不可混算趋势。

## 文件

- `qa_seed.jsonl`：种子集，25 条，基于示例论文（`papers/demo-paper.pdf`，paper_id=1，
  ReCo-MIL 结直肠癌 T 分期）手工编写，另含 3 条负例。

## Schema（JSONL，每行一条）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `qa_id` | string | 全局唯一 id，如 `recomil-001` |
| `question` | string | 用户问题，中文为主，可少量英文 |
| `ground_truth` | string | 参考答案要点，用于生成质量评测（LLM-as-judge / 人工比对） |
| `relevant_chunks` | list[object] | 期望命中的 chunk 定位信息，见下 |
| `question_type` | string | `factoid` / `summary` / `comparison` / `method_detail` / `experiment_data` / `out_of_scope` |
| `source` | string | `demo_paper` / `synthetic` / `imported_paper` |
| `has_answer` | bool | `false` 表示“库中无答案”的负例，用于测试幻觉；负例 `relevant_chunks` 必须为空 |

### relevant_chunks 定位对象

```json
{"paper_id": 1, "section": "Method", "keywords": ["CAFR", "BiGRU"]}
```

- `paper_id`（int，必填）：`papers` 表主键。
- `section`（string，可选）：章节名，如 `Abstract` / `Method` / `Results`。
- `keywords`（list[string]，可选）：内容关键词，用于在 chunk 文本中匹配。
- `section` 与 `keywords` 至少提供一个。真实的 chunk id（`p{paper_id}_c{i}`）
  由 `eval.dataset.resolve_relevant_chunks(db, entry)` 在评测时解析，
  避免数据_chunking 策略变化导致标注失效。

## 种子集构成（25 条）

| question_type | 条数 | 覆盖点 |
| --- | --- | --- |
| method_detail | 7 | 三阶段框架、CAFR、BiGRU、consistency loss、切块与特征、sigmoid、传统 MIL 局限 |
| factoid | 5 | ResNet-50/2048 维、Adam 1e-4 cosine、数据来源医院、评价指标、混淆矩阵 |
| experiment_data | 5 | 87.3% acc / 0.914 AUC / 0.847 F1、512 WSIs 70/10/20、消融 -2.4% / -1.8% |
| comparison | 3 | vs AttentionMIL/TransMIL 数值对比、与 Ilse/TransMIL/DTFD-MIL 的区别、基线清单 |
| summary | 2 | 方法概述、结论与未来工作 |
| out_of_scope | 3 | 负例：TCGA 表现、推理速度、消融 p-value（库中无答案，应拒答） |

来源分布：`demo_paper` 22 条 + `synthetic`（负例）3 条。

## 如何扩充

目标 50–100 条。导入新论文后按下述流程添加 QA：

1. **导入论文**：通过 UI 或 `POST /api/papers/import` 导入 PDF，等待 `processed=done`，
   记下 `papers` 表中的 `paper_id`。
2. **编写 QA**：每篇论文建议 10–20 条，按上面类型分布覆盖：方法细节（模块、损失、
   网络结构）、实验数据（数据集规模、关键数值、消融）、与基线对比、总结类；
   另配 1–2 条该论文回答不了的负例（`has_answer=false`、`source=synthetic`、
   `relevant_chunks=[]`）。
3. **标注定位**：`relevant_chunks` 填 `paper_id` + 章节名 + 1–3 个原文关键词
   （英文论文直接用原文术语，便于 content 匹配）。
4. **校验**：运行
   ```bash
   cd backend && env -u PYTHONPATH venv/bin/python -c \
     "from eval.dataset import load_dataset, validate_dataset; validate_dataset(load_dataset())"
   ```
   或直接跑 `pytest tests/test_dataset.py`。
5. **qa_id 命名**：按论文取前缀，如 `recomil-001`、`psmil-001`，保持全局唯一。

多条数据集可并存（如 `qa_v2.jsonl`），通过 `load_dataset(path)` 指定加载。
