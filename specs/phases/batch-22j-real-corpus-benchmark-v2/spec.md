# Batch 22J 规格：真实语料 Benchmark v2 盲化扩展

## 1. 目标

Benchmark v1 的 train/dev 已支撑多轮检索假设，继续根据已观察结果调规则会放大过拟合。
Batch 22J 从 `papers/` 中尚未被 v1 覆盖的真实论文构建新的论文级盲化基准，为后续
factoid/实体检索改进提供未触碰的评价面。

## 2. 范围与隐私

- 只读盘点 PDF 与 v1 manifest 的覆盖关系，不修改、移动或提交真实论文。
- v2 论文不得与 v1 的 train/dev/holdout 论文重叠；以内容 SHA 和稳定 paper UID 双重去重。
- 未获得真实内容出站授权前，禁止将 PDF、问题或证据发送到 Kimi/其他外部服务。
- 公开仓库只提交 schema、工具和去原文测试；v2 数据、manifest 与报告保持 gitignore。

## 3. 硬 Gate

- 覆盖清单对每个 PDF 给出稳定身份、重复组和是否已被 v1 覆盖，无原文泄漏。
- 新集合按论文 split，同一论文不能跨分区；问题类型数量在冻结前审计。
- 每条正例 evidence quote 在指定原页 100% 唯一解析；歧义、空证据或跨 split 立即失败。
- 首次运行候选前，冻结 dataset/qrels/corpus/database/page/vector 指纹和一次性 ledger。
- holdout 永久不由通用 CLI 直接打开，只允许经预注册 Gate 消费。
