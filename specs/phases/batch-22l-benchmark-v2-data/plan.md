# Batch 22L 实施计划

1. T1（主代理）：split 分配（uid 排序种子轮换 12/11/11）+ freeze_paper_splits 排他冻结
2. T2（子代理）：eval/generate_qa_v2.py 生成器+证据唯一校验器，TDD（mock LLM），含 --resume
3. T2 实跑（主代理）：真实 LLM 生成候选（约 34 篇 × 3 条）
4. T3（用户）：人工审稿
5. T4（主代理）：冻结指纹 + train/dev 盲测基线 + 报告/台账/push
